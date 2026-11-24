"""Durable four-round BLACK FRIDAY game: real evidence first, then four modelled rounds."""

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import boto3
import model
from aws_durable_execution_sdk_python import (
    CallbackTimeoutError,
    DurableContext,
    durable_execution,
)
from aws_durable_execution_sdk_python.config import CallbackConfig, Duration
from botocore.exceptions import ClientError

DB = boto3.client('dynamodb')
TABLE = os.environ['RUN_TABLE']
EVALUATOR = os.environ['EVALUATOR_ARN']
KEY = {'pk': {'S': 'arena'}}
CALLBACK = CallbackConfig(timeout=Duration.from_hours(23))
# The growth migrations: option id -> evaluator `option`. rounds.json holds the recorded fallback for each.
MIGRATIONS = ('careful', 'fast')
RECORDED = json.loads(Path(__file__).with_name('rounds.json').read_text())['recorded_evidence']
# DynamoDB reserved words used as attribute names.
NAMES = {'round': '#round', 'data': '#data'}


def register(run_id: str, arn: str) -> None:
    # A delayed async delivery must prove that its reservation is still active
    # before it can create any MicroVMs. This is the first durable step.
    DB.update_item(
        TableName=TABLE,
        Key=KEY,
        UpdateExpression='SET execution_arn = :arn REMOVE start_deadline',
        ConditionExpression=(
            'run_id = :id AND phase = :starting '
            'AND (attribute_not_exists(execution_arn) OR execution_arn = :arn)'
        ),
        ExpressionAttributeValues={
            ':arn': {'S': arn}, ':id': {'S': run_id}, ':starting': {'S': 'starting'},
        },
    )


def publish(
    run_id: str,
    arn: str,
    previous: tuple[str, ...],
    phase: str,
    fields: dict[str, str],
    expect_round: str | None = None,
) -> None:
    # A retried write can find a later phase, including when the browser has
    # already sent a callback before this step's checkpoint was acknowledged.
    # `expect_round` stops a retried write from re-opening a round that has since closed.
    values = {':id': {'S': run_id}, ':arn': {'S': arn}, ':phase': {'S': phase}}
    before = []
    for index, value in enumerate(previous):
        key = f':old{index}'
        values[key] = {'S': value}
        before.append(key)
    updates = ['phase = :phase']
    for field, value in fields.items():
        values[f':{field}'] = {'S': value}
        updates.append(f'{NAMES.get(field, field)} = :{field}')
    remove = ['lease_until']
    if 'callback_id' not in fields:
        remove.append('callback_id')
    condition = f'run_id = :id AND execution_arn = :arn AND phase IN ({", ".join(before)})'
    if expect_round is not None:
        values[':expround'] = {'S': expect_round}
        condition += ' AND #round = :expround'
    names = {'#round': 'round'} if 'round' in fields or expect_round is not None else {}
    try:
        DB.update_item(
            TableName=TABLE,
            Key=KEY,
            UpdateExpression=f'SET {", ".join(updates)} REMOVE {", ".join(remove)}',
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
            **({'ExpressionAttributeNames': names} if names else {}),
        )
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def publish_results(run_id: str, round_id: str, scored: dict[str, Any]) -> None:
    # One immutable item per (run, round); the web service caches it forever by key.
    data = {key: scored[key] for key in ('round', 'market', 'summary', 'rows')}
    DB.update_item(
        TableName=TABLE,
        Key={'pk': {'S': f'results#{run_id}#{round_id}'}},
        UpdateExpression='SET run_id = :id, #round = :round, #data = :data',
        ExpressionAttributeNames={'#round': 'round', '#data': 'data'},
        ExpressionAttributeValues={
            ':id': {'S': run_id}, ':round': {'S': round_id}, ':data': {'S': json.dumps(data)},
        },
    )


def check_launch(result: Any) -> None:
    if not isinstance(result, dict) or result.get('vm_state') != 'terminated':
        raise RuntimeError('The Launch MicroVM was not observed terminated')


def check_migrate(result: Any) -> None:
    if not isinstance(result, dict):
        raise RuntimeError('The migration returned no result')


def check_compare(result: Any) -> None:
    check_launch(result)
    if not isinstance(result.get('cart_kept'), bool):
        raise RuntimeError('The check did not say whether the cart survived')


def fork_options(context: DurableContext, run_id: str, action: str, stage: str, options: list[str], verify) -> dict[str, dict]:
    """One MicroVM per option; one option failing never touches the other."""

    def fork(option: str):
        def run(child: DurableContext) -> dict[str, Any]:
            try:
                result = child.invoke(
                    EVALUATOR, {'id': run_id, 'action': action, 'option': option}, name=f'{action}-{option}',
                )
                verify(result)
            except Exception as error:  # noqa: BLE001 - any failure of this option means its recorded evidence
                print(json.dumps({'fallback': option, 'action': action, 'error': repr(error)}))
                return {'id': option, 'ok': False}
            return {'id': option, 'ok': True, 'result': result}

        return run

    branches = context.parallel([fork(option) for option in options], name=f'{stage}-every-option')
    branches.throw_if_error()
    return {branch['id']: branch for branch in branches.get_results()}


def real_evidence(run_id: str, context: DurableContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Launch, then both migrations, then both checks. Any failure falls back to recorded evidence alone."""
    try:
        launch = context.invoke(EVALUATOR, {'id': run_id, 'action': 'launch'}, name='launch-shop')
        check_launch(launch)
        shop = launch.get('launch')
        launch_data = {**(shop if isinstance(shop, dict) else {}), 'source': 'real'}
    except Exception as error:  # noqa: BLE001
        print(json.dumps({'fallback': 'launch', 'error': repr(error)}))
        launch_data = {'source': 'recorded'}
    migrations = fork_options(context, run_id, 'migrate', 'migrate', list(MIGRATIONS), check_migrate)
    migrated = [option for option in MIGRATIONS if migrations[option]['ok']]
    # A migration that failed has nothing to check, so that option is recorded without a second MicroVM.
    checks = fork_options(context, run_id, 'compare', 'check', migrated, check_compare) if migrated else {}
    evidence = {}
    for option in MIGRATIONS:
        check = checks.get(option)
        if check and check['ok']:
            evidence[option] = {**check['result'], 'source': 'real'}
        else:
            evidence[option] = {**RECORDED[option], 'source': 'recorded'}
    return launch_data, evidence


def wait_for_close(context: DurableContext, run_id: str, arn: str, round_id: str, previous: dict[str, Any] | None,
                   fields: dict[str, str]) -> dict[str, Any]:
    first = previous is None
    callback = context.create_callback(name=f'await-close-{round_id}', config=CALLBACK)
    context.step(
        lambda _: publish(
            run_id, arn, ('starting',) if first else ('closing',), 'open',
            {**fields, 'round': round_id, 'callback_id': callback.callback_id},
            expect_round=None if first else previous['round'],
        ),
        name=f'publish-open-{round_id}',
    )
    try:
        value = callback.result()
    except CallbackTimeoutError:
        context.step(
            lambda _: publish(run_id, arn, ('open', 'closing'), 'expired', {'expired_action': 'close'},
                              expect_round=round_id),
            name=f'expire-close-{round_id}',
        )
        raise
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    if not isinstance(value, dict) or value.get('action') != 'close' or value.get('round') != round_id:
        raise ValueError(f'Unexpected result for the {round_id} close callback')
    return value


@durable_execution
def handler(event: dict[str, Any], context: DurableContext) -> dict[str, str]:
    run_id = event['id']
    if not isinstance(run_id, str) or not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Invalid rehearsal ID')
    arn = context.execution_context.durable_execution_arn
    context.step(lambda _: register(run_id, arn), name='register-execution')
    # Every MicroVM runs here, once, before Round 1 opens. Nothing after this line creates one.
    launch_data, evidence = real_evidence(run_id, context)
    scored = None
    previous_round = None
    for round_id in model.ROUND_IDS:
        fields = {'results_round': previous_round} if previous_round else {
            'launch_data': json.dumps(launch_data),
            'evidence_data': json.dumps(evidence),
        }
        closed = wait_for_close(context, run_id, arn, round_id, scored, fields)
        players = closed.get('players')
        # The one place randomness enters a round. A durable step is checkpointed, so a replay
        # gets the recorded draws back and never draws again.
        plan = context.step(
            lambda _: model.plan(round_id, model.validate_players(players), scored, secrets.randbelow),
            name=f'plan-{round_id}',
        )
        scored = context.step(lambda _: model.score(round_id, plan, scored, evidence), name=f'score-{round_id}')
        context.step(lambda _: publish_results(run_id, round_id, scored), name=f'publish-results-{round_id}')
        previous_round = round_id
    context.step(
        lambda _: publish(run_id, arn, ('closing',), 'final', {'results_round': previous_round},
                          expect_round=previous_round),
        name='publish-final',
    )
    return {'id': run_id, 'execution_arn': arn}
