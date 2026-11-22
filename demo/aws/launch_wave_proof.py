# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3==1.44.9"]
# ///
"""Run and tear down a private Lambda launch-wave benchmark in Oregon."""

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from operate import (
    ARTIFACTS,
    AWS_DIR,
    DEMO,
    account_session,
    archive,
    python_dependencies,
    stack_outputs,
)

ACTIVE = {'RUNNING', 'PAUSING', 'PAUSED'}
WAITER = {'Delay': 5, 'MaxAttempts': 60}


def announce(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def package(path: Path) -> str:
    with TemporaryDirectory(prefix='black-friday-launch-sdk-') as work:
        sdk = Path(work) / 'sdk'
        python_dependencies(sdk, 'aws-durable-execution-sdk-python==3.1.0')
        return archive(path, {
            'launch_jobs.py': AWS_DIR / 'launch_jobs.py',
            'launch_wave.py': DEMO / 'launch_wave.py',
            'virtual_rds.py': DEMO / 'virtual_rds.py',
            'arena.py': DEMO / 'arena.py',
        }, {'': sdk})


def peak_overlap(jobs: list[dict]) -> int:
    events = []
    for job in jobs:
        start, end = job['started_us'], job['ended_us']
        if start >= end:
            raise RuntimeError('Invalid worker timing interval')
        events.extend(((start, 1), (end, -1)))
    active = peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return peak


def summarize(result: dict, elapsed_ms: int) -> dict:
    jobs = result['jobs']
    plans = ('small', 'replicas', 'large', 'cached')
    expected = {(cohort, plan) for cohort in range(result['cohorts']) for plan in plans}
    if len(jobs) != len(expected) or {(job['cohort'], job['plan']) for job in jobs} != expected:
        raise RuntimeError('Durable execution did not complete every planned evaluation once')
    if len({job['aws_request_id'] for job in jobs}) != len(jobs):
        raise RuntimeError('Expected a distinct Event Lambda request ID for every job')
    return {
        'scenario': result['scenario'], 'configured_parallelism': result['parallel'],
        'completed_jobs': len(jobs), 'observed_peak_workers': peak_overlap(jobs),
        'durable_elapsed_ms': elapsed_ms, 'http_requests': sum(job['http_requests'] for job in jobs),
        'plans': {
            plan: {
                'verified_sales': sum(job['verified_sales'] for job in jobs if job['plan'] == plan),
                'profit_cents': sum(job['profit_cents'] for job in jobs if job['plan'] == plan),
                'unlock_rejections': sum(
                    phase['storage_rejections'] for job in jobs if job['plan'] == plan
                    for phase in job['phases'] if phase['name'] == 'unlock'
                ),
            } for plan in plans
        },
        'jobs': jobs,
    }


def invoke(client, function: str, cohorts: int, parallel: int, executions: list[str]) -> dict:
    response = client.invoke(
        FunctionName=function, InvocationType='Event', DurableExecutionName=f'wave-{uuid4().hex}',
        Payload=json.dumps({'cohorts': cohorts, 'parallel': parallel}).encode(),
    )
    with closing(response['Payload']) as payload:
        payload.read()
    arn = response['DurableExecutionArn']
    executions.append(arn)
    announce(f'Started {4 * cohorts} jobs, limit {parallel}: {arn}')
    deadline = monotonic() + 330
    while monotonic() < deadline:
        state = client.get_durable_execution(DurableExecutionArn=arn)
        status = state['Status']
        if status == 'SUCCEEDED':
            elapsed = round((state['EndTimestamp'] - state['StartTimestamp']).total_seconds() * 1000)
            report = summarize(json.loads(state['Result']), elapsed)
            report['started_us'] = int(state['StartTimestamp'].timestamp() * 1_000_000)
            report['ended_us'] = int(state['EndTimestamp'].timestamp() * 1_000_000)
            report['execution_arn'] = arn
            announce(f'{report["completed_jobs"]} jobs, {report["http_requests"]:,} HTTP requests, '
                     f'peak {report["observed_peak_workers"]} workers, {elapsed / 1000:.2f}s wall time')
            return report
        if status not in ACTIVE:
            raise RuntimeError(f'Launch wave {status}: {state.get("Error", {})}')
        sleep(1)
    raise RuntimeError(f'Timed out waiting for {arn}')


def clean_runs(client, executions: list[str]) -> None:
    for arn in executions:
        state = client.get_durable_execution(DurableExecutionArn=arn)
        if state['Status'] in ACTIVE:
            client.stop_durable_execution(DurableExecutionArn=arn, ErrorMessage='Demo harness cleanup')
        client.delete_durable_execution(DurableExecutionArn=arn)


def signature(report: dict) -> list[tuple]:
    return [
        (job['cohort'], job['plan'], job['seed'], job['http_requests'], job['verified_sales'], job['profit_cents'],
         [(p['name'], p['http_requests'], p['storage_units'], p['storage_rejections'], p['verified_sales'])
          for p in job['phases']])
        for job in report['jobs']
    ]


@contextmanager
def uploaded_package(session) -> Iterator[tuple[str, dict[str, str]]]:
    bucket = stack_outputs(session, ARTIFACTS)['Bucket']
    s3 = session.client('s3')
    with TemporaryDirectory(prefix='black-friday-launch-wave-') as work:
        path = Path(work) / 'launch-wave.zip'
        digest = package(path)
        key = f'packages/launch-wave/{uuid4().hex}/{digest}.zip'
        with path.open('rb') as archive_file:
            version = s3.put_object(Bucket=bucket, Key=key, Body=archive_file)['VersionId']
    try:
        yield digest, {'ArtifactBucket': bucket, 'ArtifactKey': key, 'ArtifactVersion': version}
    finally:
        s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
        announce('Deleted the temporary S3 package version')


@contextmanager
def installed_stack(session, stack: str, artifacts: dict[str, str]) -> Iterator[tuple[object, str, list[str]]]:
    cfn, client = (session.client(name) for name in ('cloudformation', 'lambda'))
    stack_id = stack
    executions: list[str] = []
    try:
        announce(f'Creating temporary stack {stack}')
        stack_id = cfn.create_stack(
            StackName=stack, TemplateBody=(AWS_DIR / 'launch-wave.yaml').read_text(),
            Capabilities=['CAPABILITY_IAM'], Tags=[{'Key': 'project', 'Value': 'black-friday-demo'}],
            Parameters=[{'ParameterKey': name, 'ParameterValue': value} for name, value in artifacts.items()],
        )['StackId']
        try:
            cfn.get_waiter('stack_create_complete').wait(StackName=stack_id, WaiterConfig=WAITER)
        except WaiterError:
            events = cfn.describe_stack_events(StackName=stack_id)['StackEvents']
            for event in events:
                if event['ResourceStatus'] == 'CREATE_FAILED':
                    announce(f'{event["LogicalResourceId"]}: {event.get("ResourceStatusReason", "")}')
            raise
        yield client, stack_outputs(session, stack_id)['WorkflowVersion'], executions
    finally:
        try:
            clean_runs(client, executions)
        finally:
            cfn.delete_stack(StackName=stack_id)
            cfn.get_waiter('stack_delete_complete').wait(StackName=stack_id, WaiterConfig=WAITER)
            announce(f'Deleted temporary stack {stack}')


def stack_name() -> str:
    return f'BlackFridayLaunchWave-{uuid4().hex[:12]}'


def proof(cohorts: int, parallel: int, compare_serial: bool) -> dict:
    with redirect_stdout(sys.stderr):
        session = account_session()
    with uploaded_package(session) as (digest, artifacts), installed_stack(
        session, stack_name(), artifacts,
    ) as (client, function, executions):
        serial = invoke(client, function, cohorts, 1, executions) if compare_serial else None
        scaled = invoke(client, function, cohorts, parallel, executions)
        if serial is not None and signature(serial) != signature(scaled):
            raise RuntimeError('Business results differ between serial and parallel execution')
    return {'deployment_digest': digest, 'serial': serial, 'scaled': scaled}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('package', help='Write a Lambda deployment ZIP without contacting AWS')
    build.add_argument('output', type=Path)
    live = commands.add_parser('proof', help='Create, run and delete a private proof stack')
    live.add_argument('--cohorts', type=int, choices=range(1, 9), default=8)
    live.add_argument('--workers', type=int, choices=range(1, 9), default=8)
    live.add_argument('--compare-serial', action='store_true', help='Run the exact same jobs serially first')
    args = parser.parse_args()
    try:
        if args.command == 'package':
            print(package(args.output))
        else:
            print(json.dumps(proof(args.cohorts, args.workers, args.compare_serial), indent=2))
    except (BotoCoreError, ClientError, OSError, RuntimeError, ValueError) as error:
        announce(f'Launch-wave proof: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
