"""Durable Launch → two migrations → the shopper returns."""

import json
import os
import re
from typing import Any

import boto3
from aws_durable_execution_sdk_python import (
    CallbackTimeoutError,
    DurableContext,
    durable_execution,
)
from aws_durable_execution_sdk_python.config import CallbackConfig, Duration
from botocore.exceptions import ClientError
from scoring import rank, total_profit, validate_teams

DB = boto3.client('dynamodb')
TABLE = os.environ['RUN_TABLE']
EVALUATOR = os.environ['EVALUATOR_ARN']
KEY = {'pk': {'S': 'arena'}}
CALLBACK = CallbackConfig(timeout=Duration.from_hours(23))


def register(run_id: str, arn: str) -> None:
    # A delayed async delivery must prove that its reservation is still active
    # before it can create any MicroVMs. This is the first durable step.
    DB.update_item(
        TableName=TABLE,
        Key=KEY,
        UpdateExpression='SET execution_arn = :arn REMOVE start_deadline',
        ConditionExpression=(
            'run_id = :id AND phase = :launching '
            'AND (attribute_not_exists(execution_arn) OR execution_arn = :arn)'
        ),
        ExpressionAttributeValues={
            ':arn': {'S': arn}, ':id': {'S': run_id}, ':launching': {'S': 'launching'},
        },
    )


def publish(
    run_id: str,
    arn: str,
    previous: tuple[str, ...],
    phase: str,
    fields: dict[str, str],
) -> None:
    # A retried write can find a later phase, including when the browser has
    # already sent a callback before this step's checkpoint was acknowledged.
    values = {':id': {'S': run_id}, ':arn': {'S': arn}, ':phase': {'S': phase}}
    before = []
    for index, value in enumerate(previous):
        key = f':old{index}'
        values[key] = {'S': value}
        before.append(key)
    updates = ['phase = :phase']
    for field, value in fields.items():
        values[f':{field}'] = {'S': value}
        updates.append(f'{field} = :{field}')
    remove = ['lease_until']
    if 'callback_id' not in fields:
        remove.append('callback_id')
    try:
        DB.update_item(
            TableName=TABLE,
            Key=KEY,
            UpdateExpression=f'SET {", ".join(updates)} REMOVE {", ".join(remove)}',
            ConditionExpression=f'run_id = :id AND execution_arn = :arn AND phase IN ({", ".join(before)})',
            ExpressionAttributeValues=values,
        )
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def evaluate_forks(context: DurableContext, run_id: str, action: str, teams: list[dict]) -> list[dict[str, Any]]:
    def fork(team: dict):
        def run(child: DurableContext) -> dict[str, Any]:
            event = {'id': run_id, 'action': action, 'team': team['id'], 'kind': team['kind']}
            if action == 'compare':
                event['architecture'] = team['architecture']
            return {**child.invoke(EVALUATOR, event, name=f'{action}-{team["id"]}'), 'id': team['id']}

        return run

    result = context.parallel([fork(team) for team in teams], name=f'{action}-every-team')
    result.throw_if_error()
    by_team = {branch['id']: branch for branch in result.get_results()}
    return [by_team[team['id']] for team in teams]


def wait_for_action(context, run_id, arn, previous, phase, busy, action, fields) -> dict:
    callback = context.create_callback(name=f'await-{action}', config=CALLBACK)
    context.step(
        lambda _: publish(run_id, arn, (previous,), phase, {**fields, 'callback_id': callback.callback_id}),
        name=f'publish-{phase}',
    )
    try:
        value = callback.result()
    except CallbackTimeoutError:
        context.step(
            lambda _: publish(run_id, arn, (phase, busy), 'expired', {'expired_action': action}),
            name=f'expire-{action}',
        )
        raise
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    if not isinstance(value, dict) or value.get('action') != action:
        raise ValueError(f'Unexpected result for {action} callback')
    return value


@durable_execution
def handler(event: dict[str, Any], context: DurableContext) -> dict[str, str]:
    run_id = event['id']
    if not isinstance(run_id, str) or not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Invalid rehearsal ID')
    arn = context.execution_context.durable_execution_arn
    context.step(lambda _: register(run_id, arn), name='register-execution')
    launch = context.invoke(EVALUATOR, {'id': run_id, 'action': 'launch'}, name='launch-shop')
    if launch['vm_state'] != 'terminated':
        raise RuntimeError('The Launch MicroVM was not observed terminated')
    # Close carries a snapshot of every pick inside the durable callback.
    closed = wait_for_action(context, run_id, arn, 'launching', 'launch', 'migrating', 'migrate', {
        'checkpoint': launch['checkpoint'],
        'launch_data': json.dumps(launch['launch']),
        'launch_vm_id': launch['vm_id'],
        'launch_vm_state': launch['vm_state'],
    })
    teams = validate_teams(closed.get('teams'))
    migrations = evaluate_forks(context, run_id, 'migrate', teams)
    wait_for_action(context, run_id, arn, 'migrating', 'migrated', 'comparing', 'compare', {
        'migrations_data': json.dumps(migrations),
    })
    outcomes = evaluate_forks(context, run_id, 'compare', teams)
    results = []
    for team, migration, outcome in zip(teams, migrations, outcomes, strict=True):
        results.append({
            **migration, **outcome,
            'id': team['id'], 'name': team['name'], 'house': team['house'], 'release': team['release'],
            'cumulative_cents': total_profit(outcome['profit_cents']),
        })
    context.step(
        lambda _: publish(run_id, arn, ('comparing',), 'compared', {'outcomes_data': json.dumps(rank(results))}),
        name='publish-comparison',
    )
    return {'id': run_id, 'execution_arn': arn}
