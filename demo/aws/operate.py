# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3==1.44.9"]
# ///
"""Package, deploy and open the private BLACK FRIDAY rehearsal."""

import argparse
import getpass
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import (
    ClientError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

AWS_DIR = Path(__file__).resolve().parent
DEMO = AWS_DIR.parent
# The scripts refuse to touch any account but this one. The profile is optional;
# without it boto3 and the AWS CLI use their default credential chain.
ACCOUNT = os.environ.get('BLACK_FRIDAY_AWS_ACCOUNT', '')
REGION = 'ap-southeast-2'
PROFILE = os.environ.get('BLACK_FRIDAY_AWS_PROFILE') or None
DEPLOYMENT = os.environ.get('BLACK_FRIDAY_DEPLOYMENT', 'rehearsal')
if DEPLOYMENT not in ('rehearsal', 'round'):
    raise SystemExit('BLACK_FRIDAY_DEPLOYMENT must be rehearsal or round')
STACK = 'BlackFridayRehearsal' if DEPLOYMENT == 'rehearsal' else 'BlackFridayRound'
ARTIFACTS = STACK + 'Artifacts'
WEB_FUNCTION = 'black-friday-arena' if DEPLOYMENT == 'rehearsal' else 'black-friday-round'
PRESENTER_PARAMETER = '/black-friday/round/presenter-key'
PASSPHRASE_PARAMETER = '/black-friday/round/presenter-passphrase'
WEB_ENDPOINT = 'stage'
TAILSCALE = shutil.which('tailscale') or '/usr/local/bin/tailscale'  # macOS app CLI is not always on PATH
TAILNET_PORT = 8443
LOGGER = logging.getLogger(__name__)


def account_session():
    if not re.fullmatch(r'\d{12}', ACCOUNT):
        raise SystemExit('Set BLACK_FRIDAY_AWS_ACCOUNT to the 12-digit AWS account these scripts may use')
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    try:
        identity = session.client('sts').get_caller_identity()
    except (SSOTokenLoadError, TokenRetrievalError, UnauthorizedSSOTokenError):
        raise SystemExit(f'Renew AWS SSO: aws sso login --profile {PROFILE or "<profile>"}') from None
    if identity['Account'] != ACCOUNT:
        raise RuntimeError(f'Expected AWS account {ACCOUNT}, not {identity["Account"]}')
    print(f'AWS account {ACCOUNT} · {REGION}', flush=True)
    return session


def stack_outputs(session, stack: str) -> dict[str, str]:
    result = session.client('cloudformation').describe_stacks(StackName=stack)
    return {item['OutputKey']: item['OutputValue'] for item in result['Stacks'][0]['Outputs']}


def cloudformation_deploy(template: str, stack: str, *, keep_failed: bool = False, **parameters: str) -> None:
    command = [
        'aws', '--region', REGION, '--no-cli-pager', 'cloudformation', 'deploy',
        '--template-file', str(AWS_DIR / template), '--stack-name', stack,
        '--capabilities', 'CAPABILITY_IAM', '--tags', 'project=black-friday-demo',
        '--no-fail-on-empty-changeset',
    ]
    if keep_failed:
        command.append('--disable-rollback')
    if parameters:
        command += ['--parameter-overrides', *(f'{name}={value}' for name, value in parameters.items())]
    env = dict(os.environ)
    if PROFILE:
        command[1:1] = ['--profile', PROFILE]
        # Do not let credentials in the parent environment override the explicit profile.
        env = {key: value for key, value in env.items() if key not in {
            'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
            'AWS_SECURITY_TOKEN', 'AWS_ENDPOINT_URL', 'AWS_ENDPOINT_URL_CLOUDFORMATION',
        }}
    subprocess.run(command, env=env, check=True)


def archive(out: Path, paths: dict[str, Path], trees: dict[str, Path] | None = None) -> str:
    files = dict(paths)
    for prefix, root in (trees or {}).items():
        for path in root.rglob('*'):
            relative = path.relative_to(root)
            if path.is_file() and not any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
                files[f'{prefix}{relative.as_posix()}'] = path
    with ZipFile(out, 'w', compression=ZIP_DEFLATED) as target:
        for name, path in sorted(files.items()):
            info = ZipInfo(name)
            info.create_system = 3  # Zip consumers preserve Unix modes when extracting the image context.
            info.external_attr = 0o100644 << 16
            target.writestr(info, path.read_bytes(), compress_type=ZIP_DEFLATED)
    return sha256(out.read_bytes()).hexdigest()


def python_dependencies(root: Path, *requirements: str) -> None:
    subprocess.run([
        'uv', 'pip', 'install', '--target', str(root), '--python-version', '3.13',
        '--python-platform', 'aarch64-manylinux_2_28', '--only-binary', ':all:', *requirements,
    ], check=True)


def packages(out: Path) -> dict[str, tuple[str, Path]]:
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='black-friday-package-') as work:
        root = Path(work)
        python_dependencies(root / 'sdk', 'boto3==1.44.9', 'aws-durable-execution-sdk-python==3.1.0')
        web = root / 'web'
        web.mkdir()
        for name in ('package.json', 'package-lock.json'):
            (web / name).write_bytes((AWS_DIR / name).read_bytes())
        subprocess.run(['npm', 'ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=web, check=True)
        definitions = {
            'image': ({
                'Dockerfile': AWS_DIR / 'Microvm.Dockerfile',
                'aws/vm_runtime.py': AWS_DIR / 'vm_runtime.py',
                **{name: DEMO / name for name in ('schema.sql', 'db.php', 'shop.php', 'migrate.php')},
            }, {}),
            'worker': ({
                'evaluator.py': AWS_DIR / 'evaluator.py', 'scoring.py': AWS_DIR / 'scoring.py',
                'options.json': AWS_DIR / 'options.json',
                'virtual_rds.py': DEMO / 'virtual_rds.py', 'arena.py': DEMO / 'arena.py',
            }, {'': root / 'sdk'}),
            'workflow': ({
                'workflow.py': AWS_DIR / 'workflow.py', 'scoring.py': AWS_DIR / 'scoring.py',
                'options.json': AWS_DIR / 'options.json',
            }, {'': root / 'sdk'}),
            'web': ({
                'index.mjs': AWS_DIR / 'web.mjs',
                **{name: AWS_DIR / name for name in (
                    'game.mjs', 'host_auth.mjs', 'roster.mjs', 'options.json',
                    'player.html', 'host.html', 'round.js', 'round.css',
                )},
                'art/cloud-surge.jpg': AWS_DIR / 'art' / 'cloud-surge.jpg',  # derived from assets/cloud-surge.png
            }, {'node_modules/': web / 'node_modules'}),
        }
        results = {}
        for kind, (files, trees) in definitions.items():
            path = out / f'{kind}.zip'
            digest = archive(path, files, trees)
            results[kind] = (f'packages/{kind}/{digest}.zip', path)
            print(f'{kind}: {digest[:12]} · {path.stat().st_size:,} bytes', flush=True)
        return results


def available_base_version(session) -> str:
    microvms = session.client('lambda-microvms')
    image = f'arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1'
    versions: list[tuple[float, str]] = []
    args = {'imageIdentifier': image}
    while True:
        page = microvms.list_managed_microvm_image_versions(**args)
        for item in page.get('items', []):
            if item['status'] == 'AVAILABLE' and any(
                cpu['architecture'] == 'ARM_64' for cpu in item.get('supportedCpuConfigurations', [])
            ):
                versions.append((item['createdAt'].timestamp(), item['imageVersion']))
        if not page.get('nextToken'):
            break
        args['nextToken'] = page['nextToken']
    if not versions:
        raise RuntimeError(f'No ARM64 base image is available for {image}')
    return max(versions)[1]


def wait_web(web, *, revision: str | None = None, endpoint: bool = False) -> dict:
    for _ in range(100):
        if revision:
            item = web.get_web_function_revision(functionName=WEB_FUNCTION, revisionId=revision)
            done = item['state'] != 'Pending'
        else:
            item = web.get_web_function_endpoint(functionName=WEB_FUNCTION, endpointName=WEB_ENDPOINT)
            done = item['state'] != 'Pending' and item.get('updateStatus') != 'InProgress'
        if done:
            if item['state'] == 'Failed' or item.get('updateStatus') == 'Failed':
                raise RuntimeError(f'Web Function {"revision" if revision else "endpoint"} failed: '
                                   f'{item.get("stateReason") or item.get("updateStatusReason")}')
            return item
        sleep(3)
    raise TimeoutError('Web Function did not become active')


# A room of phones arrives at once. With 2 environments, more than about five
# simultaneous requests got HTTP 429 from the endpoint while it scaled (24 Nov
# 2026); 10 environments served 24 at once. The throttle was not the limit.
ENDPOINT_LIMITS = {'scalingConfig': {'maxEnvironments': 10}, 'throttleConfig': {'rateLimit': 100}}


def deploy_web(session, bucket: str, key: str) -> str:
    """Create or update the arena Web Function through the Lambda Web API.

    CreateWebFunction now needs its first revision and endpoint in one call, so
    the function is not a CloudFormation resource. A new revision is created only
    when the code object or its settings change, then the endpoint is pointed at it.
    """
    outputs = stack_outputs(session, STACK)
    version = session.client('s3').head_object(Bucket=bucket, Key=key)['VersionId']
    build = {
        'codeConfig': {'s3Object': {'bucket': bucket, 'key': key, 'versionId': version}},
        'runtimeConfig': {'runtime': 'nodejs24.x'},
    }

    def service_config(domain: str | None) -> dict:
        # The page's own origin, for the Origin check and the QR code. Known only once the endpoint exists.
        return {
            'executionRoleArn': outputs['WebRole'], 'timeoutSeconds': 15, 'maxConcurrencyPerEnvironment': 8,
            'environmentVariables': {
                'AWS_LAMBDA_NODEJS_ENTRYPOINT': 'index.mjs',
                'RUN_TABLE': outputs['RunTable'], 'WORKFLOW_ARN': outputs['WorkflowVersion'],
                'PRESENTER_PARAMETER': PRESENTER_PARAMETER, 'PASSPHRASE_PARAMETER': PASSPHRASE_PARAMETER,
                **({'PUBLIC_HOST': domain} if domain else {}),
            },
        }

    web = session.client('lambda-web')
    try:
        current = web.get_web_function_endpoint(functionName=WEB_FUNCTION, endpointName=WEB_ENDPOINT)
    except web.exceptions.ResourceNotFoundException:
        created = web.create_web_function(
            functionName=WEB_FUNCTION, tags={'project': 'black-friday-demo'},
            revisionConfig={'buildConfig': build, 'serviceConfig': service_config(None)},
            endpointConfig={
                'endpointName': WEB_ENDPOINT, 'endpointType': 'HomeRegion', 'authType': 'ApplicationManaged',
                'autoDeploymentMode': 'Disabled', **ENDPOINT_LIMITS,
            },
        )
        wait_web(web, revision=created['revision']['revisionId'])
        current = wait_web(web, endpoint=True)
    service = service_config(current['domainName'])
    live = web.get_web_function_revision(functionName=WEB_FUNCTION, revisionId=current['revisionWeights'][0]['revisionId'])
    same = (
        live['buildConfig']['codeConfig']['s3Object'] == build['codeConfig']['s3Object']
        and {name: live['serviceConfig'].get(name) for name in service} == service
    )
    update = {} if same else {'revisionWeights': [{'revisionId': web.create_web_function_revision(
        functionName=WEB_FUNCTION, buildConfig=build, serviceConfig=service)['revisionId'], 'weight': 100}]}
    if update:
        wait_web(web, revision=update['revisionWeights'][0]['revisionId'])
    update.update({name: value for name, value in ENDPOINT_LIMITS.items() if current.get(name) != value})
    if update:
        web.update_web_function_endpoint(functionName=WEB_FUNCTION, endpointName=WEB_ENDPOINT, **update)
    return 'https://' + wait_web(web, endpoint=True)['domainName']


def remove_web(session) -> None:
    """Delete what deploy_web created: the endpoint, the function and its logs."""
    web = session.client('lambda-web')
    try:
        web.delete_web_function_endpoint(functionName=WEB_FUNCTION, endpointName=WEB_ENDPOINT)
    except web.exceptions.ResourceNotFoundException:
        pass
    for _ in range(40):
        try:
            web.delete_web_function(functionName=WEB_FUNCTION)
            print(f'Deleted Web Function {WEB_FUNCTION}', flush=True)
            break
        except web.exceptions.ResourceNotFoundException:
            print('No Web Function to delete', flush=True)
            break
        except web.exceptions.ConflictException:
            sleep(3)
    else:
        raise TimeoutError('The Web Function endpoint is still deleting')
    logs = session.client('logs')
    prefix = f'/aws/lambda/web/{WEB_FUNCTION}'
    for page in logs.get_paginator('describe_log_groups').paginate(logGroupNamePrefix=prefix):
        for group in page['logGroups']:
            logs.delete_log_group(logGroupName=group['logGroupName'])
            print(f'Deleted log group {group["logGroupName"]}', flush=True)


def delete_stack(session, stack: str) -> None:
    cloudformation = session.client('cloudformation')
    cloudformation.delete_stack(StackName=stack)
    cloudformation.get_waiter('stack_delete_complete').wait(StackName=stack, WaiterConfig={'Delay': 10, 'MaxAttempts': 180})
    print(f'Deleted stack {stack}', flush=True)


def stack_exists(session, stack: str) -> bool:
    try:
        session.client('cloudformation').describe_stacks(StackName=stack)
    except ClientError as error:
        if error.response['Error']['Code'] == 'ValidationError' and 'does not exist' in str(error):
            return False
        raise
    return True


def artifact_buckets(session) -> list[str]:
    """The retained artifact buckets, found by the name CloudFormation gave them."""
    prefix = ARTIFACTS.lower() + '-artifacts-'
    return [b['Name'] for b in session.client('s3').list_buckets()['Buckets'] if b['Name'].startswith(prefix)]


def destroy(*, artifacts: bool = False) -> None:
    """Tear down in the one safe order. Re-runnable after a partial teardown.

    The Web Function is not in CloudFormation, so it goes first: deleting the
    stacks first would leave a function serving an arena whose run table and
    workflow are gone. The artifact stack goes before its bucket (which it
    retains), so its bucket policy is never deleted against a missing bucket;
    the bucket is then found by name, so a re-run finishes an interrupted one.
    """
    session = account_session()
    remove_web(session)
    if stack_exists(session, STACK):
        delete_stack(session, STACK)
    if not artifacts:
        print(f'Kept {ARTIFACTS} and its bucket (packages and run evidence); '
              'destroy --artifacts deletes them', flush=True)
        return
    if stack_exists(session, ARTIFACTS):
        delete_stack(session, ARTIFACTS)
    for name in artifact_buckets(session):
        bucket = session.resource('s3').Bucket(name)
        bucket.object_versions.delete()
        bucket.delete()
        print(f'Deleted bucket {name}', flush=True)


def passphrase() -> None:
    """Set the presenter's own passphrase. It is read from a prompt or stdin and never printed."""
    if DEPLOYMENT != 'round':
        raise SystemExit('Only the round has a presenter passphrase (BLACK_FRIDAY_DEPLOYMENT=round)')
    if sys.stdin.isatty():
        value = getpass.getpass('Presenter passphrase (at least 8 characters): ')
        if value != getpass.getpass('Again: '):
            raise SystemExit('The two entries differ; nothing changed')
    else:
        value = sys.stdin.readline().rstrip('\r\n')
    if len(value) < 8:
        raise SystemExit('The passphrase needs at least 8 characters; nothing changed')
    session = account_session()
    ssm = session.client('ssm')
    ssm.put_parameter(Name=PASSPHRASE_PARAMETER, Value=value, Type='SecureString', Overwrite=True)
    # describe_parameters returns metadata only, never the value.
    [item] = ssm.describe_parameters(ParameterFilters=[
        {'Key': 'Name', 'Option': 'Equals', 'Values': [PASSPHRASE_PARAMETER]},
    ])['Parameters']
    print(f'Presenter passphrase updated (version {item["Version"]}); devices sign in again within 30 s', flush=True)


def retain_web_logs(session) -> None:
    """Keep the Web Function's request logs seven days, like every other log group here. Idempotent."""
    logs = session.client('logs')
    prefix = f'/aws/lambda/web/{WEB_FUNCTION}'
    for page in logs.get_paginator('describe_log_groups').paginate(logGroupNamePrefix=prefix):
        for group in page['logGroups']:
            if group.get('retentionInDays') != 7:
                logs.put_retention_policy(logGroupName=group['logGroupName'], retentionInDays=7)
                print(f'Log group {group["logGroupName"]}: retention 7 days', flush=True)


def deploy(*, keep_failed: bool = False) -> None:
    session = account_session()
    if DEPLOYMENT == 'rehearsal':
        raise SystemExit('The rehearsal is frozen. Deploy it from tag rehearsal-fallback-2026-11-24b; use BLACK_FRIDAY_DEPLOYMENT=round for the audience round.')
    ssm = session.client('ssm')
    try:
        ssm.get_parameter(Name=PRESENTER_PARAMETER)
    except ssm.exceptions.ParameterNotFound:
        try:
            ssm.put_parameter(Name=PRESENTER_PARAMETER, Value=secrets.token_hex(32), Type='SecureString', Overwrite=False)
        except ssm.exceptions.ParameterAlreadyExists:
            pass
    base_version = available_base_version(session)
    with TemporaryDirectory(prefix='black-friday-deploy-') as work:
        artifacts = packages(Path(work))
        cloudformation_deploy('artifacts.yaml', ARTIFACTS, WebFunctionName=WEB_FUNCTION)
        bucket = stack_outputs(session, ARTIFACTS)['Bucket']
        s3 = session.client('s3')
        for key, path in artifacts.values():
            s3.upload_file(str(path), bucket, key)
        release = sha256((base_version + ''.join(key for key, _ in artifacts.values())).encode()).hexdigest()
        cloudformation_deploy(
            'rehearsal.yaml', STACK, keep_failed=keep_failed, ArtifactBucket=bucket,
            BaseImageVersion=base_version, ReleaseId=release, Prefix=WEB_FUNCTION,
            PresenterParameter=PRESENTER_PARAMETER,
            PassphraseParameter=PASSPHRASE_PARAMETER,
            **{f'{kind.title()}Key': key for kind, (key, _) in artifacts.items() if kind != 'web'},
        )
    url = deploy_web(session, bucket, artifacts['web'][0])
    retain_web_logs(session)
    print(json.dumps({**stack_outputs(session, STACK), 'StageUrl': url}, indent=2))


def stage_url(session) -> str:
    endpoint = session.client('lambda-web').get_web_function_endpoint(functionName=WEB_FUNCTION, endpointName=WEB_ENDPOINT)
    return 'https://' + endpoint['domainName']


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def tailnet_identity() -> tuple[str, str]:
    """This Mac's MagicDNS name and its owner's login, from the local tailscaled."""
    status = json.loads(subprocess.run(
        [TAILSCALE, 'status', '--self', '--json'], check=True, capture_output=True, text=True,
    ).stdout)
    me = status['Self']
    return me['DNSName'].rstrip('.'), status['User'][str(me['UserID'])]['LoginName']


def present(port: int, tailnet: bool = False) -> None:
    session = account_session()
    endpoint = stage_url(session)
    if not re.fullmatch(rf'https://[a-z0-9.-]+\.lambda-web\.{re.escape(REGION)}\.on\.aws', endpoint):
        raise RuntimeError('Unexpected Web Function stage endpoint')
    credentials = session.get_credentials() if DEPLOYMENT == 'rehearsal' else None
    presenter_key = None if credentials else session.client('ssm').get_parameter(
        Name=PRESENTER_PARAMETER, WithDecryption=True,
    )['Parameter']['Value']
    local_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    allowed_get = {'/', '/api/state', '/art/cloud-surge.png', '/art/cloud-surge.jpg', '/host', '/api/host/state', '/api/host/qr.svg', '/round.js', '/round.css'}
    allowed_post = {f'/api/{action}' for action in ('launch', 'migrate', 'compare', 'reset')}
    # Round only: these two carry a small JSON body (a team id; an empty object) and are no
    # longer bodyless. The rehearsal's old code has neither route.
    with_body = {'/api/host/remove', '/api/host/logout'} if credentials is None else set()
    allowed_post |= with_body
    http = build_opener(NoRedirect)

    def proxy(allowed_hosts: set[str], origin: str | None, login: str | None) -> type[BaseHTTPRequestHandler]:
      class Proxy(BaseHTTPRequestHandler):
        def forward(self) -> None:
            host = self.headers.get('Host', '')
            # tailscale serve sets Tailscale-User-Login itself and drops any copy a
            # client sends; tagged devices get none. So only the owner passes.
            if login is not None and self.headers.get('Tailscale-User-Login') != login:
                self.send_error(403)
                return
            if host not in allowed_hosts or self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
                self.send_error(403)
                return
            length = 0
            if self.command == 'POST':
                expected = origin or f'http://{host}'
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    length = -1
                if (
                    self.path not in allowed_post or self.headers.get('X-Rehearsal-Action') != '1'
                    or self.headers.get('Origin', expected) != expected
                    or self.headers.get('Transfer-Encoding') is not None
                    or not (0 <= length <= 2048 if self.path in with_body else length == 0)
                ):
                    self.send_error(403)
                    return
            elif self.path not in allowed_get:
                self.send_error(404)
                return
            body = self.rfile.read(length) if length > 0 else b''
            headers = {'X-Amz-Content-SHA256': sha256(body).hexdigest()}
            if body:
                headers['Content-Type'] = 'application/json'
            for name in ('X-Rehearsal-Action', 'X-Rehearsal-Run'):
                if value := self.headers.get(name):
                    headers[name] = value
            url = endpoint + self.path
            try:
                req = AWSRequest(method=self.command, url=url, headers=headers, data=body)
                if credentials:
                    SigV4Auth(credentials.get_frozen_credentials(), 'lambda', REGION).add_auth(req)
                else:
                    req.headers['X-Presenter-Key'] = presenter_key
                try:
                    result = http.open(Request(url, data=body or None, headers=dict(req.headers), method=self.command), timeout=20)
                except HTTPError as error:
                    result = error
                with result:
                    data = result.read()
                    status = result.status
                    media_type = result.headers.get('Content-Type', 'application/octet-stream')
                    security_headers = {name: result.headers[name] for name in (
                        'Content-Security-Policy', 'X-Content-Type-Options', 'Referrer-Policy',
                    ) if name in result.headers}
            except (URLError, OSError, RuntimeError) as error:
                LOGGER.error('Stage request failed: %s', type(error).__name__)
                self.send_error(502, 'AWS stage unavailable; check your SSO login')
                return
            self.send_response(status)
            self.send_header('Content-Type', media_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            for name, value in security_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        do_GET = forward
        do_POST = forward

        def log_message(self, format: str, *args) -> None:
            if login is not None:
                super().log_message(format, *args)
      return Proxy

    path = '/' if DEPLOYMENT == 'rehearsal' else '/host'
    with ThreadingHTTPServer(('127.0.0.1', port), proxy(local_hosts, None, None)) as server:
        print(f'Open http://127.0.0.1:{port}{path} · the arena is served by {endpoint}', flush=True)
        serve = None
        if tailnet:
            name, login = tailnet_identity()
            # 443 is taken on this Mac's tailnet address outside the serve config; 8443 is free.
            public = f'https://{name}:{TAILNET_PORT}'
            remote = ThreadingHTTPServer(
                ('127.0.0.1', port + 1), proxy({f'{name}:{TAILNET_PORT}', name, f'127.0.0.1:{port + 1}'}, public, login),
            )
            Thread(target=remote.serve_forever, daemon=True).start()
            # Foreground serve: published only while this process runs, tailnet only (not Funnel).
            serve = subprocess.Popen(
                [TAILSCALE, 'serve', f'--https={TAILNET_PORT}', f'http://127.0.0.1:{port + 1}'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            sleep(3)
            if serve.poll() is not None:
                raise SystemExit(f'tailscale serve failed: {serve.stderr.read().strip()}')
            print(f'Tailnet: {public}{path} · only {login}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if serve is not None:
                serve.terminate()
                serve.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    package = commands.add_parser('package', help='Build deployment archives without AWS')
    package.add_argument('output', type=Path)
    deploy_command = commands.add_parser('deploy', help='Deploy the private rehearsal into your AWS account')
    deploy_command.add_argument('--keep-failed', action='store_true', help='Preserve failed stack resources for diagnosis')
    stage = commands.add_parser('present', help='Proxy to the selected deployment (IAM rehearsal, keyed round)')
    stage.add_argument('--port', type=int, default=8767 if DEPLOYMENT == 'rehearsal' else 8768)
    stage.add_argument('--tailnet', action='store_true',
                       help="Also publish the presenter page on this Mac's tailnet name, for its owner only")
    commands.add_parser('passphrase', help="Set the round's presenter passphrase (prompt, or stdin when piped)")
    teardown = commands.add_parser('destroy', help='Delete only the selected deployment')
    teardown.add_argument('--artifacts', action='store_true',
                          help='Also delete the artifact bucket (all versions) and its stack')
    args = parser.parse_args()
    if args.command == 'deploy':
        deploy(keep_failed=args.keep_failed)
    elif args.command == 'passphrase':
        passphrase()
    elif args.command == 'present':
        present(args.port, args.tailnet)
    elif args.command == 'destroy':
        destroy(artifacts=args.artifacts)
    else:
        packages(args.output)


if __name__ == '__main__':
    main()
