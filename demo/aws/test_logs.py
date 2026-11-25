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
${['GetItemCommand', 'GetDurableExecutionHistoryCommand', 'FilterLogEventsCommand', 'StartLiveTailCommand', 'GetParametersCommand']
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
  calls: { arena: 0, timeline: 0, history: 0, cw: {}, starts: {} }, cwDone: 0, delay: 0, pageSize: Infinity, tails: [],
  async send(command, options) {
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
    if (name === 'StartLiveTailCommand') {
      // A Live Tail session: an async iterable of sessionStart then sessionUpdate; the test pushes, ends or fails it.
      const queue = [];
      let wake, ended = false, error;
      const tail = {
        input, aborted: false,
        push: results => { queue.push({ sessionUpdate: { sessionResults: results } }); wake?.(); },
        end: () => { ended = true; wake?.(); },
        fail: failure => { error = failure; wake?.(); },
      };
      options?.abortSignal?.addEventListener('abort', () => { tail.aborted = true; error = Object.assign(new Error('aborted'), { name: 'AbortError' }); wake?.(); });
      aws.tails.push(tail);
      return { responseStream: (async function* () {
        yield { sessionStart: {} };
        for (;;) {
          if (error) throw error;
          if (queue.length) { yield queue.shift(); continue; }
          if (ended) return;
          await new Promise(resolve => { wake = resolve; });
        }
      })() };
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
const tl = (id, ago, tag, level, text, detail, log_group) => ({ id, t: T - ago, tag, level, text, ...(detail && { detail }), ...(log_group && { log_group }) });
const timelineLines = () => [
  // What the workflow writes: its decisions are DURABLE, evidence that really ran in a VM is MICROVM, recorded evidence DURABLE warn.
  tl('registered', 120000, 'DURABLE', 'info', 'Execution registered', { run_id: RUN, execution_arn: ARN }, WORKFLOW),
  tl('open-growth', 100000, 'DURABLE', 'info', 'Round 2 · Growth open', { round: 'growth' }, WORKFLOW),
  tl('close-growth', 95000, 'DURABLE', 'info', 'Close received: 2/3 picked', { players: 3, picked: 2, round: 'growth' }, WORKFLOW),
  tl('plan-growth', 94000, 'DURABLE', 'info', 'Market draw 105%', { market: 105, bots: 3, round: 'growth' }, WORKFLOW),
  tl('score-growth', 93000, 'DURABLE', 'info', 'Round scored', { round: 'growth' }, WORKFLOW),
  tl('results-growth', 92000, 'DURABLE', 'info', 'Results published', { round: 'growth' }, WORKFLOW),
  tl('expire-christmas', 91500, 'DURABLE', 'warn', 'Close never arrived: the run expired', { round: 'christmas' }, WORKFLOW),
  tl('final', 91000, 'DURABLE', 'info', 'Final results published', undefined, WORKFLOW),
  tl('launch', 110000, 'MICROVM', 'info', 'Launch shop (real MicroVM): checkout 200', { charges: 1, checkout_status: 200, vm_id: 'vm-launch', source: 'real' }, EVALUATOR),
  tl('migrate-careful', 109000, 'MICROVM', 'info', 'Careful migration done (real MicroVM)', { option: 'careful', vm_id: 'vm-careful', source: 'real' }, EVALUATOR),
  tl('compare-fast', 108000, 'DURABLE', 'warn', "Fast migration check (recorded): fumbled, Maya's cart lost (404)", { cart_status: 404, charges: 0, option: 'fast', source: 'recorded' }, WORKFLOW),
  // What the web function writes.
  tl('web-start', 125000, 'WEB', 'info', 'Presenter pressed Start: 3 players in the room', { players: 3, run_id: RUN }),
  tl('web-close-growth', 96000, 'WEB', 'info', 'Presenter closed growth: 2 of 3 players picked', { round: 'growth', picked: 2, players: 3 }),
  tl('web-close-launch', 25 * 60000, 'WEB', 'info', 'Older than the window', { round: 'launch', picked: 3, players: 3 }),
];
const timelineItem = (lines = timelineLines()) => ({
  pk: s(`timeline#${RUN}`), run_id: s(RUN), lines: { L: lines.map(line => s(JSON.stringify(line))) },
});
const history = () => [
  { EventId: 1, EventType: 'ExecutionStarted', EventTimestamp: new Date(T - 119000) },
  { EventId: 2, EventType: 'StepStarted', SubType: 'Step', Id: 'op1', Name: 'plan-growth', EventTimestamp: new Date(T - 118000) },
  { EventId: 3, EventType: 'StepSucceeded', SubType: 'Step', Id: 'op1', Name: 'plan-growth', EventTimestamp: new Date(T - 116500) },
  { EventId: 4, EventType: 'CallbackStarted', Id: 'op2', Name: 'await-close-growth', EventTimestamp: new Date(T - 100000) },
  { EventId: 5, EventType: 'ContextStarted', SubType: 'ParallelBranch', Id: 'op3', Name: 'b', EventTimestamp: new Date(T - 99000) },
  { EventId: 6, EventType: 'ChainedInvokeStarted', Id: 'op4', Name: 'launch-shop', EventTimestamp: new Date(T - 112000) },
  { EventId: 7, EventType: 'ChainedInvokeSucceeded', Id: 'op4', Name: 'launch-shop', EventTimestamp: new Date(T - 109000) },
];
const seed = () => {
  aws.items.set('arena', arena());
  aws.items.set(`timeline#${RUN}`, timelineItem());
  aws.history = history();
};
seed();
const cwEvent = (id, ago, message) => ({ eventId: `e${id}`, timestamp: T - ago, message, logStreamName: 'x', ingestionTime: T });
// The structured lines the producers print: `[VM] <json>` (the VM's entry, relayed), `[EVENT] <json>`, `[WORKFLOW] <json>`.
const jline = (tag, entry) => `[${tag}] ${JSON.stringify(entry)}`;
const vmEntry = (extra = {}) => ({ seq: 0, t: T - 70000, kind: 'stage', level: 'info', run: RUN.slice(0, 8), action: 'compare', option: 'careful', msg: 'initdb done in 1.20 s', ms: 1200, ...extra });
const phpEntry = (extra = {}) => vmEntry({ seq: 1, kind: 'php', level: 'warn', msg: 'GET /cart/cart-saved 404 12 ms', method: 'GET', path: '/cart/cart-saved', status: 404, ms: 12, ...extra });
const eventEntry = (msg, extra = {}) => ({ t: T - 80000, level: 'info', action: 'compare', option: 'careful', step: 'compare-careful', msg, ...extra });
const workflowEntry = (step, round, level, msg) => ({ step, round, level, msg });
const fill = () => {
  // Thirty events spread over ten minutes in the web group, plus every other kind of line.
  aws.cw[WEB] = Array.from({ length: 30 }, (_, i) => cwEvent(`w${i}`, 600000 - i * 20000, access(i % 3 ? '/api/join' : '/api/state', 200, i % 3 ? 'info' : 'debug')));
  aws.cw[WORKFLOW] = [cwEvent('f1', 90000, jline('WORKFLOW', workflowEntry('plan-growth', 'growth', 'info', 'market 18%, 0 bots, 2 players')))];
  aws.cw[EVALUATOR] = [cwEvent('v1', 80000, jline('EVENT', eventEntry('VM running'))), cwEvent('v2', 70000, jline('VM', phpEntry()))];
  aws.cw[MICROVM] = [cwEvent('m1', 60000, 'PHP Fatal error: Uncaught Exception in /app/x.php')];
};
const { createHub, fromCloudWatch, inspectCloudWatch, fromTimeline, cloudWatchId, redact, snapshot, TAGS, LEVEL_LIST, REAL_OPTIONS, parseCursor } = await import('./logs.mjs');
const cwId = (group, event) => cloudWatchId(group, event);
const arnOf = group => `arn:aws:logs:ap-southeast-2:1:log-group:${group}`;
const tailResult = (group, ago, message) => ({ logGroupIdentifier: arnOf(group), logStreamName: 'x', timestamp: Date.now() - ago, ingestionTime: Date.now(), message });
const sdkClients = { db: aws, lambda: aws, logs: aws };
const hubFor = (clock = () => T, extra = {}) => createHub({ ...sdkClients, table: 'runs', groups: GROUPS, workflowFunction: 'black-friday-four-workflow', now: clock, ...extra });
const result = value => console.log('RESULT:' + JSON.stringify(value));

// ---- the real server, for the HTTP scenarios
const PORT = __PORT__;
const serve = async (extra = {}) => {
  Object.assign(process.env, {
    RUN_TABLE: 'runs', WORKFLOW_FUNCTION: 'black-friday-four-workflow', LOG_GROUPS: GROUPS.join(','), POLL_MS: '150',
    AWS_LAMBDA_HTTP_ENDPOINT: `127.0.0.1:${PORT}`, PUBLIC_HOST: 'logs.example.test', PRESENTER_PARAMETER: 'p-key', PASSPHRASE_PARAMETER: 'p-pass',
    ...extra,
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

# The producers' own example lines (VmEventSlice and ProducerSlice reports), as the CloudWatch message of each group.
VM_STAGE = {'seq': 0, 't': 1795585743478, 'kind': 'stage', 'level': 'info', 'run': 'aaaaaaaa', 'action': 'compare', 'option': 'careful',
            'msg': 'initdb done in 1.20 s', 'ms': 1200}
VM_PHP = {'seq': 1, 't': 1795585743478, 'kind': 'php', 'level': 'info', 'run': 'aaaaaaaa', 'action': 'compare', 'option': 'careful',
          'msg': 'GET /cart/cart-saved 404 12 ms', 'method': 'GET', 'path': '/cart/cart-saved', 'status': 404, 'ms': 12}
EVENT_LINE = {'t': 1795585743500, 'level': 'info', 'action': 'compare', 'option': 'careful', 'step': 'compare-careful',
              'msg': 'check /health: HTTP 200 in 4 ms', 'ms': 4}
WORKFLOW_LINE = {'step': 'plan-growth', 'round': 'growth', 'level': 'info', 'msg': 'market 18%, 0 bots, 2 players'}


def structured(tag, entry):
    return f'[{tag}] ' + json.dumps(entry, separators=(',', ':'))


PRODUCER_LINES = [
    # (group, message, tag, level, detail)
    ('EVALUATOR', structured('VM', VM_STAGE), 'VM', 'info',
     {'run': 'aaaaaaaa', 'stage': 'compare', 'option': 'careful', 'seq': 0, 'ms': 1200}),
    ('EVALUATOR', structured('VM', VM_PHP), 'PHP', 'info',
     {'run': 'aaaaaaaa', 'stage': 'compare', 'option': 'careful', 'seq': 1, 'method': 'GET', 'path': '/cart/cart-saved', 'status': 404, 'ms': 12}),
    ('EVALUATOR', structured('VM', {**VM_PHP, 'seq': 2, 'level': 'error', 'action': 'migrate', 'option': 'fast', 'status': 500, 'msg': 'POST /checkout 500 80 ms',
                                    'method': 'POST', 'path': '/checkout', 'ms': 80}), 'PHP', 'error',
     {'run': 'aaaaaaaa', 'stage': 'migrate', 'option': 'fast', 'seq': 2, 'method': 'POST', 'path': '/checkout', 'status': 500, 'ms': 80}),
    ('EVALUATOR', structured('VM', {**VM_STAGE, 'seq': 3, 'level': 'warn', 'action': 'launch', 'option': None, 'msg': 'php: Warning: x'}), 'VM', 'warn',
     {'run': 'aaaaaaaa', 'stage': 'launch', 'seq': 3, 'ms': 1200}),
    ('EVALUATOR', structured('EVENT', EVENT_LINE), 'EVENT', 'info',
     {'stage': 'compare', 'option': 'careful', 'step': 'compare-careful', 'ms': 4}),
    ('EVALUATOR', structured('EVENT', {'t': 1795585743600, 'level': 'warn', 'action': 'launch', 'option': None, 'step': 'launch', 'msg': 'VM log not read: ReadTimeout'}),
     'EVENT', 'warn', {'stage': 'launch', 'step': 'launch'}),
    ('EVALUATOR', structured('EVENT', {**EVENT_LINE, 'level': 'error', 'msg': 'stage failed: RuntimeError'}), 'EVENT', 'error',
     {'stage': 'compare', 'option': 'careful', 'step': 'compare-careful', 'ms': 4}),
    ('WORKFLOW', structured('WORKFLOW', {'step': 'timeline-evidence', 'round': None, 'level': 'warn',
                                         'msg': 'launch real, careful real (cart kept), fast recorded (cart lost)'}), 'WORKFLOW', 'warn',
     {'step': 'timeline-evidence'}),
    ('WORKFLOW', structured('WORKFLOW', {'step': 'fallback-migrate-fast', 'round': None, 'level': 'warn',
                                         'msg': 'migrate fast failed (RuntimeError): recorded evidence used'}), 'WORKFLOW', 'warn',
     {'step': 'fallback-migrate-fast'}),
    ('WORKFLOW', structured('WORKFLOW', WORKFLOW_LINE), 'WORKFLOW', 'info', {'step': 'plan-growth', 'round': 'growth'}),
]


def test_producer_example_lines_parse_to_the_right_tag_level_stage_and_option():
    out = run_scenario(f"""
      const cases = {json.dumps([[group, message] for group, message, *_ in PRODUCER_LINES])};
      const groups = {{ EVALUATOR, WORKFLOW }};
      result(cases.map(([group, message], index) => {{
        const event = fromCloudWatch(groups[group], cwEvent(index, 5000, message));
        return event && {{ tag: event.tag, level: event.level, text: event.text, detail: event.detail, source: event.source, ts: event.ts - T }};
      }}));
    """)
    for (group, message, tag, level, detail), got in zip(PRODUCER_LINES, out):
        assert got is not None, message
        assert (got['tag'], got['level'], got['detail']) == (tag, level, detail), message
        assert got['source'] == {'VM': 'vm', 'PHP': 'vm', 'EVENT': 'evaluator', 'WORKFLOW': 'workflow'}[tag]
    texts = [got['text'] for got in out]
    assert texts[0] == 'compare careful: initdb done in 1.20 s'
    assert texts[1] == 'compare careful: GET /cart/cart-saved 404 12 ms'
    assert texts[4] == 'compare-careful: check /health: HTTP 200 in 4 ms'
    assert texts[9] == 'plan-growth: market 18%, 0 bots, 2 players'


def test_a_json_wrapped_line_is_read_the_same_and_the_level_is_the_lines_own():
    out = run_scenario(f"""
      const wrapped = JSON.stringify({{ level: 'ERROR', message: {json.dumps(structured('VM', VM_STAGE))} }});
      const loud = {json.dumps(structured('VM', {**VM_STAGE, 'level': 'debug', 'msg': 'Error failed fallback retry warn Traceback'}))};
      const workflow = {json.dumps(structured('WORKFLOW', {**WORKFLOW_LINE, 'level': 'debug', 'msg': 'fallback failed with Exception'}))};
      result([fromCloudWatch(EVALUATOR, cwEvent(1, 0, wrapped)), fromCloudWatch(EVALUATOR, cwEvent(2, 0, loud)),
              fromCloudWatch(WORKFLOW, cwEvent(3, 0, workflow))].map(e => [e.tag, e.level]));
    """)
    assert out == [['VM', 'info'], ['VM', 'debug'], ['WORKFLOW', 'debug']], 'words in the message never change a structured line level'


def bad_lines():
    without_method = {key: value for key, value in VM_PHP.items() if key != 'method'}
    return [
        ('EVALUATOR', '[VM] {not json MARKER-1}'),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'extra': 1})),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'level': 'fatal'})),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'option': 'medium'})),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'action': 'deploy'})),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'run': 'AAAAAAAAA'})),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'msg': 'x' * 201})),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'seq': -1})),
        ('EVALUATOR', structured('VM', {**VM_STAGE, 'kind': 'access'})),
        ('EVALUATOR', structured('VM', without_method)),
        ('EVALUATOR', structured('VM', {**VM_PHP, 'path': '/cart?token=MARKER-2'})),
        ('EVALUATOR', structured('VM', {key: value for key, value in VM_STAGE.items() if key != 'run'})),
        ('EVALUATOR', structured('EVENT', {**EVENT_LINE, 'step': 'Compare Careful'})),
        ('EVALUATOR', structured('EVENT', {**EVENT_LINE, 'surprise': True})),
        ('EVALUATOR', '[EVENT] compare careful: VM running MARKER-3'),
        ('EVALUATOR', '[PHP] GET /cart 404 12 ms MARKER-4'),
        ('EVALUATOR', structured('WORKFLOW', WORKFLOW_LINE)),
        ('WORKFLOW', structured('VM', VM_STAGE)),
        ('WORKFLOW', structured('WORKFLOW', {**WORKFLOW_LINE, 'round': 'nope'})),
        ('WORKFLOW', structured('WORKFLOW', {**WORKFLOW_LINE, 'level': 'ERROR'})),
        ('WORKFLOW', structured('WORKFLOW', {key: value for key, value in WORKFLOW_LINE.items() if key != 'msg'})),
        ('WORKFLOW', '[WORKFLOW] open-round: callback registered MARKER-5'),
        ('MICROVM', structured('VM', VM_STAGE)),
    ]


def test_an_invalid_structured_line_is_dropped_and_counted_never_shown_raw():
    bad = bad_lines()
    out = run_scenario(f"""
      const bad = {json.dumps(bad)};
      const groups = {{ EVALUATOR, WORKFLOW, MICROVM }};
      const inspected = bad.map(([group, message], index) => inspectCloudWatch(groups[group], cwEvent(index, 5000, message)));
      fill();
      bad.forEach(([group, message], index) => aws.cw[groups[group]].push(cwEvent(`bad${{index}}`, 5000 + index, message)));
      const hub = hubFor();
      await hub.poll();
      const events = hub.eventsAfter(undefined, true);
      await hub.poll();                                  // the same lines are not counted twice
      const again = hub.eventsAfter(undefined, true);
      result({{ inspected, again: again.length - events.length, watch: events.filter(e => e.tag === 'WATCH'), blob: JSON.stringify(events) }});
    """)
    for message, got in zip(bad, out['inspected']):
        assert got.get('invalid') in ('VM', 'EVENT', 'WORKFLOW', 'PHP') and 'event' not in got, message
    assert [got['invalid'] for got in out['inspected']][:12] == ['VM'] * 12
    [watch] = out['watch']
    assert watch['level'] == 'warn' and watch['source'] == 'logs' and watch['id'] == f'wt:dropped:{len(bad)}'
    assert watch['detail'] == {'dropped': len(bad), 'VM': 14, 'EVENT': 3, 'PHP': 1, 'WORKFLOW': 5}
    assert watch['text'].startswith(f'{len(bad)} log lines broke the log contract and were dropped (')
    assert out['again'] == 0
    for marker in ('MARKER-1', 'MARKER-2', 'MARKER-3', 'MARKER-4', 'MARKER-5', 'not json'):
        assert marker not in out['blob'], marker


def test_the_same_invalid_line_from_live_tail_and_the_group_read_is_counted_once():
    out = run_scenario(r"""
      fill();
      const hub = hubFor(() => Date.now(), { accountId: '1', region: 'ap-southeast-2', pollMs: 60000, filterMs: 0 });
      const release = hub.attach(() => {});
      await sleep(100);
      const live = tailResult(EVALUATOR, 0, '[VM] {broken');
      aws.tails[0].push([live]);
      await sleep(60);
      const watch = () => hub.eventsAfter(undefined, true).filter(e => e.tag === 'WATCH').map(e => [e.id, e.detail.dropped]);
      const first = watch();
      aws.cw[EVALUATOR].push({ eventId: 'other-id', timestamp: live.timestamp, message: live.message, logStreamName: 'x' });
      await hub.poll();
      result({ first, second: watch() });
      release();
    """)
    assert out['first'] == [['wt:dropped:1', 1]] and out['second'] == out['first']


def test_vm_and_evaluator_lines_are_timed_by_their_own_clock_within_the_resume_overlap():
    out = run_scenario(r"""
      const at = ago => fromCloudWatch(EVALUATOR, { timestamp: T, message: jline('VM', vmEntry({ t: T - ago })) }).ts - T;
      const ordered = [vmEntry({ seq: 1, t: T - 3000 }), vmEntry({ seq: 0, t: T - 4000 })].map(entry =>
        fromCloudWatch(EVALUATOR, { timestamp: T, message: jline('VM', entry) }));
      result({ near: at(5000), far: at(10 * 60000), future: at(-9000), ordered: ordered.map(e => [e.detail.seq, e.ts - T]) });
    """)
    assert out['near'] == -5000, 'a VM entry keeps its own time, so the entries of one relay keep their order'
    assert out['far'] == -30000, 'clamped to half the resume overlap, so the next resume window still covers it'
    assert out['future'] == 0
    assert out['ordered'] == [[1, -3000], [0, -4000]]


HOSTILE = [
    ('WEB', json.dumps({'level': 'ERROR', 'message': 'Arena request failed abc'}), 'WEB'),
    ('WEB', json.dumps({'type': 'platform.invokeReport', 'record': {'status': 'error'}}), 'WEB'),
    ('WORKFLOW', 'Traceback (most recent call last)', 'WORKFLOW'),
    ('WORKFLOW', json.dumps({'level': 'ERROR', 'message': 'boom'}), 'WORKFLOW'),
    ('WORKFLOW', json.dumps({'type': 'platform.report', 'record': {'status': 'failure', 'metrics': {'durationMs': 50}}}), 'WORKFLOW'),
    ('EVENT', 'ValueError: Exception while judging', 'EVENT'),
    ('EVENT', 'Task timed out after 30.00 seconds', 'EVENT'),
    ('MICROVM', 'PHP Fatal error: Uncaught Exception in /app/x.php', 'MICROVM'),
]
GROUP_NAMES = {'WEB': 'WEB', 'WORKFLOW': 'WORKFLOW', 'EVENT': 'EVALUATOR', 'MICROVM': 'MICROVM'}


def test_a_platform_or_runtime_error_is_level_error_on_the_tag_of_its_own_group():
    out = run_scenario(f"""
      const cases = {json.dumps([[GROUP_NAMES[kind], message] for kind, message, _ in HOSTILE])};
      const groups = {{ WEB, WORKFLOW, EVALUATOR, MICROVM }};
      result(cases.map(([group, message], index) => {{
        const event = fromCloudWatch(groups[group], cwEvent(index, 0, message));
        return [event.tag, event.level];
      }}));
    """)
    assert out == [[tag, 'error'] for _, _, tag in HOSTILE]


def test_noise_lines_are_dropped():
    quiet = [
        ('WEB', 'plain chatter'), ('WORKFLOW', 'START RequestId: abc Version: $LATEST'), ('EVALUATOR', 'START RequestId: abc'),
        ('EVALUATOR', 'END RequestId: abc'), ('EVALUATOR', 'INIT_START Runtime Version: python'),
        ('EVALUATOR', 'free text the evaluator no longer prints'), ('MICROVM', '#12 [internal] load build definition'),
        ('MICROVM', 'an old [1a2b3c4d migrate careful] PHP shop ready in 1.92 s'),
    ]
    out = run_scenario(f"""
      const cases = {json.dumps(quiet)};
      const groups = {{ WEB, WORKFLOW, EVALUATOR, MICROVM }};
      result(cases.map(([group, message], index) => inspectCloudWatch(groups[group], cwEvent(index, 0, message))));
    """)
    assert out == [{}] * len(quiet)


def test_the_other_cloudwatch_lines_get_their_tag_level_and_text():
    lines = [
        ('WEB', json.dumps({'bf': 'access', 'level': 'debug', 'method': 'GET', 'route': '/api/state', 'status': 200, 'ms': 12,
                            'phase': 'open', 'round': 'growth'}), ('WEB', 'debug', 'GET /api/state 200 12 ms · open growth')),
        ('WEB', json.dumps({'level': 'INFO', 'message': json.dumps({'bf': 'access', 'level': 'info', 'method': 'POST', 'route': '/api/pick',
                                                                    'status': 409, 'ms': 3})}), ('WEB', 'info', 'POST /api/pick 409 3 ms')),
        ('WEB', json.dumps({'bf': 'access', 'method': 'GET', 'route': '/x', 'status': 503, 'ms': 3, 'token': 'zzz'}), ('WEB', 'error', 'GET /x 503 3 ms')),
        ('WEB', json.dumps({'type': 'platform.invokeSummary', 'record': {'metrics': {'requests': 42}}}), ('WEB', 'info', 'environment served 42 requests')),
        ('WEB', json.dumps({'type': 'platform.initStart', 'record': {}}), ('WEB', 'debug', 'platform.initStart')),
        ('WEB', json.dumps({'level': 'ERROR', 'message': 'Arena request failed abc'}), ('WEB', 'error', 'Arena request failed abc')),
        ('WORKFLOW', json.dumps({'type': 'platform.report', 'record': {'status': 'success', 'metrics': {'durationMs': 1234, 'initDurationMs': 500}}}),
         ('WORKFLOW', 'info', 'Durable invocation 1.23 s (cold start 0.5 s)')),
        ('WORKFLOW', json.dumps({'type': 'platform.report', 'record': {'status': 'failure', 'metrics': {'durationMs': 50}}}),
         ('WORKFLOW', 'error', 'Durable invocation 0.05 s · failure')),
        ('EVALUATOR', 'REPORT RequestId: abc\tDuration: 3210.00 ms\tBilled Duration: 3211 ms\tMemory Size: 512 MB\tInit Duration: 1500.00 ms',
         ('EVENT', 'info', 'Event judge done in 3.2 s (cold start 1.5 s)')),
        ('EVALUATOR', 'REPORT RequestId: abc\tDuration: 900.00 ms\tBilled Duration: 901 ms', ('EVENT', 'info', 'Event judge done in 0.9 s')),
    ]
    out = run_scenario(f"""
      const cases = {json.dumps([[group, message] for group, message, _ in lines])};
      const groups = {{ WEB, WORKFLOW, EVALUATOR, MICROVM }};
      result(cases.map(([group, message], index) => {{
        const event = fromCloudWatch(groups[group], cwEvent(index, 1000 * index, message));
        return {{ tag: event.tag, level: event.level, text: event.text, id: event.id, ts: event.ts - T }};
      }}));
    """)
    for index, ((_, _, expected), got) in enumerate(zip(lines, out)):
        assert (got['tag'], got['level'], got['text']) == expected, lines[index]
        assert re.fullmatch(r'cw:[0-9a-f]{16}', got['id']) and got['ts'] == -1000 * index


def test_error_lines_are_trimmed_to_200_characters():
    out = run_scenario(r"""
      const event = fromCloudWatch(EVALUATOR, cwEvent(1, 0, 'Exception: ' + 'x'.repeat(900)));
      result({ tag: event.tag, level: event.level, length: event.text.length });
    """)
    assert out == {'tag': 'EVENT', 'level': 'error', 'length': 200}


def test_a_300_character_line_with_a_receipt_at_character_190_is_redacted_then_truncated():
    receipt = 'a1b2c3d4' * 8
    line = 'Exception: ' + 'x' * 179 + receipt + 'y' * 46
    assert len(line) == 300 and line.index(receipt) == 190
    cases = [
        ('WEB', json.dumps({'level': 'ERROR', 'message': line})), ('WORKFLOW', json.dumps({'level': 'ERROR', 'message': line})),
        ('EVALUATOR', line), ('MICROVM', line),
    ]
    out = run_scenario(f"""
      const cases = {json.dumps(cases)};
      const groups = {{ WEB, WORKFLOW, EVALUATOR, MICROVM }};
      result(cases.map(([group, message], index) => {{
        const event = fromCloudWatch(groups[group], cwEvent(index, 0, message));
        return {{ tag: event.tag, text: event.text, raw: event.detail.raw }};
      }}));
    """)
    assert [got['tag'] for got in out] == ['WEB', 'WORKFLOW', 'EVENT', 'MICROVM']
    for got in out:
        assert len(got['text']) == 200 and got['text'].startswith('Exception: ' + 'x' * 179 + 'a1b2c3d4…y')
        assert receipt[:9] not in got['text'] and receipt[:9] not in got['raw'], 'cutting first would leave the front of the receipt'
        assert got['text'].count('a1b2c3d4') == 1


def hostile_hub():
    cases = json.dumps([[GROUP_NAMES[kind], message] for kind, message, _ in HOSTILE])
    return f"""
      fill();
      const hostile = {cases};
      const byName = {{ WEB, WORKFLOW, EVALUATOR, MICROVM }};
      hostile.forEach(([group, message], i) => aws.cw[byName[group]].push(cwEvent(`h${{i}}`, 1000 + i, message)));
      aws.cw[EVALUATOR].push(cwEvent('vm', 2000, jline('VM', vmEntry())));
      aws.items.set(`timeline#${{RUN}}`, timelineItem([...timelineLines(),
        tl('surprise', 90000, 'GAME', 'info', 'An id nobody knows'), tl('plan-launch', 90000, 'GAME', 'info', 'Plan'),
        tl('score-launch', 90000, 'DURABLE', 'fatal', 'Round scored')]));
      const hub = hubFor();
      await hub.poll();
    """


def test_the_page_and_the_server_share_one_vocabulary_and_no_line_is_ever_game_or_error():
    page = (PAGE_DIR / 'logs.js').read_text()
    known = re.findall(r"'([A-Z]+)'", re.search(r'const KNOWN_TAGS = \[([^\]]*)\]', page).group(1))
    colours = re.findall(r'\b([A-Z]+):', re.search(r'const TAG_COLOURS = \{(.*?)\};', page, re.S).group(1))
    levels = re.findall(r"'([a-z]+)'", re.search(r'const LEVELS = \[([^\]]*)\]', page).group(1))
    out = run_scenario(hostile_hub() + r"""
      const events = hub.eventsAfter(undefined, true);
      result({ tags: [...TAGS], levels: [...LEVEL_LIST], frozen: Object.isFrozen(TAGS), shown: events.map(e => [e.tag, e.level, e.source]),
               ids: events.filter(e => e.id.startsWith('tl:')).map(e => e.id) });
    """)
    assert out['tags'] == known == ['DURABLE', 'WORKFLOW', 'EVENT', 'MICROVM', 'VM', 'PHP', 'WEB', 'WATCH'] and out['frozen']
    assert sorted(colours) == sorted(known), 'a colour for each tag and no other'
    assert out['levels'] == levels == ['debug', 'info', 'warn', 'error']
    assert not re.search(r'\b(GAME|ERROR|PLAYER)\b', page)
    tags = {tag for tag, _, _ in out['shown']}
    assert tags <= set(known) and tags >= {'WEB', 'WORKFLOW', 'EVENT', 'MICROVM', 'VM', 'PHP', 'DURABLE', 'WATCH'}
    assert not tags & {'GAME', 'ERROR', 'PLAYER'}
    assert all(level in levels for _, level, _ in out['shown'])
    assert not [i for i in out['ids'] if i.endswith(('surprise', 'plan-launch', 'score-launch'))], 'lines with a bad id, tag or level are dropped'

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
        detail: Object.fromEntries(events.filter(e => e.id.startsWith('tl:')).map(e => [e.id, e.detail])),
        tags: events.filter(e => e.id.startsWith('tl:') && ['EVENT', 'GAME', 'ERROR'].includes(e.tag)).map(e => e.id),
        sorted: events.every((e, i) => !i || events[i - 1].ts <= e.ts),
      });
    """)
    assert out['status'] == {'run8': '01234567', 'phase': 'open', 'round': 'growth', 'players': 3, 'picked': 2}
    wf = 'workflow'
    assert out['pick'] == {
        'tl:01234567:registered': ['DURABLE', 'info', 'Execution registered', -120000, wf],
        'tl:01234567:open-growth': ['DURABLE', 'info', 'Round 2 · Growth open', -100000, wf],
        'tl:01234567:close-growth': ['DURABLE', 'info', 'Close received: 2/3 picked', -95000, wf],
        'tl:01234567:plan-growth': ['DURABLE', 'info', 'Market draw 105%', -94000, wf],
        'tl:01234567:score-growth': ['DURABLE', 'info', 'Round scored', -93000, wf],
        'tl:01234567:results-growth': ['DURABLE', 'info', 'Results published', -92000, wf],
        'tl:01234567:expire-christmas': ['DURABLE', 'warn', 'Close never arrived: the run expired', -91500, wf],
        'tl:01234567:final': ['DURABLE', 'info', 'Final results published', -91000, wf],
        'tl:01234567:launch': ['MICROVM', 'info', 'Launch shop (real MicroVM): checkout 200', -110000, wf],
        'tl:01234567:migrate-careful': ['MICROVM', 'info', 'Careful migration done (real MicroVM)', -109000, wf],
        'tl:01234567:compare-fast': ['DURABLE', 'warn', "Fast migration check (recorded): fumbled, Maya's cart lost (404)", -108000, wf],
        'tl:01234567:web-start': ['WEB', 'info', 'Presenter pressed Start: 3 players in the room', -125000, 'web'],
        'tl:01234567:web-close-growth': ['WEB', 'info', 'Presenter closed growth: 2 of 3 players picked', -96000, 'web'],
        'du:abcdefabcdef:1': ['DURABLE', 'info', 'execution started', -119000, 'durable'],
        'du:abcdefabcdef:4': ['DURABLE', 'info', 'await-close-growth: waiting for the presenter to Close', -100000, 'durable'],
        'du:abcdefabcdef:6': ['DURABLE', 'info', 'launch-shop: invoke Event judge (MicroVM)', -112000, 'durable'],
        'du:abcdefabcdef:7': ['DURABLE', 'info', 'launch-shop: invoke done 3.0 s', -109000, 'durable'],
    }, 'the 25-minute-old line, the parallel branch, StepStarted and the history twins of timeline lines are not shown'
    assert out['tags'] == [], 'no timeline line is ever tagged EVENT, GAME or ERROR'
    assert out['detail']['tl:01234567:launch'] == {
        'charges': 1, 'checkout_status': 200, 'vm_id': 'vm-launch', 'source': 'real', 'log_group': '/aws/lambda/black-friday-four-evaluator', 'stage': 'launch'}
    assert out['detail']['tl:01234567:compare-fast'] == {
        'cart_status': 404, 'charges': 0, 'option': 'fast', 'source': 'recorded', 'log_group': '/aws/lambda/black-friday-four-workflow', 'stage': 'compare'}
    assert out['detail']['tl:01234567:registered']['run_id'] == '01234567…'
    assert out['detail']['tl:01234567:close-growth'] == {
        'players': 3, 'picked': 2, 'round': 'growth', 'log_group': '/aws/lambda/black-friday-four-workflow'}
    assert out['sorted'] and len(out['ids']) == len(set(out['ids']))
    blob = json.dumps(out)
    for secret in ('Zelda', 'Yuri', 'Xena', 'ffffffff', 'token'):
        assert secret not in blob


def test_the_fast_migration_fumble_is_shown_as_the_line_says_never_upgraded_to_an_error():
    out = run_scenario(r"""
      const lines = [
        tl('compare-fast', 5000, 'MICROVM', 'info', "Fast migration check: fumbled, Maya's cart lost (404)", { cart_status: 404, charges: 0, option: 'fast', source: 'real' }, EVALUATOR),
        tl('compare-careful', 4000, 'MICROVM', 'info', "Careful migration check: Maya's cart kept (200)", { cart_status: 200, charges: 1, option: 'careful', source: 'real' }, EVALUATOR),
      ];
      const events = fromTimeline(RUN, lines).events;
      result(events.map(e => [e.tag, e.level, e.text]));
    """)
    assert out == [['MICROVM', 'info', "Fast migration check: fumbled, Maya's cart lost (404)"],
                   ['MICROVM', 'info', "Careful migration check: Maya's cart kept (200)"]]


def test_the_timeline_wins_over_durable_history_for_the_same_fact():
    out = run_scenario(STORY_SEED + r"""
      const hub = hubFor(() => T);
      await hub.poll();
      const events = hub.eventsAfter(undefined, true).filter(e => /^(tl|du):/.test(e.id));
      result(events.map(e => [e.id.split(':')[0], e.text]));
    """)
    texts = [text for _, text in out]
    assert not [text for text in texts if 'Close received after' in text or ': step done' in text], 'the history twins of Close received and the steps'
    assert 'Close received: 3/3 picked' in texts and 'Results published' in texts and 'Round 1 · Launch open' in texts
    assert any(kind == 'du' and text.startswith('await-close-launch: waiting') for kind, text in out), 'what only Durable knows stays'
    assert any(kind == 'du' and text.startswith('launch-shop: invoke done') for kind, text in out)
    assert sum(1 for text in texts if text.startswith('Close received')) == 2, 'launch and growth: once each'


def test_a_timeline_line_with_a_bad_id_tag_level_source_or_field_is_dropped_and_counted_once():
    out = run_scenario(r"""
      const bad = [
        tl('surprise', 90000, 'DURABLE', 'info', 'An id nobody knows'),
        tl('plan-launch', 90000, 'GAME', 'info', 'Plan'),
        tl('plan-nowhere', 90000, 'DURABLE', 'info', 'A round that does not exist'),
        tl('score-launch', 90000, 'DURABLE', 'fatal', 'Round scored'),
        tl('migrate-fast', 90000, 'MICROVM', 'warn', 'Fast: recorded evidence used', { option: 'fast', source: 'recorded' }),
        tl('launch', 90000, 'DURABLE', 'info', 'Launch shop', { source: 'real' }),
        tl('compare-careful', 90000, 'MICROVM', 'info', 'No source at all'),
        tl('web-close-launch', 90000, 'DURABLE', 'info', 'The web function never writes DURABLE'),
        { ...tl('open-launch', 90000, 'DURABLE', 'info', 'Round 1 open'), extra: 1 },
        { ...tl('results-launch', 90000, 'DURABLE', 'info', 'Results'), t: 'soon' },
      ];
      const item = timelineItem([...timelineLines(), ...bad]);
      item.lines.L.push(s('{broken'));
      aws.items.set(`timeline#${RUN}`, item);
      const hub = hubFor();
      await hub.poll(); await hub.poll();
      const events = hub.eventsAfter(undefined, true);
      result({ count: bad.length + 1, watch: events.filter(e => e.tag === 'WATCH').map(e => [e.id, e.level, e.detail]),
               ids: events.filter(e => e.id.startsWith('tl:')).map(e => e.id.slice(12)), direct: fromTimeline(RUN, [bad[1], bad[4], timelineLines()[0]]) });
    """)
    assert out['watch'] == [[f'wt:dropped:{out["count"]}', 'warn', {'dropped': out['count'], 'timeline': out['count']}]]
    assert not {'surprise', 'plan-launch', 'plan-nowhere', 'score-launch', 'migrate-fast', 'compare-careful', 'open-launch', 'results-launch'} & set(out['ids'])
    assert out['ids'].count('launch') == 1
    assert len(out['direct']['invalid']) == 2 and len(out['direct']['lines']) == 1 and len(out['direct']['events']) == 1

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
      const webIds = new Set(aws.cw[WEB].map(event => cwId(WEB, event)));
      aws.pageSize = 3;
      const hub = hubFor(() => T);
      await hub.poll();
      const first = { calls: aws.calls.cw[WEB], start: T - aws.calls.starts[WEB][0],
                      count: hub.eventsAfter(undefined, true).filter(e => webIds.has(e.id)).length };
      await hub.poll();
      result({ first, secondStart: T - aws.calls.starts[WEB].at(-1),
               count: hub.eventsAfter(undefined, true).filter(e => webIds.has(e.id)).length });
    """)
    # Thirty web events need ten pages of three: the first poll reads five (15 events) from now - 15 min.
    assert out['first'] == {'calls': 5, 'start': 15 * 60000, 'count': 15}
    # The newest event seen was 320 s ago, so the next read starts 60 s before it, and finds ten more.
    assert out['secondStart'] == 380000
    assert out['count'] == 26



def test_debug_events_are_capped_at_the_newest_3000_and_old_events_expire():
    out = run_scenario(r"""
      aws.cw[WEB] = Array.from({ length: 3500 }, (_, i) => cwEvent(`d${i}`, 800000 - i * 100, access('/api/state', 200, 'debug')));
      aws.cw[EVALUATOR] = [cwEvent('late', 900000 - 1, jline('EVENT', eventEntry('x')))];
      const hub = hubFor(() => T);
      await hub.poll();
      const debug = hub.eventsAfter(undefined, true).filter(e => e.level === 'debug');
      const noDebug = hub.eventsAfter(undefined, false);
      const ids = new Set(debug.map(e => e.id));
      result({ debug: debug.length, noDebug: noDebug.some(e => e.level === 'debug'), hasNewest: ids.has(cwId(WEB, aws.cw[WEB][3499])), hasOldest: ids.has(cwId(WEB, aws.cw[WEB][0])),
               late: noDebug.some(e => e.id === cwId(EVALUATOR, aws.cw[EVALUATOR][0])) });
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
      const routes = ['/events', '/api/status', '/api/players', '/viewer', '/logs.js', '/logs.css'];
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
    assert out['denied'] == {'/events': 403, '/api/status': 403, '/api/players': 403, '/viewer': 303, '/logs.js': 403, '/logs.css': 403}
    assert set(out['allowed'].values()) == {200}
    assert out['wrong'] == 403 and out['bad'] == 403
    assert out['streamDenied'] == 403 and out['streamAllowed'] == 200


def test_signed_out_requests_to_every_other_route_learn_nothing():
    out = run_scenario(r"""
      fill(); await serve();
      const index = (await call('GET', '/')).text;
      const version = path => new RegExp(`${path.replace('.', '\\.')}\\?v=([0-9a-f]{10})`).exec(index)?.[1];
      const paths = ['/viewer', '/logs.js', `/logs.js?v=${'0'.repeat(10)}`, '/logs.css', '/logs.css?v=abc', '/stream', '/stream?debug=1',
                     '/events', '/events?after=0&debug=1', '/api/status', '/api/players', '/nope', '/api/logout'];
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


def test_status_never_carries_names_and_nothing_carries_tokens_or_hashes():
    out = run_scenario(r"""
      fill(); await serve();
      const events = await call('GET', '/events?debug=1', { headers: KEY });
      const status = await call('GET', '/api/status', { headers: KEY });
      const players = await call('GET', '/api/players', { headers: KEY });
      const stream = await open('/stream?debug=1');
      await stream.until(text => text.includes('event: story'));
      result({ events: events.text, status: status.text, players: players.text, stream: stream.text, snapshot: snapshot(arena()) });
    """)
    for secret in ('Zelda', 'Yuri', 'Xena', 'teams_data'):
        assert secret not in out['status'], 'the status snapshot has counts only'
    for blob in (out['events'], out['status'], out['players'], out['stream']):
        for secret in ('token', 'ffffffff', 'eeeeeeee', 'dddddddd', 'teams_data'):
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
    assert out['keys'] == ['now', 'events', 'status', 'story'] and out['nowClose'] and out['status'] == 'open'
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
      aws.cw[EVALUATOR].push(cwEvent('fresh', 0, jline('EVENT', eventEntry('new line', { t: Date.now() }))));
      aws.cw[EVALUATOR].at(-1).timestamp = Date.now();
      await stream.until(text => text.includes('compare-careful: new line'));
      result({ before, after: stream.events().length, fresh: stream.events().filter(e => e.id === cwId(EVALUATOR, aws.cw[EVALUATOR].at(-1))).length });
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


# ---------------------------------------------------------------- Live Tail

def test_live_tail_session_uses_the_log_group_arns_and_pushes_events_before_the_next_poll():
    out = run_scenario(r"""
      fill();
      const hub = hubFor(() => Date.now(), { accountId: '1', region: 'ap-southeast-2', pollMs: 60000, filterMs: 0 });
      const got = [];
      const release = hub.attach((added, status) => got.push({ ids: added.map(event => event.id), status }));
      await sleep(100);
      const started = aws.tails.length, groups = aws.tails[0].input.logGroupIdentifiers, polls = hub.polls;
      // Live Tail results carry no event id: a line is told apart by group, timestamp and message.
      const at = Date.now();
      got.length = 0;
      const live = tailResult(EVALUATOR, 0, jline('EVENT', eventEntry('live line', { t: Date.now() })));
      aws.tails[0].push([live, tailResult(EVALUATOR, 0, 'START RequestId: abc'),
        { ...tailResult(WEB, 0, access('/api/join')), logGroupIdentifier: arnOf(WEB + '.local') }]);
      for (let i = 0; i < 60 && got.length === 0; i++) await sleep(5);
      const latency = Date.now() - at;
      const pushed = got[0];
      // FilterLogEvents later returns the same line (with its own event id): it must not appear twice.
      const sameLine = { eventId: 'filter-id-differs', timestamp: live.timestamp, message: live.message, logStreamName: 'x' };
      aws.cw[EVALUATOR].push(sameLine);
      await hub.poll();
      const lines = hub.eventsAfter(undefined, true).filter(event => event.text === 'compare-careful: live line');
      result({ started, groups, polls, latency, count: pushed.ids.length, status: pushed.status, pollsAfter: hub.polls,
               lines: lines.length, sameId: lines[0].id === cwId(EVALUATOR, sameLine) && pushed.ids[0] === lines[0].id });
      release();
    """)
    assert out['started'] == 1
    assert out['groups'] == [f'arn:aws:logs:ap-southeast-2:1:log-group:{group}' for group in (
        '/aws/lambda/web/black-friday-four', '/aws/lambda/black-friday-four-workflow',
        '/aws/lambda/black-friday-four-evaluator', '/aws/lambda-microvms/black-friday-four-shop')]
    assert out['polls'] >= 1 and out['pollsAfter'] == out['polls'] + 1
    assert out['latency'] < 250 and out['count'] == 1, 'one event: noise and the .local group are dropped'
    assert out.get('status') is None, 'a Live Tail push carries no status'
    assert out['lines'] == 1 and out['sameId']


def test_with_live_tail_the_group_reads_are_only_the_backfill_and_a_gap_filler_every_10_s():
    out = run_scenario(r"""
      fill();
      const clock = { t: T };
      const hub = hubFor(() => clock.t, { accountId: '1', region: 'ap-southeast-2', pollMs: 100000 });
      await hub.touch();
      const reads = () => ({ web: aws.calls.cw[WEB], evaluator: aws.calls.cw[EVALUATOR], arena: aws.calls.arena });
      const first = reads();
      await hub.poll(); clock.t += 2000;
      await hub.poll(); clock.t += 2000;
      const quiet = reads();
      clock.t += 6000;                      // 10 s since the backfill
      await hub.poll();
      result({ first, quiet, filled: reads() });
    """)
    assert out['first'] == {'web': 1, 'evaluator': 1, 'arena': 1}
    assert out['quiet'] == {'web': 1, 'evaluator': 1, 'arena': 3}, 'game state is still read every poll'
    assert out['filled'] == {'web': 2, 'evaluator': 2, 'arena': 4}


def test_live_tail_restarts_with_backoff_reports_failures_and_closes_when_idle():
    out = run_scenario(r"""
      fill();
      const reports = [];
      const hub = hubFor(() => Date.now(), { accountId: '1', region: 'ap-southeast-2', pollMs: 60000, tailIdleMs: 150, tailBackoffMs: 20,
        report: (source, error) => reports.push([source, error?.name]) });
      let release = hub.attach(() => {});
      await sleep(80);
      const first = aws.tails.length;
      aws.tails[0].end();                      // the 3 hour cap: the session ends on its own
      await sleep(120);
      const afterEnd = aws.tails.length;
      aws.tails[afterEnd - 1].fail(Object.assign(new Error('x'), { name: 'AccessDeniedException' }));
      await sleep(200);
      const afterFail = aws.tails.length;
      const watch = hub.eventsAfter(undefined, true).filter(event => event.tag === 'WATCH').map(event => event.text);
      release();
      await sleep(600);
      const stopped = { active: hub.tailActive, aborted: aws.tails.at(-1).aborted, count: aws.tails.length };
      release = hub.attach(() => {});          // a new client starts a fresh session
      await sleep(100);
      result({ first, afterEnd, afterFail, reports, watch, stopped, restarted: aws.tails.length, active: hub.tailActive });
      release();
    """)
    assert out['first'] == 1 and out['afterEnd'] == 2 and out['afterFail'] == 3
    assert ['live tail', 'AccessDeniedException'] in out['reports']
    assert out['watch'] == ['live tail unavailable: AccessDeniedException']
    assert out['stopped'] == {'active': False, 'aborted': True, 'count': 3}
    assert out['restarted'] == 4 and out['active'] is True


def test_live_tail_lines_reach_sse_clients_immediately_not_on_the_next_poll():
    out = run_scenario(r"""
      fill();
      await serve({ AWS_ACCOUNT_ID: '1', AWS_REGION: 'ap-southeast-2', POLL_MS: '5000' });
      const stream = await open('/stream?debug=1');
      await stream.until(text => text.includes('event: status'));
      const before = { reads: aws.cwDone, statuses: stream.statuses(), tails: aws.tails.length };
      const at = Date.now();
      const stamp = Date.now();
      const live = { ...tailResult(EVALUATOR, 0, jline('EVENT', eventEntry('live over sse', { t: stamp }))), timestamp: stamp };
      aws.tails[0].push([live]);
      await stream.until(text => text.includes('live over sse'), 2000);
      const message = stream.messages().find(m => m.data && m.data.includes('live over sse'));
      result({ latency: Date.now() - at, before, after: { reads: aws.cwDone, statuses: stream.statuses(), tails: aws.tails.length },
               idIsTs: message.id === String(live.timestamp), event: JSON.parse(message.data) });
    """)
    assert out['latency'] < 1000, 'the next poll is 5 s away'
    assert out['before'] == out['after'] and out['before']['tails'] == 1
    assert out['idIsTs'] and out['event']['tag'] == 'EVENT' and out['event']['text'] == 'compare-careful: live over sse'


# ---------------------------------------------------------------- story, follow and the roster

# A game after the launch round closed and the growth round opened. Times are offsets from `base` in ms.
STORY_SEED = r"""
const base = T - 60000;
const ev = (id, type, at, extra = {}) => ({ EventId: id, EventType: type, EventTimestamp: new Date(base + at), ...extra });
aws.history = [
  ev(1, 'ExecutionStarted', 0),
  ev(2, 'ChainedInvokeStarted', 1000, { Id: 'a', Name: 'launch-shop' }), ev(3, 'ChainedInvokeSucceeded', 10800, { Id: 'a', Name: 'launch-shop' }),
  ev(4, 'ChainedInvokeStarted', 11000, { Id: 'b', Name: 'migrate-careful' }), ev(5, 'ChainedInvokeSucceeded', 22400, { Id: 'b', Name: 'migrate-careful' }),
  ev(6, 'ChainedInvokeStarted', 11000, { Id: 'c', Name: 'migrate-fast' }), ev(7, 'ChainedInvokeSucceeded', 22600, { Id: 'c', Name: 'migrate-fast' }),
  ev(8, 'ChainedInvokeStarted', 23000, { Id: 'd', Name: 'compare-careful' }), ev(9, 'ChainedInvokeSucceeded', 30000, { Id: 'd', Name: 'compare-careful' }),
  ev(10, 'ChainedInvokeStarted', 23000, { Id: 'e', Name: 'compare-fast' }), ev(11, 'ChainedInvokeSucceeded', 30000, { Id: 'e', Name: 'compare-fast' }),
  ev(12, 'InvocationCompleted', 30500),
  ev(13, 'CallbackStarted', 31000, { Id: 'f', Name: 'await-close-launch' }), ev(14, 'CallbackSucceeded', 40000, { Id: 'f', Name: 'await-close-launch' }),
  ev(15, 'StepSucceeded', 40600, { Id: 'g', Name: 'publish-results-launch', SubType: 'Step' }),
  ev(16, 'InvocationCompleted', 40700), ev(17, 'InvocationCompleted', 41000),
  ev(18, 'CallbackStarted', 41500, { Id: 'h', Name: 'await-close-growth' }),
];
const row = (id, name, rank, extra = {}) => ({ id, name, house: false, bot: false, rank, option: 'medium', roll: 12, risk: 35, hit: false,
  event: 'Demand was normal this round', cumulative_cents: 519700 + rank, evidence: 'modelled', ...extra });
const results = { round: 'launch', market: 112, rows: [row('t1', 'Zelda Zebra', 2), row('t2', 'Yuri Yak', 1, { risk: 0 }), row('t3', 'Xena Xerus', 3),
  { ...row('bot1', 'Bot Lambo', 4), bot: true }, { ...row('house', 'House', 5), house: true }] };
aws.items.set(`results#${RUN}#launch`, { pk: s(`results#${RUN}#launch`), data: s(JSON.stringify(results)) });
Object.assign(aws.items.get('arena'), {
  results_round: s('launch'),
  launch_data: s(JSON.stringify({ source: 'real', charges: 1, checkout_statuses: [201, 200], cart_status: 200 })),
  evidence_data: s(JSON.stringify({ careful: { source: 'real', cart_kept: true }, fast: { source: 'real', cart_kept: false } })),
});
const extraLines = [
  { id: 'web-start', t: base - 5000, tag: 'WEB', level: 'info', text: 'Presenter pressed Start: 3 players in the room', detail: { players: 3 } },
  { id: 'web-close-launch', t: base + 39900, tag: 'WEB', level: 'info', text: 'closed', detail: { round: 'launch', picked: 3, players: 3 } },
  { id: 'open-launch', t: base - 4000, tag: 'DURABLE', level: 'info', text: 'Round 1 · Launch open', detail: { round: 'launch' } },
  { id: 'close-launch', t: base + 40000, tag: 'DURABLE', level: 'info', text: 'Close received: 3/3 picked', detail: { players: 3, picked: 3, round: 'launch' } },
  { id: 'results-launch', t: base + 40600, tag: 'DURABLE', level: 'info', text: 'Results published', detail: { round: 'launch' } },
];
aws.items.get(`timeline#${RUN}`).lines.L.push(...extraLines.map(line => s(JSON.stringify(line))));
"""


def test_story_cards_say_what_the_system_is_doing_with_numbers_taken_from_the_data():
    out = run_scenario(STORY_SEED + r"""
      const hub = hubFor(() => T);
      await hub.poll();
      result(hub.storyCards());
    """)
    cards = {card['stage']: card for card in out}
    assert list(cards) == ['lobby', 'start', 'launch', 'migrations', 'checks', 'open-launch', 'close-launch', 'open-growth']
    assert [card['seq'] for card in out] == sorted(card['seq'] for card in out)
    assert [card['id'] for card in out] == [f'st:01234567:{stage}' for stage in cards]
    assert cards['lobby']['text'] == ("The lobby closed with 3 players. Until Start, nothing runs in AWS: "
                                      "the Web Function only answers phones.")
    assert cards['start']['text'] == "The Web Function started one Durable execution: the game's memory for the whole night."
    assert cards['launch']['text'] == ('Durable asked an Event judge, which booted a MicroVM running the real PHP shop. '
                                       'It sold a mug, charged exactly once, and the VM was destroyed (9.8 s).')
    assert cards['migrations']['text'] == ('Two Event judges, two MicroVMs in parallel: careful copies every table, '
                                           'fast skips the carts (11.4 s / 11.6 s).')
    assert cards['checks']['text'] == ("Two fresh VMs restore each database and look for Maya's cart: "
                                       'careful kept it, fast fumbled it (carts skipped by design).')
    assert cards['close-launch']['text'] == ("Close woke the workflow: it drew the market (112%), rolled everyone's dice, "
                                             'scored 3 players and published, in 0.6 s. Then it went back to sleep.')
    assert cards['open-launch']['text'].startswith('Round 1 was open: 3/3 picked.')
    assert cards['open-growth']['text'] == ("Round 2 is open. Durable is suspended, waiting for the presenter's Close. "
                                            'No code is running and nothing is billed. 2/3 picked.')
    # The numbers, each one from the fixture: the history's timestamps, the launch and results items, the roster.
    assert cards['lobby']['numbers'] == {'players': 3}
    assert cards['launch']['numbers'] == {'seconds': 9.8, 'charges': 1}
    assert cards['migrations']['numbers'] == {'careful_s': 11.4, 'fast_s': 11.6}
    assert cards['close-launch']['numbers'] == {'round': 1, 'market': 112, 'players': 3, 'seconds': 0.6}
    assert cards['open-growth']['numbers'] == {'round': 2, 'picked': 2, 'players': 3}
    # No sentence carries a number its card does not list.
    for card in out:
        shown = {str(value) for value in card['numbers'].values()}
        for token in re.findall(r'\d+(?:\.\d+)?', card['text']):
            assert token in shown, (card['stage'], token, card['text'])


def test_a_number_that_is_not_in_the_data_is_left_out_of_the_sentence():
    out = run_scenario(STORY_SEED + r"""
      aws.history = aws.history.filter(e => e.Name !== 'publish-results-launch' && !/^(migrate|launch)/.test(e.Name ?? ''));
      delete aws.items.get('arena').launch_data;
      aws.items.delete(`results#${RUN}#launch`);
      const hub = hubFor(() => T);
      await hub.poll();
      result(hub.storyCards());
    """)
    cards = {card['stage']: card for card in out}
    assert 'launch' in cards and 'migrations' in cards, 'the timeline evidence lines still say these ran'
    assert cards['launch']['text'] == 'Durable asked an Event judge, which booted a MicroVM running the real PHP shop.'
    assert cards['launch']['numbers'] == {}
    assert cards['migrations']['text'].endswith('fast skips the carts.') and cards['migrations']['numbers'] == {}
    assert cards['close-launch']['text'] == ("Close woke the workflow: it is drawing the market, rolling everyone's dice and scoring.")
    assert cards['close-launch']['numbers'] == {'round': 1}


def test_final_card_counts_runs_and_microvms_from_the_history():
    out = run_scenario(STORY_SEED + r"""
      aws.history.push(ev(19, 'ExecutionSucceeded', 59000));
      aws.items.get('arena').phase = s('final');
      const hub = hubFor(() => T);
      await hub.poll();
      result(hub.storyCards().filter(card => card.stage === 'final'));
    """)
    [card] = out
    assert card['text'] == 'Game over. One Durable execution, 3 short runs of our code, 5 MicroVMs.'
    assert card['numbers'] == {'runs': 3, 'microvms': 5}


def test_follow_lines_come_from_picks_and_result_rows_and_use_no_gendered_pronouns():
    out = run_scenario(STORY_SEED + r"""
      const hub = hubFor(() => T);
      await hub.poll();
      const cards = Object.fromEntries(hub.storyCards().map(card => [card.stage, card]));
      result({ growth: cards['open-growth'].players, launch: cards['close-launch'].players, players: hub.players });
    """)
    assert out['growth']['t1'] == {
        'lines': ["Zelda Zebra picked Careful migration: their shop is the real VM that kept Maya's cart."], 'pick': 'careful'}
    assert out['growth']['t3']['lines'] == [
        'Xena Xerus picked Fast migration: their shop is the real VM that fumbled the migration, skipping the carts by design.']
    assert 't2' not in out['growth'], 'nobody who has not picked has a pick line'
    assert out['launch']['t1']['lines'] == [
        "Zelda Zebra rolled 12 against Medium server's 35: Demand was normal this round.",
        'Zelda Zebra is 2nd on $5,197 (evidence: modelled).']
    assert out['launch']['t2']['lines'][0] == 'Yuri Yak: Demand was normal this round.', 'no dice line when there is no risk'
    assert out['players'] == [{'id': 't3', 'name': 'Xena Xerus'}, {'id': 't2', 'name': 'Yuri Yak'}, {'id': 't1', 'name': 'Zelda Zebra'}]
    for lines in [out['growth'], out['launch']]:
        for entry in lines.values():
            for line in entry['lines']:
                assert not re.search(r"\b(his|her|hers|he|she|him)\b", line), line


def test_story_lines_attach_to_their_card_by_stage_run_and_option_and_follow_filters_by_option():
    page = (PAGE_DIR / 'logs.js').read_text()
    out = run_scenario(STORY_SEED + f"""
      const page = {json.dumps(page)};
      const slice = page.slice(page.indexOf('function attached'), page.indexOf('function el('));
      const consts = page.match(/^const (?:STAGE_CARD|VM_STAGES) = .*;$/gm).join('\\n');
      const build = new Function('lineEvents', 'storyRun', 'followedOption', 'cards', `${{consts}}\\n${{slice}}\\nreturn attached;`);
      const run = RUN.slice(0, 8);
      const vm = (seq, extra) => jline('VM', vmEntry({{ seq, run, t: T - 50000 + seq * 1000, ...extra }}));
      aws.cw[EVALUATOR] = [
        cwEvent('a', 40000, vm(0, {{ action: 'launch', option: null, msg: 'shop ready' }})),
        cwEvent('b', 39000, vm(1, {{ action: 'migrate', option: 'careful', msg: 'dump copied' }})),
        cwEvent('c', 38000, vm(2, {{ action: 'migrate', option: 'fast', msg: 'dump skipped' }})),
        cwEvent('d', 37000, jline('VM', phpEntry({{ seq: 3, run, t: T - 37000, level: 'info', msg: 'GET /cart/cart-saved 200 5 ms', status: 200, ms: 5 }}))),
        cwEvent('e', 36000, jline('VM', phpEntry({{ seq: 4, run, t: T - 36000, option: 'fast', msg: 'GET /cart/cart-saved 404 12 ms' }}))),
        cwEvent('f', 35000, vm(5, {{ run: 'ffffffff', action: 'launch', option: null, msg: 'another run' }})),
        cwEvent('g', 34000, jline('EVENT', eventEntry('check /health: HTTP 200 in 4 ms', {{ t: T - 34000, option: 'fast', step: 'compare-fast' }}))),
      ];
      const hub = hubFor(() => T);
      await hub.poll();
      const lineEvents = hub.eventsAfter(undefined, true).filter(e => e.detail && e.detail.stage);
      const cards = hub.storyCards().filter(card => ['launch', 'migrations', 'checks'].includes(card.stage));
      let mine = null;
      const attached = build(lineEvents, run, () => mine, new Map(cards.map(card => [card.id, card])));
      const texts = {{}};
      for (const who of [null, 'careful', 'fast']) {{
        mine = who;
        texts[who ?? 'nobody'] = Object.fromEntries(cards.map(card => [card.stage, attached(card).map(e => e.text)]));
      }}
      result(texts);
    """)
    launch = ['Launch shop (real MicroVM): checkout 200', 'launch: shop ready']
    careful_check = 'compare careful: GET /cart/cart-saved 200 5 ms'
    fast_checks = ["Fast migration check (recorded): fumbled, Maya's cart lost (404)", 'compare fast: GET /cart/cart-saved 404 12 ms',
                   'compare-fast: check /health: HTTP 200 in 4 ms']
    assert out['nobody'] == {
        'launch': launch,
        'migrations': ['Careful migration done (real MicroVM)', 'migrate careful: dump copied', 'migrate fast: dump skipped'],
        'checks': [fast_checks[0], careful_check, *fast_checks[1:]],
    }, 'by stage, oldest first; the other run (ffffffff) is left out'
    assert out['careful'] == {'launch': launch, 'migrations': ['Careful migration done (real MicroVM)', 'migrate careful: dump copied'],
                              'checks': [careful_check]}
    assert out['fast'] == {'launch': launch, 'migrations': ['migrate fast: dump skipped'], 'checks': fast_checks}


def test_roster_diffing_joins_renames_picks_changes_and_removals():
    out = run_scenario(r"""
      const { parseRoster, diffRoster } = await import('./logs.mjs');
      const roster = (...players) => parseRoster({ teams_data: s(JSON.stringify(players.map(([id, name, picks]) => ({ id, name, token_hash: 'f'.repeat(64), picks })))) });
      const map = players => new Map(players.map(player => [player.id, player]));
      const at = 1000, run8 = '01234567';
      const kinds = events => events.map(event => [event.id.replace('pl:01234567:', ''), event.level, event.text]);
      const base = roster(['a', 'Ann', {}], ['b', 'Bob', {}]);
      const silent = diffRoster(undefined, base, { run8, at, announceJoins: false });
      const lobby = diffRoster(undefined, base, { run8, at, announceJoins: true });
      const joined = diffRoster(map(base), roster(['a', 'Ann', {}], ['b', 'Bob', {}], ['c', 'Cy', {}]), { run8, at });
      const renamed = diffRoster(map(base), roster(['a', 'Anna', {}], ['b', 'Bob', {}]), { run8, at });
      const picked = diffRoster(map(base), roster(['a', 'Ann', { growth: 'careful' }], ['b', 'Bob', {}]), { run8, at });
      const changed = diffRoster(map(roster(['a', 'Ann', { growth: 'careful' }])), roster(['a', 'Ann', { growth: 'fast' }]), { run8, at });
      const removed = diffRoster(map(base), roster(['a', 'Ann', {}]), { run8, at });
      const same = diffRoster(map(base), base, { run8, at });
      const again = diffRoster(map(base), roster(['a', 'Ann', { growth: 'careful' }], ['b', 'Bob', {}]), { run8, at: 9999 });
      result({ silent, lobby: kinds(lobby), joined: kinds(joined), renamed: kinds(renamed), picked: kinds(picked), changed: kinds(changed),
               removed: kinds(removed), same, sameIds: picked.map(e => e.id).join() === again.map(e => e.id).join(), detail: picked[0].detail, tag: picked[0].tag,
               text: JSON.stringify([lobby, joined, renamed, picked, changed, removed]) });
    """)
    assert out['silent'] == [] and out['same'] == []
    assert out['lobby'] == [['a:join', 'info', 'Ann joined (2 players)'], ['b:join', 'info', 'Bob joined (2 players)']]
    assert out['joined'] == [['c:join', 'info', 'Cy joined (3 players)']]
    [renamed] = out['renamed']
    assert re.fullmatch(r'a:ren:[0-9a-f]{8}', renamed[0]) and renamed[2] == 'Ann renamed to Anna'
    assert out['picked'] == [['a:pick:growth', 'debug', 'Ann picked Careful migration in Growth']]
    [changed] = out['changed']
    assert re.fullmatch(r'a:pick:growth:[0-9a-f]{8}', changed[0])
    assert changed[2] == 'Ann changed their pick in Growth from Careful migration to Fast migration'
    assert out['removed'] == [['b:rm', 'info', 'Bob was removed (1 players left)']]
    assert out['sameIds'], 'the same change gets the same id wherever it is seen'
    assert out['detail'] == {'player': 'a', 'round': 'growth', 'option': 'careful'} and out['tag'] == 'WEB'
    assert 'token' not in out['text'] and 'ffffffff' not in out['text']


def test_the_hub_follows_the_roster_over_time_and_serves_players_without_tokens():
    out = run_scenario(r"""
      aws.items.delete(`timeline#${RUN}`);
      const item = () => aws.items.get('arena');
      const set = players => { item().teams_data = s(JSON.stringify(players)); };
      item().phase = s('ready');
      delete item().execution_arn;
      set([{ id: 'a', name: 'Ann', token_hash: 'f'.repeat(64), picks: {} }]);
      const hub = hubFor(() => T);
      await hub.poll();
      set([{ id: 'a', name: 'Anna', token_hash: 'f'.repeat(64), picks: {} }, { id: 'b', name: 'Bob', token_hash: 'e'.repeat(64), picks: {} }]);
      await hub.poll();
      item().phase = s('open'); item().round = s('launch');
      set([{ id: 'a', name: 'Anna', picks: { launch: 'small' } }, { id: 'b', name: 'Bob', picks: { launch: 'large' } }]);
      await hub.poll();
      set([{ id: 'a', name: 'Anna', picks: { launch: 'medium' } }]);
      await hub.poll();
      const text = hub.eventsAfter(undefined, true).filter(e => e.source === 'arena').map(e => e.text);
      const card = hub.storyCards().at(-1);
      result({ text, players: hub.players, card: [card.stage, card.text, card.numbers], blob: JSON.stringify([hub.eventsAfter(undefined, true), hub.storyCards(), hub.players]) });
    """)
    assert sorted(out['text']) == sorted([
        'Ann joined (1 players)', 'Bob joined (2 players)', 'Ann renamed to Anna', 'Anna picked Small server in Launch',
        'Bob picked Large server in Launch', 'Anna changed their pick in Launch from Small server to Medium server',
        'Bob was removed (1 players left)'])
    assert out['players'] == [{'id': 'a', 'name': 'Anna'}]
    assert out['card'][0] == 'open-launch' and out['card'][2] == {'round': 1, 'picked': 1, 'players': 1}
    assert 'token' not in out['blob'] and 'ffffffff' not in out['blob'] and 'eeeeeeee' not in out['blob']


def test_players_route_lists_id_and_name_only_and_needs_the_presenter():
    out = run_scenario(r"""
      fill(); await serve();
      const signedOut = await call('GET', '/api/players');
      const reply = await call('GET', '/api/players', { headers: KEY });
      result({ signedOut: [signedOut.status, signedOut.text], reply: [reply.status, reply.body], text: reply.text });
    """)
    assert out['signedOut'][0] == 403
    for leak in ('Zelda', 'Yuri', 'Xena', 'players'):
        assert leak not in out['signedOut'][1]
    assert out['reply'][0] == 200
    assert out['reply'][1] == {'players': [{'id': 't3', 'name': 'Xena Xerus'}, {'id': 't2', 'name': 'Yuri Yak'}, {'id': 't1', 'name': 'Zelda Zebra'}]}
    assert 'token' not in out['text'] and 'picks' not in out['text']


def test_story_cards_stream_as_story_events_without_ids_and_are_replaced_when_numbers_change():
    out = run_scenario(r"""
      fill(); await serve();
      const stream = await open('/stream?debug=1');
      await stream.until(text => text.includes('event: story'));
      const cardsOf = () => stream.messages().filter(m => m.event === 'story');
      const first = cardsOf().map(m => JSON.parse(m.data));
      const growth = first.find(card => card.stage === 'open-growth');
      const teamsNow = JSON.parse(aws.items.get('arena').teams_data.S);
      teamsNow[1].picks = { growth: 'fast' };
      aws.items.get('arena').teams_data = s(JSON.stringify(teamsNow));
      await stream.until(() => cardsOf().map(m => JSON.parse(m.data)).some(card => card.stage === 'open-growth' && card.numbers.picked === 3));
      const all = cardsOf();
      const poll = await call('GET', '/events', { headers: KEY });
      result({ stages: first.map(card => card.stage), picked: growth.numbers.picked, hasId: all.some(m => 'id' in m),
               later: all.map(m => JSON.parse(m.data)).filter(card => card.stage === 'open-growth').map(card => [card.id, card.numbers.picked]),
               polled: poll.body.story.map(card => [card.id, card.stage]) });
    """)
    assert out['stages'][0] == 'lobby' and out['stages'][-1] == 'open-growth' and out['picked'] == 2
    assert out['hasId'] is False, 'story frames must not move the Last-Event-ID cursor'
    assert out['later'][0] == ['st:01234567:open-growth', 2] and out['later'][-1] == ['st:01234567:open-growth', 3]
    assert ['st:01234567:open-growth', 'open-growth'] in out['polled'], 'the polling fallback carries the same cards'


def test_a_missing_after_is_the_start_of_the_window_never_the_cursor_zero():
    out = run_scenario(r"""
      fill(); await serve();
      // A missing query parameter arrives as null, and Number(null) is 0.
      const missing = [parseCursor(null), parseCursor(undefined, null, ''), parseCursor('   '), parseCursor('abc'), parseCursor('-5'), parseCursor(null, null)];
      const given = { zero: parseCursor('0'), after: parseCursor(null, '1234'), header: parseCursor('99', '1234'), skipsBad: parseCursor('abc', '7') };
      const hub = hubFor(() => T);
      await hub.poll();
      const everything = hub.eventsAfter(undefined, true).map(e => e.id);
      const viaNull = hub.eventsAfter(parseCursor(null), true).map(e => e.id);
      const ids = async path => (await call('GET', path, { headers: KEY })).body.events.map(e => e.id);
      const none = await ids('/events?debug=1'), empty = await ids('/events?debug=1&after='), zero = await ids('/events?debug=1&after=0'), junk = await ids('/events?debug=1&after=abc');
      const stream = await open('/stream?debug=1');
      await stream.until(text => text.includes('event: status'));
      result({ missing, given, same: JSON.stringify(everything) === JSON.stringify(viaNull),
               http: [none.length > 30, JSON.stringify(none) === JSON.stringify(empty), JSON.stringify(none) === JSON.stringify(junk), JSON.stringify(none) === JSON.stringify(zero)],
               stream: stream.events().length >= none.length });
    """)
    assert out['missing'] == [None] * 6
    assert out['given'] == {'zero': 0, 'after': 1234, 'header': 99, 'skipsBad': 7}
    assert out['same']
    assert out['http'] == [True, True, True, True] and out['stream']
