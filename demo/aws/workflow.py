"""Durable four-round BLACK FRIDAY game: real evidence first, then four modelled rounds."""

import json
import os
import re
import secrets
import time
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
# The presenter's "Behind the scenes" timeline: one item per run, capped, written only inside Durable steps.
TIMELINE_CAP = 300
TIMELINE_DETAIL = frozenset((
    'duration_s', 'vm_id', 'execution_arn', 'round', 'option', 'cart_status', 'charges', 'market', 'players',
    'picked', 'source', 'checkout_status', 'checkout_ms', 'bots', 'best_option', 'leader', 'run_id',
))
LABELS = {'careful': 'Careful', 'fast': 'Fast'}


def evaluator_log_group() -> str | None:
    parts = EVALUATOR.split(':')
    return f'/aws/lambda/{parts[6]}' if len(parts) >= 7 and parts[5] == 'function' and parts[6] else None


def clean_detail(detail: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in detail.items():
        if key not in TIMELINE_DETAIL or value is None:
            continue
        if isinstance(value, str):
            value = value[:100]
        elif isinstance(value, list):
            value = [str(item)[:100] for item in value][:10]
        elif not isinstance(value, (bool, int, float)):
            continue
        out[key] = value
    return out


def line(line_id: str, tag: str, text: str, detail: dict[str, Any] | None = None, microvm: bool = False) -> dict[str, Any]:
    group = evaluator_log_group() if microvm else os.environ.get('AWS_LAMBDA_LOG_GROUP_NAME')
    return {
        'id': line_id, 'tag': tag, 'text': text[:100],
        **({'detail': clean_detail(detail)} if detail else {}),
        **({'log_group': group} if group else {}),
    }


def timeline(run_id: str, make) -> None:
    """Append lines for this run. Called only inside a checkpointed step, so a replay never repeats a line.
    Nothing here may change the game: every failure is logged and swallowed."""
    try:
        now = int(time.time() * 1000)
        lines = [{'t': now + index, **item} for index, item in enumerate(make())]
        if not lines:
            return
        DB.update_item(
            TableName=TABLE,
            Key={'pk': {'S': f'timeline#{run_id}'}},
            UpdateExpression='SET run_id = :run, #lines = list_append(if_not_exists(#lines, :empty), :new)',
            ConditionExpression='attribute_not_exists(#lines) OR size(#lines) < :cap',
            ExpressionAttributeNames={'#lines': 'lines'},
            ExpressionAttributeValues={
                ':run': {'S': run_id}, ':cap': {'N': str(TIMELINE_CAP)}, ':empty': {'L': []},
                ':new': {'L': [{'S': json.dumps(item, separators=(',', ':'))} for item in lines]},
            },
        )
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':  # the cap was reached: ignore
            print(json.dumps({'timeline_error': repr(error)}))
    except Exception as error:  # noqa: BLE001 - the timeline never fails the game
        print(json.dumps({'timeline_error': repr(error)}))


def say(step: str, make) -> None:
    """One `[WORKFLOW] <step>: <text>` log line. Called only inside a checkpointed step, so a replay never
    repeats it. Text is counts, labels and statuses only; it never carries ids, names, tokens or links.
    Nothing here may change the game: a failure to build the text is swallowed."""
    try:
        print(f'[WORKFLOW] {step}: {make()}')
    except Exception:  # noqa: BLE001 - logging never fails the game
        pass


def market_text(round_id: str, market: Any) -> str:
    unit = next((rnd['market'].get('unit', '') for rnd in getattr(model, 'ROUNDS', ()) if rnd.get('id') == round_id), '')
    return f'{market}{unit}'


def evidence_text(launch_data: dict[str, Any], evidence: dict[str, Any]) -> str:
    parts = [f'launch {launch_data.get("source", "recorded")}']
    for option in MIGRATIONS:
        result = evidence[option]
        kept = result.get('cart_kept')
        parts.append(f'{option} {result.get("source", "recorded")} (cart {"kept" if kept else "lost"})')
    return ', '.join(parts)


def plan_text(round_id: str, plan: dict[str, Any]) -> str:
    rows = [row for row in plan.get('players') or [] if isinstance(row, dict)]
    bots = sum(1 for row in rows if row.get('bot') is True)
    humans = sum(1 for row in rows if not row.get('house') and row.get('bot') is not True)
    return f'market {market_text(round_id, plan["market"])}, {bots} bots, {humans} players'


def score_text(round_id: str, scored: dict[str, Any]) -> str:
    best, _, total = score_parts(round_id, scored)
    return ', '.join(([f'best option {option_label(round_id, best)}'] if best else []) + (
        [f'leader {total}'] if total else [])) or 'round scored'


def round_title(round_id: str) -> str:
    for rnd in getattr(model, 'ROUNDS', ()):
        if rnd.get('id') == round_id and isinstance(rnd.get('title'), str):
            return rnd['title']
    return f'Round {model.ROUND_IDS.index(round_id) + 1} · {round_id.capitalize()}'


def option_label(round_id: str, option: str) -> str:
    for rnd in getattr(model, 'ROUNDS', ()):
        if rnd.get('id') == round_id:
            for item in rnd.get('options', ()):
                if item.get('id') == option:
                    return str(item.get('label') or option)
    return option


def money(cents: Any) -> str:
    return f'-${-cents / 100:,.0f}' if cents < 0 else f'${cents / 100:,.0f}'


def leader_of(scored: dict[str, Any]) -> tuple[str | None, str | None]:
    rows = scored.get('rows') or []
    top = rows[0] if rows and isinstance(rows[0], dict) else None
    if not top or not isinstance(top.get('name'), str):
        return None, None
    cents = top.get('cumulative_cents')
    return top['name'], money(cents) if isinstance(cents, int) else None


def evidence_lines(launch_data: dict[str, Any], evidence: dict[str, Any], notes: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    real = launch_data.get('source') == 'real'
    statuses = launch_data.get('checkout_statuses')
    statuses = statuses if isinstance(statuses, list) and len(statuses) == 2 else None
    charges = launch_data.get('charges')
    text = 'Launch shop (real MicroVM)' if real else 'Launch shop (recorded fallback)'
    extra = ([f'checkout {statuses[0]}→{statuses[1]}'] if statuses and real else []) + (
        [f'{charges} charge{"" if charges == 1 else "s"}'] if isinstance(charges, int) and real else [])
    out.append(line('launch', 'MICROVM' if real else 'DURABLE', ': '.join(filter(None, [text, ', '.join(extra)])), {
        'source': launch_data.get('source'), 'vm_id': notes.get('launch_vm'), 'charges': charges if real else None,
        'checkout_status': statuses[1] if statuses and real else None,
        'cart_status': launch_data.get('cart_status') if real else None,
    }, microvm=real))
    for option in MIGRATIONS:
        label, ran = LABELS[option], option in notes.get('migrate_vm', {})
        out.append(line(f'migrate-{option}', 'MICROVM' if ran else 'DURABLE',
                        f'{label} migration done (real MicroVM)' if ran else f'{label} migration: recorded evidence used',
                        {'option': option, 'vm_id': notes.get('migrate_vm', {}).get(option),
                         'source': 'real' if ran else 'recorded'}, microvm=ran))
    for option in MIGRATIONS:
        result = evidence[option]
        real = result.get('source') == 'real'
        status, kept = result.get('cart_status'), result.get('cart_kept')
        outcome = f'cart {"kept" if kept else "LOST"}' + (f' ({status})' if isinstance(status, int) else '')
        ms = result.get('checkout_ms')
        out.append(line(f'compare-{option}', 'MICROVM' if real else 'DURABLE',
                        f'{LABELS[option]} migration check{"" if real else " (recorded)"}: {outcome}', {
                            'option': option, 'source': result.get('source'), 'cart_status': status,
                            'charges': result.get('charges'), 'checkout_status': result.get('checkout_status'),
                            'checkout_ms': ms, 'vm_id': result.get('vm_id') if real else None,
                            'duration_s': round(ms / 1000, 3) if real and isinstance(ms, (int, float)) else None,
                        }, microvm=real))
    return out


def score_parts(round_id: str, scored: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(best option id, leader name, leader's total) of a scored round."""
    options = (scored.get('summary') or {}).get('options') or {}
    played = {key: value for key, value in options.items()
              if isinstance(value, dict) and value.get('players') and isinstance(value.get('mean_profit_cents'), int)}
    best = max(played, key=lambda key: played[key]['mean_profit_cents']) if played else None
    leader, total = leader_of(scored)
    return best, leader, total


def results_lines(round_id: str, closed: dict[str, Any], scored: dict[str, Any]) -> list[dict[str, Any]]:
    humans = [row for row in closed.get('players') or [] if isinstance(row, dict) and not row.get('house')]
    picked = sum(1 for row in humans if row.get('option'))
    out = [line(f'close-{round_id}', 'EVENT', f'Close received: {picked}/{len(humans)} picked',
                {'round': round_id, 'players': len(humans), 'picked': picked})]
    unit = next((rnd['market'].get('unit', '') for rnd in getattr(model, 'ROUNDS', ()) if rnd.get('id') == round_id), '')
    bots = [bot['name'] for bot in scored.get('bots') or [] if isinstance(bot, dict) and isinstance(bot.get('name'), str)]
    out.append(line(f'plan-{round_id}', 'GAME',
                    f'Market draw {scored["market"]}{unit}' + (f'; bots: {", ".join(bots)}' if bots else ''),
                    {'round': round_id, 'market': scored['market'], 'bots': bots}))
    best, leader, total = score_parts(round_id, scored)
    parts = ([f'Best option {option_label(round_id, best)}'] if best else []) + (
        [f'leader {leader}' + (f' {total}' if total else '')] if leader else [])
    out.append(line(f'score-{round_id}', 'GAME', '; '.join(parts) or 'Round scored',
                    {'round': round_id, 'best_option': best, 'leader': leader}))
    out.append(line(f'results-{round_id}', 'GAME', 'Results published', {'round': round_id}))
    return out


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


def register_step(run_id: str, arn: str) -> None:
    register(run_id, arn)
    timeline(run_id, lambda: [line('registered', 'DURABLE', 'Execution registered',
                                   {'execution_arn': arn, 'run_id': run_id})])
    say('register-execution', lambda: 'execution registered')


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


def publish_results_step(run_id: str, round_id: str, closed: dict[str, Any], scored: dict[str, Any]) -> None:
    publish_results(run_id, round_id, scored)
    timeline(run_id, lambda: results_lines(round_id, closed, scored))
    say(f'publish-results-{round_id}', lambda: 'results saved')


def publish_final_step(run_id: str, arn: str, last_round: str, scored: dict[str, Any]) -> None:
    publish(run_id, arn, ('closing',), 'final', {'results_round': last_round}, expect_round=last_round)
    timeline(run_id, lambda: [line('final', 'GAME', 'Final results published', {'leader': leader_of(scored)[0]})])
    say('publish-final', lambda: 'final results published')


def publish_open_step(run_id: str, arn: str, round_id: str, previous: dict[str, Any] | None,
                      fields: dict[str, str], callback_id: str) -> None:
    publish(
        run_id, arn, ('starting',) if previous is None else ('closing',), 'open',
        {**fields, 'round': round_id, 'callback_id': callback_id},
        expect_round=None if previous is None else previous['round'],
    )
    timeline(run_id, lambda: [line(f'open-{round_id}', 'GAME', f'{round_title(round_id)} open', {'round': round_id})])
    say(f'publish-open-{round_id}', lambda: 'round open')


def expire_step(run_id: str, arn: str, round_id: str) -> None:
    publish(run_id, arn, ('open', 'closing'), 'expired', {'expired_action': 'close'}, expect_round=round_id)
    say(f'expire-close-{round_id}', lambda: 'close callback timed out, run expired')


def evidence_step(run_id: str, launch_data: dict[str, Any], evidence: dict[str, Any], notes: dict[str, Any]) -> None:
    timeline(run_id, lambda: evidence_lines(launch_data, evidence, notes))
    say('timeline-evidence', lambda: evidence_text(launch_data, evidence))


def plan_step(round_id: str, players: Any, previous: dict[str, Any] | None) -> dict[str, Any]:
    plan = model.plan(round_id, model.validate_players(players), previous, secrets.randbelow)
    say(f'plan-{round_id}', lambda: plan_text(round_id, plan))
    return plan


def score_step(round_id: str, plan: dict[str, Any], previous: dict[str, Any] | None,
               evidence: dict[str, Any]) -> dict[str, Any]:
    scored = model.score(round_id, plan, previous, evidence)
    say(f'score-{round_id}', lambda: score_text(round_id, scored))
    return scored


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


def real_evidence(run_id: str, context: DurableContext) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Launch, then both migrations, then both checks. Any failure falls back to recorded evidence alone.
    The third value names the MicroVMs that really ran, for the timeline only."""
    notes: dict[str, Any] = {'migrate_vm': {}}
    try:
        launch = context.invoke(EVALUATOR, {'id': run_id, 'action': 'launch'}, name='launch-shop')
        check_launch(launch)
        shop = launch.get('launch')
        launch_data = {**(shop if isinstance(shop, dict) else {}), 'source': 'real'}
        notes['launch_vm'] = launch.get('vm_id')
    except Exception as error:  # noqa: BLE001
        print(json.dumps({'fallback': 'launch', 'error': repr(error)}))
        launch_data = {'source': 'recorded'}
    migrations = fork_options(context, run_id, 'migrate', 'migrate', list(MIGRATIONS), check_migrate)
    migrated = [option for option in MIGRATIONS if migrations[option]['ok']]
    notes['migrate_vm'] = {option: migrations[option]['result'].get('vm_id') for option in migrated}
    # A migration that failed has nothing to check, so that option is recorded without a second MicroVM.
    checks = fork_options(context, run_id, 'compare', 'check', migrated, check_compare) if migrated else {}
    evidence = {}
    for option in MIGRATIONS:
        check = checks.get(option)
        if check and check['ok']:
            evidence[option] = {**check['result'], 'source': 'real'}
        else:
            evidence[option] = {**RECORDED[option], 'source': 'recorded'}
    return launch_data, evidence, notes


def wait_for_close(context: DurableContext, run_id: str, arn: str, round_id: str, previous: dict[str, Any] | None,
                   fields: dict[str, str]) -> dict[str, Any]:
    callback = context.create_callback(name=f'await-close-{round_id}', config=CALLBACK)
    context.step(
        lambda _: publish_open_step(run_id, arn, round_id, previous, fields, callback.callback_id),
        name=f'publish-open-{round_id}',
    )
    try:
        value = callback.result()
    except CallbackTimeoutError:
        context.step(lambda _: expire_step(run_id, arn, round_id), name=f'expire-close-{round_id}')
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
    context.step(lambda _: register_step(run_id, arn), name='register-execution')
    # Every MicroVM runs here, once, before Round 1 opens. Nothing after this line creates one.
    launch_data, evidence, notes = real_evidence(run_id, context)
    context.step(lambda _: evidence_step(run_id, launch_data, evidence, notes), name='timeline-evidence')
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
        plan = context.step(lambda _: plan_step(round_id, players, scored), name=f'plan-{round_id}')
        scored = context.step(lambda _: score_step(round_id, plan, scored, evidence), name=f'score-{round_id}')
        context.step(lambda _: publish_results_step(run_id, round_id, closed, scored),
                     name=f'publish-results-{round_id}')
        previous_round = round_id
    context.step(lambda _: publish_final_step(run_id, arn, previous_round, scored), name='publish-final')
    return {'id': run_id, 'execution_arn': arn}
