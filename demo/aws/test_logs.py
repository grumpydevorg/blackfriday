"""The presenter's live log: logs.mjs and logs_web.mjs run for real under node against a stubbed AWS SDK.

No AWS call is made: every `@aws-sdk/*` module is replaced by a stub whose clients hand each command to an
in-memory `aws` object (DynamoDB items, Durable history, CloudWatch events), which also counts the calls.
"""

import json
import re
import socket
import subprocess
from pathlib import Path

import pytest

AWS_DIR = Path(__file__).parent
PAGE_DIR = AWS_DIR / 'logs_page'

PRELUDE = r"""
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';

const STUB = `
export const sdk = () => globalThis.__aws;
${['DynamoDBClient', 'LambdaClient', 'SSMClient', 'CloudWatchLogsClient']
  .map(name => `export class ${name} { send(command, options) { return sdk().send(command, options); } }`).join('\n')}
${['GetItemCommand', 'GetDurableExecutionHistoryCommand', 'FilterLogEventsCommand', 'GetParametersCommand']
  .map(name => `export class ${name} { constructor(input) { this.input = input; } }`).join('\n')}
`;
registerHooks({
  resolve(specifier, context, next) {
    return specifier.startsWith('@aws-sdk/') ? { url: 'stub:aws', shortCircuit: true } : next(specifier, context);
  },
  load(url, context, next) {
    return url === 'stub:aws' ? { format: 'module', source: STUB, shortCircuit: true } : next(url, context);
  },
});

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const s = text => ({ S: text });
const T = Date.now();
const RUN = '0123456789abcdef0123456789abcdef';
const ARN = `arn:aws:lambda:ap-southeast-2:1:function:black-friday-four-workflow:1/durable-execution/${RUN}/11111111-2222-3333-4444-abcdefabcdef`;
const WEB = '/aws/lambda/web/black-friday-four', WORKFLOW = '/aws/lambda/black-friday-four-workflow';
const EVALUATOR = '/aws/lambda/black-friday-four-evaluator', MICROVM = '/aws/lambda-microvms/black-friday-four-shop';
const GROUPS = [WEB, WORKFLOW, EVALUATOR, MICROVM];
const access = (route, status = 200, level = 'info') => JSON.stringify({ bf: 'access', level, method: 'GET', route, status, ms: 7 });

const aws = {
  items: new Map(), history: [], cw: Object.fromEntries(GROUPS.map(group => [group, []])),
  calls: { arena: 0, timeline: 0, history: 0, cw: {}, starts: {} }, cwDone: 0, delay: 0, pageSize: Infinity,
  async send(command) {
    const name = command.constructor.name, input = command.input;
    if (name === 'GetItemCommand') {
      const pk = input.Key.pk.S;
      if (pk === 'arena') aws.calls.arena++; else if (pk.startsWith('timeline#')) aws.calls.timeline++;
      return { Item: aws.items.get(pk) && structuredClone(aws.items.get(pk)) };
    }
    if (name === 'GetDurableExecutionHistoryCommand') {
      aws.calls.history++;
      assert.equal(input.IncludeExecutionData, false);
      assert.equal(input.DurableExecutionArn, ARN);
      return { Events: aws.history };
    }
    if (name === 'FilterLogEventsCommand') {
      const group = input.logGroupName;
      aws.calls.cw[group] = (aws.calls.cw[group] ?? 0) + 1;
      (aws.calls.starts[group] ??= []).push(input.startTime);
      if (aws.delay) await sleep(aws.delay);
      const all = aws.cw[group].filter(event => event.timestamp >= input.startTime);
      const from = Number(input.nextToken ?? 0);
      const events = all.slice(from, from + aws.pageSize);
      aws.cwDone++;
      return { events, ...(from + aws.pageSize < all.length && { nextToken: String(from + aws.pageSize) }) };
    }
    if (name === 'GetParametersCommand') return { Parameters: [{ Name: 'p-key', Value: 'k'.repeat(64) }, { Name: 'p-pass', Value: 'a long passphrase' }] };
    throw new Error(`unexpected ${name}`);
  },
};
globalThis.__aws = aws;

// What the game and the workflow have written: names and token hashes must never reach a client.
const teams = [
  { id: 't1', name: 'Zelda Zebra', token_hash: 'f'.repeat(64), picks: { growth: 'careful' } },
  { id: 't2', name: 'Yuri Yak', token_hash: 'e'.repeat(64), picks: {} },
  { id: 't3', name: 'Xena Xerus', token_hash: 'd'.repeat(64), picks: { growth: 'fast' } },
];
const arena = () => ({
  pk: s('arena'), run_id: s(RUN), phase: s('open'), round: s('growth'), execution_arn: s(ARN), teams_data: s(JSON.stringify(teams)),
});
const timelineItem = () => ({
  pk: s(`timeline#${RUN}`), run_id: s(RUN), lines: { L: [
    { id: 'registered', t: T - 120000, tag: 'DURABLE', text: 'Execution registered', detail: { run_id: RUN, execution_arn: ARN }, log_group: WORKFLOW },
    { id: 'open-growth', t: T - 100000, tag: 'GAME', text: 'Growth open', detail: { round: 'growth' } },
    { id: 'old', t: T - 25 * 60000, tag: 'GAME', text: 'Older than the window' },
  ].map(line => s(JSON.stringify(line))) },
});
const history = () => [
  { EventId: 1, EventType: 'ExecutionStarted', EventTimestamp: new Date(T - 119000) },
  { EventId: 2, EventType: 'StepStarted', SubType: 'Step', Id: 'op1', Name: 'plan', EventTimestamp: new Date(T - 118000) },
  { EventId: 3, EventType: 'StepSucceeded', SubType: 'Step', Id: 'op1', Name: 'plan', EventTimestamp: new Date(T - 116500) },
  { EventId: 4, EventType: 'CallbackStarted', Id: 'op2', Name: 'close-growth', EventTimestamp: new Date(T - 100000) },
  { EventId: 5, EventType: 'ContextStarted', SubType: 'ParallelBranch', Id: 'op3', Name: 'b', EventTimestamp: new Date(T - 99000) },
];
const seed = () => {
  aws.items.set('arena', arena());
  aws.items.set(`timeline#${RUN}`, timelineItem());
  aws.history = history();
};
seed();
const cwEvent = (id, ago, message) => ({ eventId: `e${id}`, timestamp: T - ago, message, logStreamName: 'x', ingestionTime: T });
const fill = () => {
  // Thirty events spread over ten minutes in the web group, plus every other kind of line.
  aws.cw[WEB] = Array.from({ length: 30 }, (_, i) => cwEvent(`w${i}`, 600000 - i * 20000, access(i % 3 ? '/api/join' : '/api/state', 200, i % 3 ? 'info' : 'debug')));
  aws.cw[WORKFLOW] = [cwEvent('f1', 90000, '[WORKFLOW] open-round: callback registered')];
  aws.cw[EVALUATOR] = [cwEvent('v1', 80000, '[EVENT] compare careful: VM running'), cwEvent('v2', 70000, '[PHP] GET /cart 404 12 ms')];
  aws.cw[MICROVM] = [cwEvent('m1', 60000, '[1a2b3c4d migrate careful] PHP shop ready in 1.92 s')];
};
const { createHub, fromCloudWatch, redact, snapshot } = await import('./logs.mjs');
const sdkClients = { db: aws, lambda: aws, logs: aws };
const hubFor = (clock = () => T, extra = {}) => createHub({ ...sdkClients, table: 'runs', groups: GROUPS, workflowFunction: 'black-friday-four-workflow', now: clock, ...extra });
const result = value => console.log('RESULT:' + JSON.stringify(value));

// ---- the real server, for the HTTP scenarios
const PORT = __PORT__;
const serve = async () => {
  Object.assign(process.env, {
    RUN_TABLE: 'runs', WORKFLOW_FUNCTION: 'black-friday-four-workflow', LOG_GROUPS: GROUPS.join(','), POLL_MS: '150',
    AWS_LAMBDA_HTTP_ENDPOINT: `127.0.0.1:${PORT}`, PUBLIC_HOST: 'logs.example.test', PRESENTER_PARAMETER: 'p-key', PASSPHRASE_PARAMETER: 'p-pass',
  });
  await import('./logs_web.mjs');
};
const KEY = { 'x-presenter-key': 'k'.repeat(64) };
const call = async (method, path, { headers = {}, body } = {}) => {
  const response = await fetch(`http://127.0.0.1:${PORT}${path}`, {
    method, redirect: 'manual', headers: { ...(body === undefined ? {} : { 'content-type': 'application/json' }), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch {}
  return { status: response.status, body: parsed, text, headers: response.headers };
};
/** An SSE connection: `until(predicate)` waits on what has arrived so far. */
async function open(path, headers = KEY) {
  const controller = new AbortController();
  const started = Date.now();
  const response = await fetch(`http://127.0.0.1:${PORT}${path}`, { headers, signal: controller.signal });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const stream = { status: response.status, headers: response.headers, text: '', firstAt: undefined, controller };
  let pending;
  const next = () => (pending ??= reader.read().then(chunk => { pending = undefined; return chunk; }));
  stream.until = async (predicate, ms = 5000) => {
    const end = Date.now() + ms;
    while (!predicate(stream.text)) {
      if (Date.now() > end) throw new Error(`timed out; got: ${stream.text.slice(0, 500)}`);
      const chunk = await Promise.race([next(), sleep(50).then(() => null)]);
      if (chunk && chunk.value) { stream.firstAt ??= Date.now() - started; stream.text += decoder.decode(chunk.value, { stream: true }); }
      if (chunk && chunk.done) break;
    }
    return stream;
  };
  stream.messages = () => stream.text.split('\n\n').filter(Boolean).map(block => {
    const message = {};
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) message.comment = line;
      else { const at = line.indexOf(':'); message[line.slice(0, at)] = line.slice(at + 1).trim(); }
    }
    return message;
  });
  stream.events = () => stream.messages().filter(m => m.data && !m.event).map(m => JSON.parse(m.data));
  stream.statuses = () => stream.messages().filter(m => m.event === 'status').length;
  stream.close = () => controller.abort();
  return stream;
}
"""


def run_scenario(source):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    program = PRELUDE.replace('__PORT__', str(port)) + source + '\nprocess.exit(0);\n'
    done = subprocess.run(
        ['node', '--input-type=module', '-e', program], cwd=AWS_DIR, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith('RESULT:')]
    return json.loads(lines[-1][len('RESULT:'):]) if lines else None


# ---------------------------------------------------------------- redaction

def test_redaction_of_links_ids_and_secrets():
    run = '0123456789abcdef0123456789abcdef'
    texts = [
        f'run {run} done',
        'open https://bucket.s3.amazonaws.com/x?X-Amz-Signature=abc&X-Amz-Credential=zzz now',
        'pay at https://pay.example/checkout/42 please',
        'sig https://example.com/a?Signature=deadbeef',
        'receipt=abcdef0123456789 stored',
        '{"token": "s3cr3t-value", "ok": 1}',
        'Cookie: __Host-bf_host=123.abc.def; other=1',
        'x-presenter-key: ' + 'k' * 64,
        'hash ' + 'f' * 64 + ' end',
        'ids a1b2c3d4 and 12345 stay',
    ]
    out = run_scenario('result(' + json.dumps(texts) + '.map(redact));')
    assert out[0] == 'run 01234567… done'
    assert out[1] == 'open [link redacted] now'
    assert out[2] == 'pay at [link redacted] please'
    assert out[3] == 'sig [link redacted]'
    assert out[4] == '[redacted] stored' and 'abcdef0123456789' not in out[4]
    assert 's3cr3t' not in out[5]
    assert 'bf_host' not in out[6] and '123.abc' not in out[6]
    assert 'k' * 64 not in out[7]
    assert out[8] == 'hash ffffffff… end'
    assert out[9] == texts[9]


def test_detail_is_redacted_and_secret_keys_are_dropped():
    out = run_scenario(r"""
      const events = fromCloudWatch(WORKFLOW, cwEvent('d1', 1000,
        JSON.stringify({ level: 'ERROR', message: `boom https://x.test/?X-Amz-Signature=abc run ${RUN}`, token: 'zzz' })));
      result({ text: events.text, raw: events.detail.raw });
    """)
    for value in out.values():
        assert 'X-Amz' not in value and '0123456789abcdef0123456789abcdef' not in value and 'abc' not in value.replace('abcdef', '')


# ---------------------------------------------------------------- each line type

LINES = [
    (r'WEB', json.dumps({'bf': 'access', 'level': 'debug', 'method': 'GET', 'route': '/api/state', 'status': 200, 'ms': 12,
                         'phase': 'open', 'round': 'growth'}),
     ('WEB', 'debug', 'GET /api/state 200 12 ms · open growth')),
    ('WEB', json.dumps({'level': 'INFO', 'message': json.dumps({'bf': 'access', 'level': 'info', 'method': 'POST', 'route': '/api/pick',
                                                                'status': 409, 'ms': 3})}),
     ('WEB', 'info', 'POST /api/pick 409 3 ms')),
    ('WEB', json.dumps({'bf': 'access', 'method': 'GET', 'route': '/x', 'status': 503, 'ms': 3, 'token': 'zzz'}),
     ('WEB', 'error', 'GET /x 503 3 ms')),
    ('WEB', json.dumps({'type': 'platform.invokeSummary', 'record': {'metrics': {'requests': 42}}}),
     ('WEB', 'info', 'environment served 42 requests')),
    ('WEB', json.dumps({'type': 'platform.initStart', 'record': {}}), ('WEB', 'debug', 'platform.initStart')),
    ('WEB', json.dumps({'level': 'ERROR', 'message': 'Arena request failed abc'}), ('ERROR', 'error', 'web: Arena request failed abc')),
    ('WEB', 'plain chatter', None),
    ('WORKFLOW', '[WORKFLOW] open-round: callback registered', ('WORKFLOW', 'info', 'open-round: callback registered')),
    ('WORKFLOW', '[WORKFLOW] close-round: fallback to recorded evidence', ('WORKFLOW', 'warn', 'close-round: fallback to recorded evidence')),
    ('WORKFLOW', json.dumps({'type': 'platform.report', 'record': {'status': 'success', 'metrics': {'durationMs': 1234, 'initDurationMs': 500}}}),
     ('WORKFLOW', 'info', 'Durable invocation 1.23 s (cold start 0.5 s)')),
    ('WORKFLOW', json.dumps({'type': 'platform.report', 'record': {'status': 'failure', 'metrics': {'durationMs': 50}}}),
     ('WORKFLOW', 'error', 'Durable invocation 0.05 s · failure')),
    ('WORKFLOW', json.dumps({'fallback': 'careful', 'error': 'X'}), ('ERROR', 'warn', 'workflow fell back to recorded evidence: careful · X')),
    ('WORKFLOW', json.dumps({'timeline_error': 'boom'}), ('ERROR', 'warn', 'workflow timeline write failed: boom')),
    ('WORKFLOW', 'Traceback (most recent call last)', ('ERROR', 'error', 'workflow: Traceback (most recent call last)')),
    ('WORKFLOW', 'START RequestId: abc Version: $LATEST', None),
    ('EVENT', 'REPORT RequestId: abc\tDuration: 3210.00 ms\tBilled Duration: 3211 ms\tMemory Size: 512 MB\tInit Duration: 1500.00 ms',
     ('EVENT', 'info', 'Event judge done in 3.2 s (cold start 1.5 s)')),
    ('EVENT', 'REPORT RequestId: abc\tDuration: 900.00 ms\tBilled Duration: 901 ms', ('EVENT', 'info', 'Event judge done in 0.9 s')),
    ('EVENT', '[EVENT] compare careful: VM running', ('EVENT', 'info', 'compare careful: VM running')),
    ('EVENT', '[EVENT] launch -: boot failed with code 1', ('EVENT', 'error', 'launch -: boot failed with code 1')),
    ('EVENT', '[VM] PHP shop ready in 1.92 s', ('VM', 'info', 'PHP shop ready in 1.92 s')),
    ('EVENT', '[PHP] GET /cart 404 12 ms', ('PHP', 'warn', 'GET /cart 404 12 ms')),
    ('EVENT', '[PHP] POST /checkout 500 80 ms', ('PHP', 'error', 'POST /checkout 500 80 ms')),
    ('EVENT', '[PHP] GET / 200 4 ms', ('PHP', 'info', 'GET / 200 4 ms')),
    ('EVENT', 'START RequestId: abc Version: $LATEST', None),
    ('EVENT', 'END RequestId: abc', None),
    ('EVENT', 'INIT_START Runtime Version: python', None),
    ('EVENT', 'ValueError: Exception while judging', ('ERROR', 'error', 'event: ValueError: Exception while judging')),
    ('MICROVM', '#12 [internal] load build definition', None),
    ('MICROVM', '[1a2b3c4d migrate careful] PHP shop ready in 1.92 s', ('MICROVM', 'info', '[1a2b3c4d migrate careful] PHP shop ready in 1.92 s')),
    ('MICROVM', 'PHP Fatal error: Uncaught Exception in /app/x.php', ('ERROR', 'error', 'microvm: PHP Fatal error: Uncaught Exception in /app/x.php')),
]
GROUP_NAMES = {'WEB': 'WEB', 'WORKFLOW': 'WORKFLOW', 'EVENT': 'EVALUATOR', 'MICROVM': 'MICROVM'}


def test_every_cloudwatch_line_type_gets_its_tag_level_and_text():
    cases = [{'group': GROUP_NAMES[kind], 'message': message} for kind, message, _ in LINES]
    out = run_scenario(f"""
      const cases = {json.dumps(cases)};
      const groups = {{ WEB, WORKFLOW, EVALUATOR, MICROVM }};
      result(cases.map(({{ group, message }}, index) => {{
        const event = fromCloudWatch(groups[group], cwEvent(index, 1000 * index, message));
        return event && {{ tag: event.tag, level: event.level, text: event.text, id: event.id, ts: event.ts - T, source: event.source }};
      }}));
    """)
    for index, ((_, _, expected), got) in enumerate(zip(LINES, out)):
        if expected is None:
            assert got is None, LINES[index]
            continue
        assert (got['tag'], got['level'], got['text']) == expected, LINES[index]
        assert got['id'] == f'cw:e{index}' and got['ts'] == -1000 * index
    assert out[0]['source'] == 'web/black-friday-four'
    assert out[18]['source'] == 'black-friday-four-evaluator'
    assert out[29]['source'] == 'black-friday-four-shop'


def test_error_lines_are_trimmed_to_200_characters():
    out = run_scenario(r"""
      const event = fromCloudWatch(EVALUATOR, cwEvent(1, 0, 'Exception: ' + 'x'.repeat(900)));
      result({ tag: event.tag, length: event.text.length });
    """)
    assert out == {'tag': 'ERROR', 'length': 200}


# ---------------------------------------------------------------- the hub

def test_timeline_durable_and_status_events_use_ids_taken_from_the_data():
    out = run_scenario(r"""
      fill();
      const hub = hubFor();
      await hub.poll();
      const events = hub.eventsAfter(undefined, true);
      result({
        ids: events.map(event => event.id), status: hub.status,
        pick: Object.fromEntries(events.filter(e => /^(tl|du):/.test(e.id)).map(e => [e.id, [e.tag, e.level, e.text, e.ts - T, e.source]])),
        detail: events.find(e => e.id === `tl:${RUN.slice(0, 8)}:registered`).detail,
        sorted: events.every((e, i) => !i || events[i - 1].ts <= e.ts),
      });
    """)
    assert out['status'] == {'run8': '01234567', 'phase': 'open', 'round': 'growth', 'players': 3, 'picked': 2}
    assert out['pick'] == {
        'tl:01234567:registered': ['DURABLE', 'info', 'Execution registered', -120000, 'black-friday-four-workflow'],
        'tl:01234567:open-growth': ['GAME', 'info', 'Growth open', -100000, 'arena'],
        'du:abcdefabcdef:1': ['DURABLE', 'info', 'execution started', -119000, 'durable'],
        'du:abcdefabcdef:3': ['DURABLE', 'info', 'plan: step done 1.5 s', -116500, 'durable'],
        'du:abcdefabcdef:4': ['DURABLE', 'info', 'close-growth: waiting for the presenter to Close', -100000, 'durable'],
    }, 'the 25-minute-old line is outside the 20-minute window; the parallel branch and StepStarted are skipped'
    assert out['detail']['run_id'] == '01234567…'
    assert out['sorted'] and len(out['ids']) == len(set(out['ids']))
    blob = json.dumps(out)
    for secret in ('Zelda', 'Yuri', 'Xena', 'ffffffff', 'token'):
        assert secret not in blob


def test_two_pollers_on_the_same_data_produce_identical_ids_and_times():
    out = run_scenario(r"""
      fill();
      const first = hubFor(() => T), second = hubFor(() => T + 7000);
      await first.poll(); await second.poll();
      const a = first.eventsAfter(undefined, true), b = second.eventsAfter(undefined, true);
      result({ same: JSON.stringify(a) === JSON.stringify(b), count: a.length });
    """)
    assert out['same'] and out['count'] > 30


def test_backfill_then_steady_state_reads_overlap_and_pages_are_capped():
    out = run_scenario(r"""
      fill();
      aws.pageSize = 3;
      const hub = hubFor(() => T);
      await hub.poll();
      const first = { calls: aws.calls.cw[WEB], start: T - aws.calls.starts[WEB][0],
                      count: hub.eventsAfter(undefined, true).filter(e => e.id.startsWith('cw:ew')).length };
      await hub.poll();
      result({ first, secondStart: T - aws.calls.starts[WEB].at(-1),
               count: hub.eventsAfter(undefined, true).filter(e => e.id.startsWith('cw:ew')).length });
    """)
    # Thirty web events need ten pages of three: the first poll reads five (15 events) from now - 15 min.
    assert out['first'] == {'calls': 5, 'start': 15 * 60000, 'count': 15}
    # The newest event seen was 320 s ago, so the next read starts 60 s before it, and finds ten more.
    assert out['secondStart'] == 380000
    assert out['count'] == 26



def test_debug_events_are_capped_at_the_newest_3000_and_old_events_expire():
    out = run_scenario(r"""
      aws.cw[WEB] = Array.from({ length: 3500 }, (_, i) => cwEvent(`d${i}`, 800000 - i * 100, access('/api/state', 200, 'debug')));
      aws.cw[EVALUATOR] = [cwEvent('late', 900000 - 1, '[EVENT] a b: x')];
      const hub = hubFor(() => T);
      await hub.poll();
      const debug = hub.eventsAfter(undefined, true).filter(e => e.level === 'debug');
      const noDebug = hub.eventsAfter(undefined, false);
      const ids = new Set(debug.map(e => e.id));
      result({ debug: debug.length, noDebug: noDebug.some(e => e.level === 'debug'), hasNewest: ids.has('cw:ed3499'), hasOldest: ids.has('cw:ed0'),
               late: noDebug.some(e => e.id === 'cw:elate') });
    """)
    assert out == {'debug': 3000, 'noDebug': False, 'hasNewest': True, 'hasOldest': False, 'late': True}


def test_resume_overlap_resends_at_or_after_cursor_minus_60s_and_a_client_set_has_no_gaps_or_duplicates():
    out = run_scenario(r"""
      fill();
      const hub = hubFor(() => T);
      await hub.poll();
      const all = hub.eventsAfter(undefined, true);
      const cursor = all[Math.floor(all.length / 2)].ts;
      const resent = hub.eventsAfter(cursor, true);
      const client = new Set(all.filter(e => e.ts <= cursor).map(e => e.id));     // what the first connection delivered
      const duplicates = resent.filter(e => client.has(e.id)).length;
      for (const e of resent) client.add(e.id);
      const expected = all.filter(e => e.ts >= cursor - 60000).map(e => e.id);
      result({
        resentIds: resent.map(e => e.id), expected, duplicatesResent: duplicates,
        gaps: all.filter(e => !client.has(e.id)).length, setSize: client.size, total: all.length,
        stable: JSON.stringify(resent) === JSON.stringify(all.filter(e => e.ts >= cursor - 60000)),
      });
    """)
    assert out['resentIds'] == out['expected'] and out['stable']
    assert out['duplicatesResent'] >= 2, 'the overlap re-sends events the client already has'
    assert out['gaps'] == 0 and out['setSize'] == out['total']


def test_the_poller_stops_when_nobody_is_connected_and_restarts_on_demand():
    out = run_scenario(r"""
      fill();
      const clock = { t: T };
      let fire; let timers = 0;
      const hub = hubFor(() => clock.t, { setTimer: fn => { fire = fn; timers++; return timers; }, clearTimer: () => {} });
      await hub.touch();
      const afterTouch = { polls: hub.polls, active: hub.active };
      clock.t += 11000;                        // /events has been quiet for longer than 10 s
      fire(); await sleep(20);
      const idle = { polls: hub.polls, active: hub.active };
      const release = hub.attach(() => {});    // a connection keeps it going
      await sleep(20);
      clock.t += 11000;
      fire(); await sleep(20);
      const connected = { polls: hub.polls, active: hub.active };
      release();
      result({ afterTouch, idle, connected });
    """)
    assert out['afterTouch'] == {'polls': 1, 'active': True}
    assert out['idle'] == {'polls': 1, 'active': False}
    assert out['connected']['polls'] >= 3 and out['connected']['active'] is True


# ---------------------------------------------------------------- the web function

def test_data_routes_need_the_presenter_and_return_200_with_the_key():
    out = run_scenario(r"""
      fill(); await serve();
      const routes = ['/events', '/api/status', '/viewer', '/logs.js', '/logs.css'];
      const denied = {}, allowed = {};
      for (const path of routes) {
        denied[path] = (await call('GET', path)).status;
        allowed[path] = (await call('GET', path, { headers: KEY })).status;
      }
      const wrong = (await call('GET', '/events', { headers: { 'x-presenter-key': 'nope' } })).status;
      const bad = (await call('GET', '/events', { headers: { cookie: '__Host-bf_host=1.2.3' } })).status;
      const stream = await open('/stream', {});
      const withKey = await open('/stream', KEY);
      await withKey.until(text => text.includes('event: hello'));
      result({ denied, allowed, wrong, bad, streamDenied: stream.status, streamAllowed: withKey.status });
    """)
    assert out['denied'] == {'/events': 403, '/api/status': 403, '/viewer': 303, '/logs.js': 403, '/logs.css': 403}
    assert set(out['allowed'].values()) == {200}
    assert out['wrong'] == 403 and out['bad'] == 403
    assert out['streamDenied'] == 403 and out['streamAllowed'] == 200


def test_signed_out_requests_to_every_other_route_learn_nothing():
    out = run_scenario(r"""
      fill(); await serve();
      const index = (await call('GET', '/')).text;
      const version = path => new RegExp(`${path.replace('.', '\\.')}\\?v=([0-9a-f]{10})`).exec(index)?.[1];
      const paths = ['/viewer', '/logs.js', `/logs.js?v=${'0'.repeat(10)}`, '/logs.css', '/logs.css?v=abc', '/stream', '/stream?debug=1',
                     '/events', '/events?after=0&debug=1', '/api/status', '/nope', '/api/logout'];
      const replies = [];
      for (const path of paths) replies.push([path, 'GET', await call('GET', path)]);
      replies.push(['/api/logout', 'POST', await call('POST', '/api/logout', { headers: { 'x-rehearsal-action': '1' } })]);
      replies.push(['/api/logout', 'POST', await call('POST', '/api/logout')]);
      replies.push(['/api/status', 'POST', await call('POST', '/api/status', { headers: { 'x-rehearsal-action': '1' } })]);
      result(replies.map(([path, method, r]) => ({ path, method, status: r.status, location: r.headers.get('location'), text: r.text })));
    """)
    for reply in out:
        label = f'{reply["method"]} {reply["path"]}'
        assert reply['status'] in (403, 303), label
        if reply['status'] == 303:
            assert reply['path'] == '/viewer' and reply['location'] == '/', label
        for leak in ('durable-execution', 'Zelda', 'black-friday-four', '01234567', 'Execution registered', 'EventSource', 'logs.js'):
            assert leak not in reply['text'], (label, leak)


def test_the_sign_in_page_and_its_assets_are_public_and_carry_no_log_data():
    out = run_scenario(r"""
      fill(); await serve();
      const page = await call('GET', '/');
      const hash = /\/signin\.js\?v=([0-9a-f]{10})/.exec(page.text)?.[1];
      const css = /\/signin\.css\?v=([0-9a-f]{10})/.exec(page.text)?.[1];
      const script = await call('GET', `/signin.js?v=${hash}`), bare = await call('GET', '/signin.js');
      const sheet = await call('GET', `/signin.css?v=${css}`);
      result({ page, hash, css, script: [script.status, script.text, script.headers.get('cache-control'), script.headers.get('content-type')],
               bare: bare.status, sheet: [sheet.status, sheet.text], pageHeaders: Object.fromEntries(page.headers) });
    """)
    assert out['page']['status'] == 200 and out['hash'] and out['css']
    assert out['script'][0] == 200 and out['bare'] == 200 and out['sheet'][0] == 200
    assert 'immutable' in out['script'][2] or 'max-age' in out['script'][2]
    assert out['script'][3].startswith('text/javascript')
    headers = out['pageHeaders']
    assert "connect-src 'self'" in headers['content-security-policy'] and "frame-ancestors 'none'" in headers['content-security-policy']
    assert 'default-src \'self\'' in headers['content-security-policy']
    assert headers['strict-transport-security'].startswith('max-age=')
    assert headers['x-content-type-options'] == 'nosniff'
    blobs = out['page']['text'] + out['script'][1] + out['sheet'][1]
    for leak in ('durable-execution', 'Zelda', 'Yuri', 'black-friday-four', '01234567', 'arn:', 'EventSource', '/stream', '/events'):
        assert leak not in blobs, leak
    assert '/api/login' in out['script'][1]


def test_sign_in_cookie_opens_the_viewer_and_sign_out_closes_it_and_failures_are_throttled():
    out = run_scenario(r"""
      fill(); await serve();
      const wrong = await call('POST', '/api/login', { body: { passphrase: 'not the passphrase' } });
      const ok = await call('POST', '/api/login', { body: { passphrase: 'a long passphrase' } });
      const cookie = ok.headers.get('set-cookie').split(';')[0];
      const flags = ok.headers.get('set-cookie');
      const viewer = await call('GET', '/viewer', { headers: { cookie } });
      const root = await call('GET', '/', { headers: { cookie } });
      const status = await call('GET', '/api/status', { headers: { cookie } });
      const noHeader = await call('POST', '/api/logout', { headers: { cookie } });
      const out = await call('POST', '/api/logout', { headers: { cookie, 'x-rehearsal-action': '1' } });
      const foreign = await call('GET', '/api/status', { headers: { cookie, origin: 'https://evil.example' } });
      let last;
      for (let i = 0; i < 11; i++) last = await call('POST', '/api/login', { body: { passphrase: 'wrong wrong' } });
      result({ wrong: wrong.status, ok: ok.status, flags, viewer: [viewer.status, viewer.text.includes('logs.js?v=')], root: [root.status, root.headers.get('location')],
               status: [status.status, status.body.status], noHeader: noHeader.status, out: [out.status, out.headers.get('set-cookie')], foreign: foreign.status, last: last.status });
    """)
    assert out['wrong'] == 401 and out['ok'] == 200
    assert out['flags'].startswith('__Host-bf_host=') and 'HttpOnly' in out['flags'] and 'Secure' in out['flags']
    assert out['viewer'] == [200, True]
    assert out['root'] == [303, '/viewer']
    assert out['status'][0] == 200 and out['status'][1]['run8'] == '01234567'
    assert out['noHeader'] == 403
    assert out['out'][0] == 200 and 'Max-Age=0' in out['out'][1]
    assert out['foreign'] == 403
    assert out['last'] == 429


def test_status_and_events_never_carry_names_or_tokens():
    out = run_scenario(r"""
      fill(); await serve();
      const events = await call('GET', '/events?debug=1', { headers: KEY });
      const status = await call('GET', '/api/status', { headers: KEY });
      const stream = await open('/stream?debug=1');
      await stream.until(text => text.includes('event: status'));
      result({ events: events.text, status: status.text, stream: stream.text, snapshot: snapshot(arena()) });
    """)
    for blob in (out['events'], out['status'], out['stream']):
        for secret in ('Zelda', 'Yuri', 'Xena', 'token', 'ffffffff', 'teams_data'):
            assert secret not in blob
    assert out['snapshot'] == {'run8': '01234567', 'phase': 'open', 'round': 'growth', 'players': 3, 'picked': 2}
    assert json.loads(out['status'])['status'] == out['snapshot']


def test_events_returns_now_events_and_status_with_the_overlap_rule_and_debug_filter():
    out = run_scenario(r"""
      fill(); await serve();
      const all = (await call('GET', '/events?debug=1', { headers: KEY })).body;
      const quiet = (await call('GET', '/events?debug=0', { headers: KEY })).body;
      const cursor = all.events[Math.floor(all.events.length / 2)].ts;
      const after = (await call('GET', `/events?after=${cursor}&debug=1`, { headers: KEY })).body;
      const reply = await call('GET', '/events?debug=1', { headers: KEY });
      result({
        keys: Object.keys(all), nowClose: Math.abs(all.now - Date.now()) < 5000, status: all.status.phase,
        hasDebug: all.events.some(e => e.level === 'debug'), quietDebug: quiet.events.some(e => e.level === 'debug'),
        afterIds: after.events.map(e => e.id), expected: all.events.filter(e => e.ts >= cursor - 60000).map(e => e.id),
        cache: reply.headers.get('cache-control'),
      });
    """)
    assert out['keys'] == ['now', 'events', 'status'] and out['nowClose'] and out['status'] == 'open'
    assert out['hasDebug'] and not out['quietDebug']
    assert out['afterIds'] == out['expected'] and len(out['afterIds']) > 5
    assert out['cache'] == 'no-store'


def test_sse_sends_hello_before_any_aws_poll_completes_and_has_the_streaming_headers():
    out = run_scenario(r"""
      fill(); aws.delay = 1500; await serve();
      const stream = await open('/stream?debug=1');
      await stream.until(text => text.includes('event: hello'));
      const at = stream.firstAt, done = aws.cwDone;
      const first = stream.text;
      await stream.until(text => text.includes('event: status'), 8000);
      result({ at, done, first, headers: Object.fromEntries(stream.headers), final: stream.text, polled: aws.cwDone });
    """)
    assert out['done'] == 0 and out['at'] < 900
    assert out['first'].startswith('retry: 2000\nevent: hello\ndata: ')
    assert json.loads(out['first'].split('data: ', 1)[1])['now'] > 0
    headers = out['headers']
    assert headers['content-type'] == 'text/event-stream'
    assert headers['cache-control'] == 'no-store' and headers['x-accel-buffering'] == 'no'
    assert "connect-src 'self'" in headers['content-security-policy'] and headers['strict-transport-security']
    assert out['polled'] >= 4, 'the stream waited for the slow poll and then delivered its events'
    assert 'id: ' in out['final'] and '\ndata: {"id":"' in out['final']


def test_sse_events_have_ts_ids_debug_filter_and_resume_from_last_event_id():
    out = run_scenario(r"""
      fill(); await serve();
      const quiet = await open('/stream?debug=0');
      const loud = await open('/stream?debug=1');
      await quiet.until(text => text.includes('event: status'));
      await loud.until(text => text.includes('event: status'));
      const quietEvents = quiet.events(), loudEvents = loud.events();
      const ids = loud.messages().filter(m => m.data && !m.event).map(m => [m.id, JSON.parse(m.data).ts]);
      const cursor = loudEvents[Math.floor(loudEvents.length / 2)].ts;
      const resumed = await open('/stream?debug=1', { ...KEY, 'last-event-id': String(cursor) });
      await resumed.until(text => text.includes('event: status'));
      const client = new Set(loudEvents.filter(e => e.ts <= cursor).map(e => e.id));
      const resent = resumed.events();
      const overlap = resent.filter(e => client.has(e.id)).length;
      resent.forEach(e => client.add(e.id));
      const viaQuery = await open(`/stream?debug=1&after=${cursor}`);
      await viaQuery.until(text => text.includes('event: status'));
      result({
        quietDebug: quietEvents.some(e => e.level === 'debug'), loudDebug: loudEvents.some(e => e.level === 'debug'),
        idsAreTs: ids.every(([id, ts]) => id === String(ts)), same: JSON.stringify(quietEvents.map(e => e.id)) === JSON.stringify(loudEvents.filter(e => e.level !== 'debug').map(e => e.id)),
        resentIds: resent.map(e => e.id), expected: loudEvents.filter(e => e.ts >= cursor - 60000).map(e => e.id), overlap,
        gaps: loudEvents.filter(e => !client.has(e.id)).length, viaQuery: viaQuery.events().map(e => e.id),
      });
    """)
    assert not out['quietDebug'] and out['loudDebug'] and out['idsAreTs'] and out['same']
    assert out['resentIds'] == out['expected'] and out['overlap'] >= 2 and out['gaps'] == 0
    assert out['viaQuery'] == out['expected']


def test_fan_out_many_stream_clients_share_one_set_of_aws_calls_per_poll():
    out = run_scenario(r"""
      fill(); await serve();
      const clients = await Promise.all(Array.from({ length: 8 }, () => open('/stream?debug=1')));
      await Promise.all(clients.map(client => client.until(() => client.statuses() >= 3)));
      const calls = structuredClone(aws.calls);
      const ids = clients.map(client => JSON.stringify([...new Set(client.events().map(e => e.id))].sort()));
      result({ calls: { arena: calls.arena, timeline: calls.timeline, cw: Object.values(calls.cw) , groups: Object.keys(calls.cw).length },
               allSame: new Set(ids).size === 1, count: clients[0].events().length });
    """)
    calls = out['calls']
    assert calls['groups'] == 4
    assert len(set(calls['cw'])) == 1, 'every group is read once per poll'
    polls = calls['cw'][0]
    assert 3 <= polls <= 12, 'one poll per interval, not one per client'
    assert calls['arena'] == polls and calls['timeline'] == polls
    assert out['allSame'] and out['count'] > 30


def test_the_stream_pings_and_keeps_new_events_flowing():
    out = run_scenario(r"""
      fill(); await serve();
      const stream = await open('/stream?debug=1');
      await stream.until(text => text.includes('event: status'));
      const before = stream.events().length;
      aws.cw[EVALUATOR].push(cwEvent('fresh', 0, '[EVENT] judge x: new line'));
      aws.cw[EVALUATOR].at(-1).timestamp = Date.now();
      await stream.until(text => text.includes('judge x: new line'));
      result({ before, after: stream.events().length, fresh: stream.events().filter(e => e.id === 'cw:efresh').length });
    """)
    assert out['after'] == out['before'] + 1 and out['fresh'] == 1


# ---------------------------------------------------------------- the page files

def test_page_has_no_inline_script_or_style_and_builds_dom_with_text_content():
    for name in ('index.html', 'signin.html'):
        html = (PAGE_DIR / name).read_text()
        assert not re.search(r'<script(?![^>]*\bsrc=)', html), name
        assert not re.search(r'<style|\sstyle=|\son[a-z]+=', html), name
    for name in ('logs.js', 'signin.js'):
        script = (PAGE_DIR / name).read_text()
        assert 'innerHTML' not in script and 'insertAdjacentHTML' not in script and 'document.write' not in script, name
        assert 'eval(' not in script
    viewer = (PAGE_DIR / 'logs.js').read_text()
    for needle in ("new EventSource('/stream?debug='", "`/events?", 'HELLO_WAIT_MS = 5000', "'live stream'", "'polling'", '429', 'seen'):
        assert needle in viewer, needle
    html = (PAGE_DIR / 'index.html').read_text()
    assert 'href="/logs.css"' in html and 'src="/logs.js"' in html
    assert 'id="signout"' in html and 'passphrase' not in html


@pytest.mark.parametrize('name', ['index.html', 'logs.js', 'logs.css', 'signin.html', 'signin.js', 'signin.css'])
def test_page_files_exist(name):
    assert (PAGE_DIR / name).is_file()
