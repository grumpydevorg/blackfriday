# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3==1.44.9"]
# ///
"""Live log of the fourround game for the room: game state, Durable operations and Lambda logs.

Read-only: DynamoDB GetItem, Durable execution history, CloudWatch Logs Live Tail.
Run: cd demo && BLACK_FRIDAY_DEPLOYMENT=fourround uv run aws/watch.py | tee -a ~/bf-watch.log

Every event is also appended as one JSON line to ~/bf-watch.jsonl (env BF_WATCH_JSONL overrides),
which aws/watch_web.py serves as a filterable live viewer. Debug events go only to the JSONL.
"""

import json
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

ACCOUNT = '075168677499'   # the only account this may read
REGION = 'ap-southeast-2'
STACK = 'BlackFridayFourRound'
PROFILE = os.environ.get('BLACK_FRIDAY_AWS_PROFILE') or None
ZONE = ZoneInfo('Australia/Adelaide')
WIDTH = 120
LOG_GROUPS = {
    '/aws/lambda/web/black-friday-four': 'WEB',
    '/aws/lambda/web/black-friday-four.local': 'WEB',
    '/aws/lambda/black-friday-four-workflow': 'WORKFLOW',
    '/aws/lambda/black-friday-four-evaluator': 'EVENT',
    '/aws/lambda-microvms/black-friday-four-shop': 'MICROVM',
}
COLOURS = {'GAME': '1;32', 'DURABLE': '1;35', 'EVENT': '1;33', 'MICROVM': '1;36', 'WEB': '1;34',
           'WORKFLOW': '1;95', 'VM': '1;36', 'PHP': '1;96', 'ERROR': '1;31', 'WATCH': '1;37'}
DEFAULT_SOURCE = {'GAME': 'arena', 'MICROVM': 'arena', 'DURABLE': 'durable', 'WEB': 'web'}
HEX32 = re.compile(r'\b[0-9a-fA-F]{32}\b')
SIGNED_URL = re.compile(r'https?://\S*(?:X-Amz-|Signature=|[Pp]ay)\S*')


def scrub(text) -> str:
    """Last line of defence: no pre-signed or payment links, no full 32-hex run id (8-char prefix stays)."""
    return HEX32.sub(lambda m: m.group(0)[:8] + '…', SIGNED_URL.sub('[link redacted]', str(text)))


SSO_LINE = 'AWS SSO expired: run aws sso login --sso-session rick-personal --use-device-code'
CREDENTIAL_CODES = {'ExpiredToken', 'ExpiredTokenException', 'UnrecognizedClientException',
                    'InvalidClientTokenId', 'UnauthorizedException', 'AccessDeniedException'}

ROUNDS = json.loads((Path(__file__).resolve().parent / 'rounds.json').read_text())['rounds']
ROUND = {r['id']: r for r in ROUNDS}
OPTION = {(r['id'], o['id']): o['label'] for r in ROUNDS for o in r['options']}
_print_lock = threading.Lock()


def short_title(round_id: str | None) -> str:
    if not round_id:
        return '-'
    title = ROUND.get(round_id, {}).get('title', round_id)
    return title.split('·')[-1].strip()


def jsonl_path() -> Path:
    return Path(os.environ.get('BF_WATCH_JSONL') or Path.home() / 'bf-watch.jsonl')


def event_record(tag: str, level: str, source: str, text: str, detail: dict | None = None,
                 now: datetime | None = None) -> dict:
    """The JSONL shape: one dict per event, timestamps in UTC."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {'ts': stamp.isoformat(timespec='milliseconds').replace('+00:00', 'Z'), 'tag': tag, 'level': level,
            'source': source, 'text': ' '.join(str(text).split()), 'detail': detail or {}}


def write_jsonl(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
    try:
        with _print_lock, open(jsonl_path(), 'a', encoding='utf-8') as handle:
            handle.write(line)
    except OSError as error:   # a full disk must not stop the room screen
        print(f'watch: cannot write {jsonl_path()}: {error}', file=sys.stderr, flush=True)


def emit(tag: str, text: str, colour: str | None = None, *, level: str | None = None,
         source: str | None = None, detail: dict | None = None, public: str | None = None) -> None:
    """One event. Stdout: local time, fixed-width coloured tag, text cut to fit WIDTH (not for debug).
    JSONL: always, with `public` as the text when the screen line shows more than the file may (names)."""
    level = level or ('error' if (colour or tag) == 'ERROR' else 'info')
    text = scrub(text)
    write_jsonl(event_record(tag, level, source or DEFAULT_SOURCE.get(tag, 'watch'),
                             text if public is None else scrub(public), detail))
    # The room screen stays as it was: polls, and routine per-request WEB access lines (100 phones joining and
    # picking), go to the JSONL viewer only; a 4xx/5xx access line still shows.
    if level == 'debug' or (tag == 'WEB' and detail and 'route' in detail and level == 'info'):
        return
    stamp = datetime.now(ZONE).strftime('%H:%M:%S')
    label = f'[{tag}]'.ljust(10)
    text = ' '.join(str(text).split())
    room = WIDTH - len(stamp) - 1 - len(label) - 1
    if len(text) > room:
        text = text[:room - 1] + '…'
    code = COLOURS.get(colour or tag, '0')
    with _print_lock:
        print(f'{stamp} \x1b[{code}m{label}\x1b[0m {text}', flush=True)


def dollars(cents) -> str:
    try:
        value = int(cents) / 100
    except (TypeError, ValueError):
        return '?'
    return f'-${-value:,.0f}' if value < 0 else f'${value:,.0f}'


def is_credential_error(error: Exception) -> bool:
    if isinstance(error, (SSOTokenLoadError, TokenRetrievalError, UnauthorizedSSOTokenError)):
        return True
    if isinstance(error, ClientError):
        return error.response.get('Error', {}).get('Code') in CREDENTIAL_CODES
    return 'expired' in str(error).lower() and 'token' in str(error).lower()


class Aws:
    """Clients that are rebuilt after an SSO outage; one red line per outage."""

    def __init__(self):
        self.lock = threading.Lock()
        self.outage = False
        self.session = None

    def client(self, name: str):
        with self.lock:
            if self.session is None:
                self.session = boto3.Session(profile_name=PROFILE, region_name=REGION)
            return self.session.client(name)

    def failed(self, error: Exception, where: str) -> float:
        """Report a failure; returns seconds to wait before retrying."""
        if is_credential_error(error):
            with self.lock:
                first = not self.outage
                self.outage = True
                self.session = None
            if first:
                emit('ERROR', SSO_LINE)
            return 30
        emit('ERROR', f'{where}: {type(error).__name__}: {error}')
        return 5

    def ok(self) -> None:
        if self.outage:
            with self.lock:
                if self.outage:
                    self.outage = False
                    emit('WATCH', 'AWS credentials work again')


AWS = Aws()


def check_account() -> str:
    while True:
        try:
            account = AWS.client('sts').get_caller_identity()['Account']
            break
        except Exception as error:  # noqa: BLE001
            if not is_credential_error(error):
                raise
            time.sleep(AWS.failed(error, 'sts'))
    if account != ACCOUNT:
        raise SystemExit(f'Refusing to run: caller is account {account}, not {ACCOUNT}')
    AWS.ok()
    return account


def table_name() -> str:
    while True:
        try:
            outputs = AWS.client('cloudformation').describe_stacks(StackName=STACK)['Stacks'][0]['Outputs']
            AWS.ok()
            return next(item['OutputValue'] for item in outputs if item['OutputKey'] == 'RunTable')
        except Exception as error:  # noqa: BLE001
            time.sleep(AWS.failed(error, 'describe-stacks'))


# ---------------------------------------------------------------- [GAME]

class Shared:
    """What the game poller has seen that the Durable poller needs."""
    execution_arn: str | None = None


def text_attr(item: dict, name: str) -> str | None:
    value = item.get(name)
    return value.get('S') if value else None


def roster(item: dict) -> list[dict]:
    try:
        players = json.loads(text_attr(item, 'teams_data') or '[]')
    except ValueError:
        return []
    # Keep only what the log may show: never token_hash.
    return [{'id': p.get('id'), 'name': p.get('name', '?'), 'picks': p.get('picks') or {}}
            for p in players if isinstance(p, dict)]


def plural(count: int, noun: str) -> str:
    return f'{count} {noun}' + ('' if count == 1 else 's')


def pick_summary(players: list[dict], round_id: str) -> str:
    counts: dict[str, int] = {}
    for player in players:
        option = player['picks'].get(round_id)
        if option:
            counts[option] = counts.get(option, 0) + 1
    split = ' · '.join(f'{OPTION.get((round_id, k), k)} {v}' for k, v in counts.items())
    return f'{sum(counts.values())}/{len(players)} picked  {split}'.rstrip()


def phase_text(phase: str | None, round_id: str | None) -> str:
    if phase in ('open', 'closing'):
        return f'{phase} {short_title(round_id)}'
    return phase or '?'


def evidence_lines(launch: dict | None, evidence: dict | None) -> list[str]:
    lines = []
    if launch is not None:
        if launch.get('source') == 'real':
            statuses = launch.get('checkout_statuses') or []
            lines.append(f'Launch shop (real VM): checkout {"→".join(map(str, statuses)) or "?"}, '
                         f'{launch.get("charges", "?")} charge(s), cart {launch.get("cart_status", "?")}, '
                         f'order {launch.get("order_status", "?")}')
        else:
            lines.append('Launch shop: recorded evidence (the real VM run did not complete)')
    for option, kind in (('careful', 'preserve'), ('fast', 'omit')):
        data = (evidence or {}).get(option)
        if not data:
            continue
        source = data.get('source', 'real')
        kept = 'cart kept' if data.get('cart_kept') else 'cart LOST'
        extra = f', checkout {data.get("checkout_status")} in {data.get("checkout_ms")} ms' \
            if data.get('checkout_status') else ''
        vm = f', {data.get("vm_id")} {data.get("vm_state")}' if data.get('vm_id') else ''
        lines.append(f'{OPTION.get(("growth", option), option)} ({kind}, {source}): {kept} '
                     f'({data.get("cart_status", "?")}){extra}{vm}')
    return lines


def results_line(data: dict, names: bool = True) -> str:
    round_id = data.get('round')
    market = data.get('market')
    unit = ROUND.get(round_id, {}).get('market', {}).get('unit', '')
    options = (data.get('summary') or {}).get('options') or {}
    used = [(k, v) for k, v in options.items() if v.get('players')]
    best = max(used, key=lambda kv: kv[1].get('mean_profit_cents', 0), default=None)
    best_text = (f'best {OPTION.get((round_id, best[0]), best[0])} (mean {dollars(best[1].get("mean_profit_cents"))}, '
                 f'{best[1].get("players")} players)') if best else 'no picks'
    rows = data.get('rows') or []
    top = (f'top {rows[0].get("name") if names else "player"} {dollars(rows[0].get("cumulative_cents"))}'
           if rows else '')
    return f'{short_title(round_id)} results: market {market}{unit} · {best_text} · {top}'


def watch_game(table: str) -> None:
    db = None
    last: dict = {}
    last_change = time.monotonic()
    shown_results: set[str] = set()
    shown_evidence: set[str] = set()
    picks_seen: tuple | None = None
    picks_printed_at = 0.0
    while True:
        try:
            db = db or AWS.client('dynamodb')
            item = db.get_item(TableName=table, Key={'pk': {'S': 'arena'}}, ConsistentRead=True).get('Item') or {}
            AWS.ok()
        except Exception as error:  # noqa: BLE001
            db = None
            time.sleep(AWS.failed(error, 'arena GetItem'))
            continue
        run_id, phase = text_attr(item, 'run_id'), text_attr(item, 'phase')
        round_id, results_round = text_attr(item, 'round'), text_attr(item, 'results_round')
        players = roster(item)
        now = time.monotonic()
        if not last:
            emit('GAME', f'watching run {(run_id or "?")[:8]} · {phase_text(phase, round_id)} · {len(players)} players')
        elif run_id != last['run_id']:
            emit('GAME', f'RESET → new run {(run_id or "?")[:8]} · {phase_text(phase, round_id)} · {len(players)} players')
            shown_results.clear()
            shown_evidence.clear()
            last_change = now
        elif (phase, round_id) != (last['phase'], last['round']):
            if last['phase'] == 'open' and last['round']:
                # Picks are frozen at Close; the throttled count above may not have shown the last ones.
                emit('GAME', f'{short_title(last["round"])} closed: {pick_summary(players, last["round"])}')
            emit('GAME', f'{phase_text(last["phase"], last["round"])} → {phase_text(phase, round_id).upper()} '
                         f'(after {now - last_change:.1f} s)')
            last_change = now
        Shared.execution_arn = text_attr(item, 'execution_arn')

        if last and run_id == last['run_id']:
            before = {p['id'] for p in last['players']}
            after = {p['id'] for p in players}
            joined = [p['name'] for p in players if p['id'] not in before]
            left = len(before - after)
            if joined:
                names = ', '.join(joined[:3]) + (f' +{len(joined) - 3} more' if len(joined) > 3 else '')
                emit('GAME', f'+{len(joined)} joined ({plural(len(players), "player")}): {names}',
                     public=f'+{len(joined)} joined ({plural(len(players), "player")})')
            if left:
                emit('GAME', f'-{left} removed ({plural(len(players), "player")})')

        if phase == 'open' and round_id:
            picked = sum(1 for p in players if round_id in p['picks'])
            state = (run_id, round_id, picked, len(players))
            if state != picks_seen and (now - picks_printed_at >= 3 or picked == len(players)):
                emit('GAME', f'{short_title(round_id)}: {pick_summary(players, round_id)}')
                picks_printed_at = now
                picks_seen = state

        launch_raw, evidence_raw = text_attr(item, 'launch_data'), text_attr(item, 'evidence_data')
        if launch_raw and evidence_raw and run_id not in shown_evidence:
            shown_evidence.add(run_id)
            try:
                for line in evidence_lines(json.loads(launch_raw), json.loads(evidence_raw)):
                    emit('MICROVM', line)
            except ValueError:
                pass

        for done in ([results_round] if results_round else []):
            key = f'results#{run_id}#{done}'
            if key in shown_results:
                continue
            try:
                result = db.get_item(TableName=table, Key={'pk': {'S': key}}, ConsistentRead=True).get('Item')
            except Exception as error:  # noqa: BLE001
                time.sleep(AWS.failed(error, 'results GetItem'))
                continue
            if result:
                shown_results.add(key)
                try:
                    data = json.loads(result['data']['S'])
                    emit('GAME', results_line(data), public=results_line(data, names=False))
                except (KeyError, ValueError):
                    emit('GAME', f'{short_title(done)} results published')
        last = {'run_id': run_id, 'phase': phase, 'round': round_id, 'players': players}
        time.sleep(1)


# ---------------------------------------------------------------- [DURABLE]

SKIP_STARTED = {'StepStarted', 'ContextStarted'}   # instantaneous; the matching success line says it all


def durable_line(event: dict, started: dict) -> tuple[str, str | None] | None:
    kind, name, sub = event['EventType'], event.get('Name') or '', event.get('SubType') or ''
    at = event['EventTimestamp'].timestamp()
    op = event.get('Id')
    elapsed = f' {at - started[op]:.1f} s' if op in started else ''
    if kind.endswith('Started') and op:
        started[op] = at
    if sub == 'ParallelBranch':
        return None
    if kind == 'ExecutionStarted':
        return 'execution started', None
    if kind == 'ExecutionSucceeded':
        return 'execution finished: game over', None
    if kind in ('ExecutionFailed', 'ExecutionTimedOut', 'ExecutionStopped'):
        return f'execution {kind[9:].lower()}', 'ERROR'
    if kind == 'InvocationCompleted':
        return 'invocation ended · workflow suspended; the next one replays from checkpoints', None
    if kind == 'ContextStarted' and sub == 'Parallel':
        return f'{name}: started in parallel', None
    if kind in SKIP_STARTED:
        return None
    if kind == 'ChainedInvokeStarted':
        return f'{name}: invoke Event judge (MicroVM)', None
    if kind == 'CallbackStarted':
        return f'{name}: waiting for the presenter to Close', None
    if kind == 'CallbackSucceeded':
        return f'{name}: Close received after{elapsed}', None
    if kind.endswith('Succeeded'):
        label = {'Step': 'step', 'ChainedInvoke': 'invoke', 'Parallel': 'parallel'}.get(sub, sub.lower() or 'op')
        return f'{name}: {label} done{elapsed if elapsed.strip() not in ("0.0 s",) else ""}', None
    if kind.endswith(('Failed', 'TimedOut', 'Stopped')):
        return f'{name}: {sub or "op"} {kind}{elapsed}', 'ERROR'
    return f'{name or "-"}: {kind}', None


def watch_durable() -> None:
    lam = None
    arn = None
    seen: set[str] = set()
    started: dict[str, float] = {}
    while True:
        current = Shared.execution_arn
        if current != arn:
            arn, seen, started = current, set(), {}
            first = True
            if arn:
                emit('DURABLE', f'execution …{arn[-36:]}')
        if not arn:
            time.sleep(2)
            continue
        try:
            lam = lam or AWS.client('lambda')
            events, marker = [], None
            while True:
                page = lam.get_durable_execution_history(
                    DurableExecutionArn=arn, IncludeExecutionData=False, MaxItems=1000,
                    **({'Marker': marker} if marker else {}))
                events += page['Events']
                marker = page.get('NextMarker')
                if not marker:
                    break
            AWS.ok()
        except Exception as error:  # noqa: BLE001
            lam = None
            time.sleep(AWS.failed(error, 'durable history'))
            continue
        events.sort(key=lambda e: (e['EventTimestamp'], int(e.get('EventId') or 0)))
        fresh = [e for e in events if str(e.get('EventId')) not in seen]
        if first and len(fresh) > 6:
            # Joining mid-game: summarise the past instead of replaying it on screen.
            for event in fresh[:-3]:
                seen.add(str(event.get('EventId')))
                if event['EventType'].endswith('Started') and event.get('Id'):
                    started[event['Id']] = event['EventTimestamp'].timestamp()
            last_named = next((e.get('Name') for e in reversed(fresh[:-3]) if e.get('Name')), '-')
            emit('DURABLE', f'{len(fresh) - 3} earlier events (last: {last_named})')
            fresh = fresh[-3:]
        first = False
        for event in fresh:
            seen.add(str(event.get('EventId')))
            line = durable_line(event, started)
            if line:
                emit('DURABLE', line[0], line[1])
        time.sleep(2)


# ---------------------------------------------------------------- Live Tail

REPORT = re.compile(r'REPORT RequestId: \S+\s+Duration: ([\d.]+) ms.*?(?:Init Duration: ([\d.]+) ms)?\s*$')
ERRORISH = re.compile(r'\b(ERROR|Error|Exception|Traceback|FAILED|Task timed out)\b')


class WebStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.requests = 0
        self.instances: set[str] = set()
        self.concurrency = 0
        self.cold = 0
        self.errors = 0
        self.since = time.monotonic()

    def flush(self):
        with self.lock:
            if self.requests or self.cold or self.errors:
                minutes = max((time.monotonic() - self.since) / 60, 1 / 60)
                emit('WEB', f'{self.requests / minutes:,.0f} req/min · {len(self.instances)} environment(s) · '
                            f'max {self.concurrency} concurrent · {self.cold} cold start(s) · {self.errors} error(s)',
                     'ERROR' if self.errors else None, source='web',
                     detail={'requests': self.requests, 'environments': len(self.instances),
                             'max_concurrent': self.concurrency, 'cold_starts': self.cold, 'errors': self.errors})
            self.reset()


WEB = WebStats()


CONTRACT = re.compile(r'(?:^|\s)\[(EVENT|VM|PHP|WORKFLOW)\] ?(.*)$', re.S)
PHP_ACCESS = re.compile(r'^([A-Z]+) (\S+) (\d{3}) ([\d.]+) ms\b')
STAGE_LINE = re.compile(r'^(\S+) (\S+): ')
STEP_LINE = re.compile(r'^([^\s:]+): ')
WARNISH = re.compile(r'\b(WARN|WARNING|warn|warning|retry|retrying|fallback)\b')
ACCESS_KEYS = ('method', 'route', 'status', 'ms', 'phase', 'round')   # the only fields that may leave the log line
ACCESS_IN_TEXT = re.compile(r'\{[^{}]*"bf"\s*:\s*"access"[^{}]*\}')
LEVEL_NAMES = {'DEBUG': 'debug', 'INFO': 'info', 'WARN': 'warn', 'WARNING': 'warn', 'ERROR': 'error',
               'CRITICAL': 'error', 'FATAL': 'error'}


def access_level(status, given=None) -> str:
    if isinstance(given, str) and given.lower() in LEVEL_NAMES.values():
        return given.lower()
    status = status if isinstance(status, int) else 0
    return 'error' if status >= 500 else 'warn' if status >= 400 else 'info'


def parse_access(message, depth: int = 0) -> dict | None:
    """The web access record in a log message, or None. It may be the whole message, nested in the `message` or
    `record` field of a JSON log record, or embedded in a plain console line. Only ACCESS_KEYS are kept."""
    found = None
    if isinstance(message, str):
        text = message.strip()
        if text.startswith('{'):
            try:
                found = parse_access(json.loads(text), depth + 1) if depth < 3 else None
            except ValueError:
                found = None
        if found is None:
            match = ACCESS_IN_TEXT.search(text)
            if match:
                try:
                    found = parse_access(json.loads(match.group(0)), depth + 1)
                except ValueError:
                    found = None
        return found
    if not isinstance(message, dict) or depth > 3:
        return None
    if message.get('bf') == 'access':
        out = {key: message[key] for key in ACCESS_KEYS if isinstance(message.get(key), (str, int, float))}
        out['level'] = access_level(out.get('status'), message.get('level'))
        return out
    for key in ('message', 'record'):
        found = parse_access(message.get(key), depth + 1)
        if found:
            return found
    return None


def access_text(access: dict) -> str:
    text = f'{access.get("method", "?")} {access.get("route", "?")} {access.get("status", "?")} {access.get("ms", "?")} ms'
    extra = ' '.join(str(access[k]) for k in ('phase', 'round') if access.get(k))
    return f'{text} · {extra}' if extra else text


def contract_level(tag: str, text: str, hint: str | None = None) -> str:
    if tag == 'PHP':
        match = PHP_ACCESS.match(text)
        if match:
            status = int(match.group(3))
            return 'error' if status >= 500 else 'warn' if status >= 400 else 'info'
    if hint:
        return hint
    if ERRORISH.search(text) or ' failed' in text:
        return 'error'
    return 'warn' if WARNISH.search(text) else 'info'


def parse_contract(message: str) -> dict | None:
    """`[EVENT] ...`, `[VM] ...`, `[PHP] ...` or `[WORKFLOW] ...` (also inside a JSON record's `message`).
    Returns {'tag', 'level', 'text', 'detail'} or None."""
    hint = None
    message = message.strip()
    if message.startswith('{'):
        try:
            record = json.loads(message)
        except ValueError:
            record = None
        if isinstance(record, dict) and isinstance(record.get('message'), str):
            message = record['message'].strip()
            hint = LEVEL_NAMES.get(str(record.get('level', '')).upper())
    match = CONTRACT.search(message)
    if not match:
        return None
    tag, text = match.group(1), ' '.join(match.group(2).split())
    detail: dict = {}
    if tag == 'PHP':
        php = PHP_ACCESS.match(text)
        if php:
            detail = {'method': php.group(1), 'path': php.group(2), 'status': int(php.group(3)),
                      'ms': float(php.group(4))}
    elif tag == 'EVENT':
        stage = STAGE_LINE.match(text)
        if stage:
            detail = {'stage': stage.group(1), 'option': None if stage.group(2) == '-' else stage.group(2)}
    elif tag == 'WORKFLOW':
        step = STEP_LINE.match(text)
        if step:
            detail = {'step': step.group(1)}
    return {'tag': tag, 'level': contract_level(tag, text, hint), 'text': text, 'detail': detail}


def handle_access(access: dict, source: str) -> None:
    level = access['level']
    with WEB.lock:
        if level == 'error':
            WEB.errors += 1
    emit('WEB', access_text(access), 'ERROR' if level == 'error' else None, level=level, source=source,
         detail=access)


def handle_log(tag: str, message: str, source: str | None = None) -> None:
    message = message.strip()
    if not message:
        return
    source = source or tag.lower()
    if tag == 'WEB':
        access = parse_access(message)
        if access:
            handle_access(access, source)
            return
    else:
        line = parse_contract(message)
        if line:
            emit(line['tag'], line['text'], 'ERROR' if line['level'] == 'error' else None, level=line['level'],
                 source=source, detail=line['detail'])
            return
    record = None
    if message.startswith('{'):
        try:
            record = json.loads(message)
        except ValueError:
            record = None
    if tag == 'WEB' and isinstance(record, dict):
        kind, body = record.get('type', ''), record.get('record') or {}
        with WEB.lock:
            if kind == 'platform.invokeSummary':
                metrics = body.get('metrics') or {}
                WEB.requests += int(metrics.get('requests') or 0)
                WEB.concurrency = max(WEB.concurrency, int(metrics.get('concurrentRequests') or 0))
                WEB.instances.add(body.get('instanceId', '?'))
            elif kind == 'platform.initStart':
                WEB.cold += 1
            elif kind in ('platform.invokeReport', 'platform.initReport') and body.get('status') not in (None, 'success'):
                WEB.errors += 1
        if record.get('level') == 'ERROR' or ERRORISH.search(str(record.get('message', ''))):
            WEB.errors += 1
            emit('ERROR', f'web: {record.get("message") or message}', source=source)
        return
    if tag == 'WORKFLOW' and isinstance(record, dict):
        if record.get('type') == 'platform.report':
            metrics = (record.get('record') or {}).get('metrics') or {}
            cold = f' (cold start {metrics["initDurationMs"] / 1000:.1f} s)' if metrics.get('initDurationMs') else ''
            status = (record.get('record') or {}).get('status')
            emit('WORKFLOW', f'Durable invocation {metrics.get("durationMs", 0) / 1000:.2f} s{cold}'
                             + ('' if status == 'success' else f' · {status}'), None if status == 'success' else 'ERROR',
                 source=source)
        elif record.get('level') in ('ERROR', 'CRITICAL') or ERRORISH.search(str(record.get('message', ''))):
            emit('ERROR', f'workflow: {record.get("message") or message}', source=source)
        elif 'fallback' in record:
            emit('ERROR', f'workflow fell back to recorded evidence: {record.get("fallback")} · {record.get("error")}',
                 source=source)
        return
    if tag == 'EVENT':
        match = REPORT.search(message)
        if match:
            cold = f' (cold start {float(match.group(2)) / 1000:.1f} s)' if match.group(2) else ''
            emit('EVENT', f'Event judge done in {float(match.group(1)) / 1000:.1f} s{cold}', source=source)
        elif message.startswith(('START', 'END', 'INIT_START')):
            return
        elif ERRORISH.search(message):
            emit('ERROR', f'event: {message}', source=source)
        else:
            emit('EVENT', message, source=source)
        return
    if tag == 'MICROVM':
        if isinstance(record, dict):   # a structured wrapper: show the line it carries
            message = str(record.get('message') or record.get('log') or message).strip()
        if message.startswith('#') and not ERRORISH.search(message):
            return   # image build output
        # vm_runtime.py lines look like "[1a2b3c4d migrate careful] PHP shop ready in 1.92 s".
        bad = ERRORISH.search(message) or ' failed with code ' in message or 'php: PHP ' in message
        emit('ERROR' if bad else 'MICROVM', message, source=source)
        return
    if ERRORISH.search(message):
        emit('ERROR', f'{tag.lower()}: {message}', source=source)


def log_source(identifier: str) -> str:
    """Short log group name: /aws/lambda/web/black-friday-four → web/black-friday-four. Live Tail reports the
    group as an ARN or as '<account>:<group>'."""
    group = identifier.split('log-group:', 1)[-1].removesuffix(':*')
    group = re.sub(r'^\d{12}:', '', group)
    return group.removeprefix('/aws/lambda-microvms/').removeprefix('/aws/lambda/') or 'logs'


def watch_logs(account: str) -> None:
    identifiers = [f'arn:aws:logs:{REGION}:{account}:log-group:{group}' for group in LOG_GROUPS]
    tags = {identifier: LOG_GROUPS[group] for identifier, group in zip(identifiers, LOG_GROUPS)}
    recent: deque = deque(maxlen=4000)
    recent_set: set = set()
    while True:
        try:
            stream = AWS.client('logs').start_live_tail(logGroupIdentifiers=identifiers)['responseStream']
            for event in stream:
                if 'sessionStart' in event:
                    AWS.ok()
                    emit('WATCH', 'CloudWatch Live Tail connected (5 log groups)')
                    continue
                for result in (event.get('sessionUpdate') or {}).get('sessionResults') or []:
                    identifier = result.get('logGroupIdentifier', '')
                    tag = tags.get(identifier) or next(
                        (t for i, t in tags.items() if identifier.endswith(i.rsplit(':', 1)[-1])), 'WEB')
                    # The web function writes the same records to its .local group: show each once.
                    key = (tag, result.get('timestamp'), hash(result.get('message')))
                    if key in recent_set:
                        continue
                    if len(recent) == recent.maxlen:
                        recent_set.discard(recent[0])
                    recent.append(key)
                    recent_set.add(key)
                    handle_log(tag, result.get('message') or '', log_source(identifier))
            emit('WATCH', 'Live Tail session ended; reconnecting')
        except (ClientError, BotoCoreError) as error:
            time.sleep(min(AWS.failed(error, 'live tail'), 30))
        except Exception as error:  # noqa: BLE001
            emit('ERROR', f'live tail dropped: {type(error).__name__}: {error}')
            time.sleep(5)


# ---------------------------------------------------------------- main

def main() -> None:
    if os.environ.get('BLACK_FRIDAY_DEPLOYMENT', 'fourround') != 'fourround':
        raise SystemExit('watch.py only watches BLACK_FRIDAY_DEPLOYMENT=fourround')
    account = check_account()
    table = table_name()
    emit('WATCH', f'BLACK FRIDAY live log · account {account} · {REGION} · table {table}')
    for target, args in ((watch_game, (table,)), (watch_durable, ()), (watch_logs, (account,))):
        threading.Thread(target=target, args=args, daemon=True).start()
    try:
        while True:
            time.sleep(60)
            WEB.flush()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == '__main__':
    main()
