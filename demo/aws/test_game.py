"""The Web Function end to end: web.mjs runs for real over HTTP against an in-memory DynamoDB.

The AWS SDK modules are replaced with stubs (no AWS call is made). The fake table evaluates the very
condition and update expressions web.mjs sends, and refuses an expression whose placeholders do not
match its values, as DynamoDB does. The Durable workflow is played by the tests, which write the
`open` phase, round, callback id and results items the way workflow.py does.
"""

import json
import socket
import subprocess
from pathlib import Path

AWS_DIR = Path(__file__).parent

PRELUDE = r"""
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';

const STUB = `
export const sdk = () => globalThis.__aws;
${['DynamoDBClient', 'LambdaClient', 'SSMClient'].map(name => `export class ${name} { send(command, options) { return sdk().send(command, options); } }`).join('\n')}
${['GetItemCommand', 'PutItemCommand', 'UpdateItemCommand', 'GetDurableExecutionCommand', 'InvokeCommand',
  'ListDurableExecutionsByFunctionCommand', 'SendDurableExecutionCallbackSuccessCommand', 'GetParametersCommand']
  .map(name => `export class ${name} { constructor(input) { this.input = input; } }`).join('\n')}
export default { toString: async text => '<svg>' + text + '</svg>' };
`;
registerHooks({
  resolve(specifier, context, next) {
    return specifier.startsWith('@aws-sdk/') || specifier === 'qrcode' ? { url: 'stub:aws', shortCircuit: true } : next(specifier, context);
  },
  load(url, context, next) {
    return url === 'stub:aws' ? { format: 'module', source: STUB, shortCircuit: true } : next(url, context);
  },
});

// ---- an in-memory DynamoDB that understands the expressions web.mjs sends
const value = attribute => (attribute.S ?? Number(attribute.N));
function condition(expression, item, values) {
  const tokens = expression.match(/attribute_not_exists|attribute_exists|<=|>=|<>|[()=<>,]|:\w+|\w+/g);
  let at = 0;
  const peek = () => tokens[at], take = () => tokens[at++];
  const operand = token => (token.startsWith(':') ? values[token] : item[token]);
  const or = () => { let result = and(); while (peek() === 'OR') { take(); const right = and(); result = result || right; } return result; };
  const and = () => { let result = term(); while (peek() === 'AND') { take(); const right = term(); result = result && right; } return result; };
  function term() {
    const token = take();
    if (token === '(') { const result = or(); take(); return result; }
    if (token.startsWith('attribute_')) { take(); const name = take(); take(); return (name in item) === (token === 'attribute_exists'); }
    let left;
    if (token === 'size') { take(); const name = take(); take(); left = item[name] && { N: String(item[name].L?.length ?? item[name].S.length) }; }
    else left = operand(token);
    const op = take();
    if (op === 'IN') {
      take();
      const list = [];
      for (let next = take(); next !== ')'; next = take()) if (next !== ',') list.push(operand(next));
      return left !== undefined && list.some(entry => value(entry) === value(left));
    }
    const right = operand(take());
    if (left === undefined || right === undefined) return false;
    const [a, b] = [value(left), value(right)];
    return { '=': a === b, '<>': a !== b, '<=': a <= b, '>=': a >= b, '<': a < b, '>': a > b }[op];
  }
  return or();
}
function applyUpdate(expression, item, values) {
  for (const [, verb, rest] of expression.matchAll(/\b(SET|REMOVE|ADD)\b([^]*?)(?=\b(?:SET|REMOVE|ADD)\b|$)/g)) {
    const top = [];
    for (let depth = 0, from = 0, at = 0; at <= rest.length; at++) {
      if (rest[at] === '(') depth++;
      else if (rest[at] === ')') depth--;
      else if (at === rest.length || (rest[at] === ',' && depth === 0)) { top.push(rest.slice(from, at)); from = at + 1; }
    }
    const evaluate = source => {
      const call = /^(list_append|if_not_exists)\((.*)\)$/.exec(source);
      if (call) {
        const args = []; let depth = 0, from = 0;
        for (let at = 0; at <= call[2].length; at++) {
          if (call[2][at] === '(') depth++;
          else if (call[2][at] === ')') depth--;
          else if (at === call[2].length || (call[2][at] === ',' && depth === 0)) { args.push(evaluate(call[2].slice(from, at).trim())); from = at + 1; }
        }
        return call[1] === 'list_append' ? { L: [...args[0].L, ...args[1].L] } : (item[call[2].split(',')[0].trim()] ?? args[1]);
      }
      return source.startsWith(':') ? values[source] : item[source];
    };
    for (const part of top.map(text => text.trim()).filter(Boolean)) {
      if (verb === 'SET') {
        const at = part.indexOf('=');
        item[part.slice(0, at).trim()] = structuredClone(evaluate(part.slice(at + 1).trim()));
      } else if (verb === 'REMOVE') delete item[part];
      else { const [name, token] = part.split(/\s+/); item[name] = { N: String(Number(item[name]?.N ?? 0) + Number(values[token].N)) }; }
    }
  }
}
const RESERVED = new Set(['COUNT', 'DATA', 'DATE', 'DAY', 'HOUR', 'KEY', 'LEVEL', 'LINES', 'MINUTE', 'MONTH', 'NAME', 'OWNER',
  'REGION', 'ROLE', 'SECOND', 'SIZE', 'STATE', 'STATUS', 'TIMESTAMP', 'TYPE', 'USER', 'VALUE', 'VALUES', 'YEAR', 'ZONE']);
export const items = new Map();
export const log = { invokes: [], callbacks: [], results_reads: 0 };
export const aws = {
  status: 'RUNNING', callbackError: undefined,
  async send(command) {
    let { input } = command;
    const name = command.constructor.name;
    if (aws.failTimeline && name === 'UpdateItemCommand' && input.Key.pk.S.startsWith('timeline#')) throw Object.assign(new Error('x'), { name: 'ProvisionedThroughputExceededException' });
    if (name === 'InvokeCommand') { log.invokes.push(input); return { DurableExecutionArn: 'arn:aws:lambda:ap-southeast-2:1:function:wf:1/durable-execution/run/abc' }; }
    if (name === 'GetDurableExecutionCommand') return { Status: aws.status };
    if (name === 'SendDurableExecutionCallbackSuccessCommand') {
      if (aws.callbackError) throw Object.assign(new Error('x'), { name: aws.callbackError });
      log.callbacks.push({ id: input.CallbackId, payload: JSON.parse(Buffer.from(input.Result).toString()) });
      return {};
    }
    if (name === 'GetParametersCommand') return { Parameters: [{ Name: 'p-key', Value: 'k'.repeat(64) }, { Name: 'p-pass', Value: 'a long passphrase' }] };
    const pk = (input.Key ?? input.Item).pk.S, current = items.get(pk);
    if (name === 'GetItemCommand') { if (pk.startsWith('results#')) log.results_reads++; return { Item: current && structuredClone(current) }; }
    const values = input.ExpressionAttributeValues ?? {};
    const names = input.ExpressionAttributeNames ?? {};
    const raw = `${input.ConditionExpression ?? ''} ${input.UpdateExpression ?? ''}`;
    // DynamoDB refuses a reserved word used as a bare attribute name (a function call such as size( is fine),
    // an undefined #name, and a #name that is defined but never used.
    for (const word of raw.match(/(?<![:#\w])[A-Za-z_]\w*(?!\w)(?!\s*\()/g) ?? []) {
      if (RESERVED.has(word.toUpperCase())) throw new Error(`Invalid expression: Attribute name is a reserved keyword; reserved keyword: ${word}`);
    }
    assert.deepEqual([...new Set(raw.match(/#\w+/g) ?? [])].sort(), Object.keys(names).sort(), 'name placeholders must match names');
    const text = raw.replace(/#\w+/g, placeholder => names[placeholder]);
    const rewrite = expression => expression && expression.replace(/#\w+/g, placeholder => names[placeholder]);
    input = { ...input, ConditionExpression: rewrite(input.ConditionExpression), UpdateExpression: rewrite(input.UpdateExpression) };
    assert.deepEqual([...new Set(text.match(/:\w+/g) ?? [])].sort(), Object.keys(values).sort(), 'placeholders must match values');
    if (input.ConditionExpression && !condition(input.ConditionExpression, current ?? {}, values)) {
      throw Object.assign(new Error('The conditional request failed'), { name: 'ConditionalCheckFailedException' });
    }
    if (name === 'PutItemCommand') { items.set(pk, structuredClone(input.Item)); return {}; }
    const next = current ? structuredClone(current) : { pk: input.Key.pk };
    applyUpdate(input.UpdateExpression, next, values);
    items.set(pk, next);
    return { Attributes: input.ReturnValues === 'ALL_NEW' ? structuredClone(next) : undefined };
  },
};
globalThis.__aws = aws;

Object.assign(process.env, {
  RUN_TABLE: 'runs', WORKFLOW_ARN: 'arn:aws:lambda:ap-southeast-2:1:function:wf:1', AWS_LAMBDA_HTTP_ENDPOINT: '127.0.0.1:__PORT__',
  PUBLIC_HOST: 'game.example.test', PRESENTER_PARAMETER: 'p-key', PASSPHRASE_PARAMETER: 'p-pass',
});
await import('./web.mjs');
const { ROUNDS, BOTS } = await import('./game.mjs');

// ---- helpers
const s = text => ({ S: text });
const call = async (method, path, { headers = {}, body } = {}) => {
  const response = await fetch(`http://127.0.0.1:__PORT__${path}`, {
    method, headers: { ...(body === undefined ? {} : { 'content-type': 'application/json' }), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch {}
  return { status: response.status, body: parsed, text };
};
const PRESENTER = { 'x-presenter-key': 'k'.repeat(64), 'x-rehearsal-action': '1' };
const as = player => (player ? { 'x-team-id': player.id, 'x-team-token': player.token } : {});
const state = player => call('GET', '/api/state', { headers: as(player) });
const hostState = async () => (await call('GET', '/api/host/state', { headers: PRESENTER })).body;
const hostPost = async (action, { round, run, headers = PRESENTER, body } = {}) => call('POST', `/api/host/${action}`, {
  headers: { ...headers, ...(run === undefined ? { 'x-rehearsal-run': (await hostState()).action_token } : { 'x-rehearsal-run': run }),
    ...(round ? { 'x-rehearsal-round': round } : {}) },
  body,
});
const join = async name => {
  const response = await call('POST', '/api/join', { body: name === undefined ? {} : { name } });
  return response.status === 200 ? { ...response.body, status: 200 } : response;
};
const pick = (player, round, option) => call('POST', '/api/pick', { body: { id: player.id, token: player.token, round, option } });
const arena = () => items.get('arena');
// What workflow.py does when it opens a round.
const openRound = (round, extra = {}) => {
  Object.assign(arena(), { phase: s('open'), round: s(round), callback_id: s(`cb-${round}`), ...extra });
  delete arena().lease_until;
};
const publishResults = (round, rows, market = 100) => {
  items.set(`results#${arena().run_id.S}#${round}`, {
    pk: s(`results#${arena().run_id.S}#${round}`), run_id: arena().run_id, round: s(round),
    data: s(JSON.stringify({ round, market, summary: { market, options: {} }, rows })),
  });
  arena().results_round = s(round);
};
const row = (id, name, rank) => ({
  id, name, house: false, bot: false, rank, option: 'medium', roll: 12, risk: 0, hit: false, outcome: 'held', event: 'Demand was normal this round',
  sales: 120, slow: 10, failed: 5, cost_cents: 3000, refund_cents: 0, profit_cents: 120000, cumulative_cents: 250000 - rank, returning: 80, evidence: 'modelled',
});
await state();   // creates the arena item
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
    return done.stdout


ACCESS = '{"bf":"access"'


def scenario(source):
    """The scenario's own output: the Web Function's access lines are asserted separately."""
    return ''.join(line + '\n' for line in run_scenario(source).splitlines() if not line.startswith(ACCESS))


def test_generated_names_are_unique_and_never_a_bot_and_the_101st_join_is_refused():
    out = scenario(r"""
      const seen = new Set();
      const bots = new Set(BOTS.map(name => name.toLowerCase()));
      for (let count = 0; count < 100; count++) {
        const player = await join();
        assert.equal(player.status, 200);
        assert.equal(typeof player.token, 'string');
        assert.ok(/^[A-Z][a-z]+ [A-Z][a-z]+( \d+)?$/.test(player.name), player.name);
        assert.ok([...player.name].length <= 20, player.name);
        assert.ok(!seen.has(player.name.toLowerCase()) && !bots.has(player.name.toLowerCase()), player.name);
        seen.add(player.name.toLowerCase());
      }
      const full = await join();
      assert.equal(full.status, 409);
      assert.equal((await join('Somebody')).status, 409);
      assert.equal((await state()).body.player_count, 100);
      assert.equal((await state()).body.joinable, true, 'joinable by phase; the cap refuses the join itself');
      // A typed name that collides is refused, a bot's name is reserved, blank means generate.
      const reset = await hostPost('reset');
      assert.equal(reset.status, 200);
      assert.equal((await join('Alice')).status, 200);
      assert.equal((await join('alice')).status, 409);
      assert.equal((await join('Bot Lambo')).status, 409);
      assert.equal((await join('   ')).name.includes(' '), true);
      assert.equal((await call('POST', '/api/join', { body: { name: 42 } })).status, 400);
    """)
    assert out == ''


def test_state_size_at_100_players_stays_under_30_kb_and_phones_get_a_board_not_a_roster():
    out = scenario(r"""
      const players = [];
      for (let count = 0; count < 100; count++) players.push(await join());
      assert.equal((await hostPost('start')).status, 200);
      openRound('launch', { launch_data: s(JSON.stringify({ source: 'real', checkout_ms: 180, vm_id: 'vm-1' })),
        evidence_data: s(JSON.stringify({ careful: { cart_kept: true, source: 'real' }, fast: { cart_kept: false, source: 'recorded' } })) });
      for (const player of players) assert.equal((await pick(player, 'launch', 'medium')).status, 200);
      openRound('growth', { results_round: s('launch') });
      publishResults('launch', players.map((player, index) => row(player.id, player.name, index + 1)));
      const me = players[57];
      const phone = await state(me), host = await call('GET', '/api/host/state', { headers: PRESENTER });
      const bytes = value => Buffer.byteLength(value.text);
      console.log(JSON.stringify({ phone_bytes: bytes(phone), host_bytes: bytes(host) }));
      assert.ok(bytes(phone) < 30000 && bytes(host) < 30000, `${bytes(phone)} / ${bytes(host)}`);
      // The phone: no roster, no tokens, no ARN, no action token; a board of the top 10 plus its own row.
      for (const word of ['token_hash', 'execution_arn', 'action_token']) assert.ok(!phone.text.includes(word), word);
      for (const word of ['players', 'execution_arn', 'action_token', 'retry_action']) assert.ok(!(word in phone.body), word);
      assert.equal(phone.body.me.name, me.name);
      assert.deepEqual(phone.body.me.picks, { launch: 'medium' });
      const { results } = phone.body;
      assert.equal(results.board.rows.length, 11);
      assert.equal(results.board.total, 100);
      assert.equal(results.board.more, 89);
      assert.equal(results.board.rows.at(-1).id, me.id);
      assert.equal(results.mine.id, me.id);
      assert.equal(results.round, 'launch');
      assert.equal(phone.body.round, 'growth');
      assert.equal(phone.body.round_number, 2);
      assert.equal(phone.body.rounds.length, 4);
      assert.deepEqual(Object.keys(phone.body.evidence), ['launch', 'careful', 'fast']);
      assert.equal((await state()).body.results.mine, null);
      // The host sees who has picked for the open round, never a token hash.
      assert.equal(host.body.players.length, 100);
      assert.equal(host.body.players.filter(player => player.picked).length, 0, 'nobody has picked growth yet');
      assert.ok(!host.text.includes('token_hash'));
      assert.equal(host.body.public_url, 'https://game.example.test');
      assert.equal(host.body.auth_mode, 'key');
      assert.match(host.body.execution_arn, /^arn:/);
      assert.equal(host.body.action_token, host.body.id);
    """)
    sizes = out.strip().splitlines()[-1]
    print(sizes)
    assert 'phone_bytes' in sizes


def test_join_and_rename_windows_follow_the_round():
    scenario(r"""
      const first = await join('Early');
      assert.equal((await state()).body.phase, 'ready');
      assert.equal((await state()).body.renamable, true);
      assert.equal((await hostPost('start')).status, 200);
      assert.equal((await state()).body.phase, 'starting');
      assert.equal((await join('During start')).status, 200, 'joining stays open while the game starts');
      openRound('launch');
      assert.equal((await join('Launch joiner')).status, 200);
      assert.equal((await call('POST', '/api/rename', { body: { id: first.id, token: first.token, name: 'Renamed' } })).body.name, 'Renamed');
      assert.equal((await call('POST', '/api/rename', { body: { id: first.id, token: 'bad', name: 'Nope' } })).status, 403);
      assert.equal((await call('POST', '/api/rename', { body: { id: first.id, token: first.token, name: '' } })).body.name.includes(' '), true);
      const closed = await hostPost('close', { round: 'launch' });
      assert.equal(closed.status, 200);
      assert.equal(closed.body.phase, 'closing');
      assert.equal((await join('Too late')).status, 409);
      assert.equal((await state()).body.joinable, false);
      // Round 2 opens: joining still works, renaming no longer does.
      openRound('growth');
      const state2 = (await state(first)).body;
      assert.equal(state2.joinable, true);
      assert.equal(state2.renamable, false);
      const late = await join('Growth joiner');
      assert.equal(late.status, 200);
      assert.equal((await call('POST', '/api/rename', { body: { id: late.id, token: late.token, name: 'Late rename' } })).status, 409);
      // Growth closes: joining is over for good, even when round 3 opens.
      assert.equal((await hostPost('close', { round: 'growth' })).status, 200);
      assert.equal((await join('Way too late')).status, 409);
      openRound('blackfriday');
      assert.equal((await join('Way too late')).status, 409);
      assert.equal((await state()).body.joinable, false);
      assert.equal((await state()).body.round_number, 3);
    """)


def test_picks_are_bound_to_the_open_round_and_to_requires():
    scenario(r"""
      const player = await join('Picker');
      assert.equal((await hostPost('start')).status, 200);
      openRound('launch');
      const closedRound = await pick(player, 'growth', 'careful');
      assert.equal(closedRound.status, 409, 'growth is not open yet');
      assert.equal((await pick(player, 'launch', 'huge')).status, 400);
      assert.equal((await pick(player, 'nonsense', 'small')).status, 400);
      assert.equal((await call('POST', '/api/pick', { body: { id: player.id, token: 'x', round: 'launch', option: 'small' } })).status, 403);
      assert.equal((await pick(player, 'launch', 'large')).status, 200);
      assert.equal((await state(player)).body.me.picks.launch, 'large');
      assert.equal((await pick(player, 'launch', 'small')).status, 200, 'a pick can be changed while its round is open');
      openRound('growth');
      assert.equal((await pick(player, 'launch', 'large')).status, 409, 'launch has closed');
      assert.equal((await pick(player, 'growth', 'fast')).status, 200);
      openRound('blackfriday');
      assert.equal((await pick(player, 'growth', 'stay')).status, 409);
      assert.equal((await pick(player, 'blackfriday', 'autoscale')).status, 200, 'fast satisfies requires');
      const other = await join('Stayer').catch(() => null);
      assert.equal(other?.status, 409, 'cannot join in round 3');
    """)
    scenario(r"""
      const player = await join('Cautious');
      openRound('growth');
      assert.equal((await pick(player, 'growth', 'stay')).status, 200);
      openRound('blackfriday');
      const refused = await pick(player, 'blackfriday', 'autoscale');
      assert.equal(refused.status, 409);
      assert.match(refused.body.error, /shared database/i);
      assert.equal((await pick(player, 'blackfriday', 'queue')).status, 200);
      const state3 = (await state(player)).body;
      assert.deepEqual(state3.me.picks, { growth: 'stay', blackfriday: 'queue' });
    """)


def test_close_is_presenter_only_round_bound_single_use_and_sends_the_frozen_roster():
    scenario(r"""
      const a = await join('Anna'), b = await join('Ben');
      assert.equal((await hostPost('start')).status, 200);
      openRound('launch');
      assert.equal((await pick(a, 'launch', 'large')).status, 200);
      const run = (await hostState()).action_token;
      // Presenter only: nothing, a phone's credentials, and a missing action header are all refused.
      assert.equal((await call('POST', '/api/host/close', { headers: { 'x-rehearsal-run': run, 'x-rehearsal-round': 'launch' } })).status, 403);
      assert.equal((await call('POST', '/api/host/close', { headers: { ...as(a), 'x-rehearsal-action': '1', 'x-rehearsal-run': run, 'x-rehearsal-round': 'launch' } })).status, 403);
      assert.equal((await call('POST', '/api/host/close', { headers: { 'x-presenter-key': 'k'.repeat(64), 'x-rehearsal-run': run, 'x-rehearsal-round': 'launch' } })).status, 403);
      // Wrong round, no round, wrong run token: refused, nothing changes.
      assert.equal((await hostPost('close', { round: 'growth' })).status, 409);
      assert.equal((await hostPost('close', { round: 'nonsense' })).status, 409);
      assert.equal((await hostPost('close')).status, 409);
      assert.equal((await hostPost('close', { round: 'launch', run: 'wrong' })).status, 409);
      assert.equal(arena().phase.S, 'open');
      assert.equal(log.callbacks.length, 0);
      // Close: one callback with the roster as it stood, no token hashes.
      const closed = await hostPost('close', { round: 'launch' });
      assert.equal(closed.status, 200);
      assert.equal(closed.body.phase, 'closing');
      assert.equal(log.callbacks.length, 1);
      assert.equal(log.callbacks[0].id, 'cb-launch');
      const { payload } = log.callbacks[0];
      assert.equal(payload.action, 'close');
      assert.equal(payload.round, 'launch');
      assert.deepEqual(payload.players.map(player => player.name), ['Anna', 'Ben']);
      assert.deepEqual(payload.players[0], { id: a.id, name: 'Anna', house: false, option: 'large', picks: { launch: 'large' } });
      assert.deepEqual(payload.players[1], { id: b.id, name: 'Ben', house: false, picks: {} });
      assert.ok(!JSON.stringify(payload).includes('token'));
      assert.equal(arena().frozen_data.S, arena().teams_data.S);
      assert.equal(arena().lease_until, undefined, 'the lease is handed back once AWS confirmed');
      // A second Close and a late pick change nothing.
      assert.equal((await hostPost('close', { round: 'launch' })).status, 409);
      assert.equal((await pick(b, 'launch', 'small')).status, 409);
      assert.equal(log.callbacks.length, 1);
      assert.equal(JSON.parse(arena().frozen_data.S)[1].picks.launch, undefined);
      // The workflow moved on to the next round: a Close for the old round is refused.
      openRound('growth', { callback_id: s('cb-new') });
      arena().callback_id = s('cb-newer');
      assert.equal((await hostPost('close', { round: 'launch' })).status, 409, 'launch is no longer the open round');
    """)


def test_a_close_whose_callback_was_not_confirmed_is_retried_after_its_lease_with_the_same_roster():
    scenario(r"""
      const a = await join('Retry');
      assert.equal((await hostPost('start')).status, 200);
      openRound('launch');
      await pick(a, 'launch', 'large');
      aws.callbackError = 'ThrottlingException';
      const first = await hostPost('close', { round: 'launch' });
      assert.equal(first.status, 200);
      assert.equal(first.body.phase, 'closing');
      assert.equal(first.body.retry_action, undefined, 'the lease is still held');
      assert.ok(arena().lease_until, 'the lease stays until AWS confirms');
      assert.equal((await hostPost('close', { round: 'launch' })).status, 409, 'no retry while leased');
      const frozen = arena().frozen_data.S;
      arena().lease_until = { N: String(Date.now() - 1) };
      const lapsed = await hostState();
      assert.equal(lapsed.retry_action, 'close');
      assert.equal((await state()).body.retry_action, undefined);
      aws.callbackError = undefined;
      assert.equal((await hostPost('close', { round: 'growth' })).status, 409, 'the retry is round-bound too');
      assert.equal((await hostPost('close', { round: 'launch' })).status, 200);
      assert.equal(log.callbacks.length, 1);
      assert.equal(log.callbacks[0].payload.players[0].option, 'large');
      assert.equal(arena().frozen_data.S, frozen);
      assert.equal(arena().lease_until, undefined);
      // AWS says the callback is already consumed or expired: not an error, the workflow decides.
      openRound('growth');
      aws.callbackError = 'CallbackTimeoutException';
      assert.equal((await hostPost('close', { round: 'growth' })).status, 200);
      assert.equal(arena().lease_until, undefined);
    """)


def test_reset_works_in_every_phase_with_the_presenters_token_and_only_then():
    scenario(r"""
      const phases = [['starting', null], ['open', 'launch'], ['closing', 'launch'], ['final', 'christmas'], ['failed', null]];
      for (const [phase, round] of phases) {
        const player = await join(`In ${phase}`);
        assert.equal(player.status, 200);
        const before = (await hostState()).action_token;
        Object.assign(arena(), { phase: s(phase) });
        if (round) arena().round = s(round);
        assert.equal((await hostPost('reset', { run: 'stale' })).status, 409, phase);
        assert.equal((await hostPost('reset', { run: before, headers: { 'x-rehearsal-action': '1' } })).status, 403, 'presenter only');
        const reset = await hostPost('reset', { run: before });
        assert.equal(reset.status, 200, phase);
        assert.equal(reset.body.phase, 'ready');
        assert.notEqual(reset.body.id, before);
        assert.equal(reset.body.player_count, 0);
        assert.equal((await pick(player, 'launch', 'small')).status, 403, 'the old token no longer names a player');
      }
    """)


def test_the_one_round_routes_are_gone_and_every_host_route_needs_the_presenter():
    scenario(r"""
      for (const path of ['/api/launch', '/api/migrate', '/api/compare', '/api/reset']) {
        for (const headers of [{}, { ...PRESENTER, 'x-rehearsal-run': (await hostState()).action_token }]) {
          assert.equal((await call('POST', path, { headers })).status, 404, path);
        }
      }
      assert.equal((await call('GET', '/api/launch')).status, 404);
      assert.equal((await call('POST', '/api/host/nonsense', { headers: PRESENTER })).status, 404);
      for (const [method, path] of [['GET', '/api/host/state'], ['GET', '/api/host/qr.svg'], ['POST', '/api/host/start'],
          ['POST', '/api/host/close'], ['POST', '/api/host/reset'], ['POST', '/api/host/remove']]) {
        assert.equal((await call(method, path, { headers: { 'x-rehearsal-action': '1' } })).status, 403, path);
      }
      const qr = await call('GET', '/api/host/qr.svg', { headers: PRESENTER });
      assert.equal(qr.status, 200);
      assert.ok(qr.text.includes('https://game.example.test'));
      for (const path of ['/api/state', '/', '/host', '/round.js']) {
        assert.equal((await call('GET', path)).status, 200, path);
      }
      // Start has no body and needs this run's token, and works once.
      assert.equal((await hostPost('start', { run: 'wrong' })).status, 409);
      assert.equal((await hostPost('start', { body: {} })).status, 413);
      assert.equal((await hostPost('start')).status, 200);
      assert.equal(log.invokes.length, 1);
      assert.equal((await hostPost('start')).status, 409);
      assert.equal(arena().execution_arn.S.startsWith('arn:'), true);
    """)


def test_remove_is_a_presenter_action_for_roster_phases_only():
    scenario(r"""
      const a = await join('Stays'), b = await join('Goes');
      assert.equal((await hostPost('remove', { body: { id: b.id } })).status, 200);
      assert.equal((await hostState()).players.length, 1);
      assert.equal((await hostPost('remove', { body: { id: b.id } })).status, 404);
      assert.equal((await hostPost('remove', { run: 'wrong', body: { id: a.id } })).status, 409);
      assert.equal((await call('POST', '/api/host/remove', { headers: { 'x-rehearsal-action': '1' }, body: { id: a.id } })).status, 403);
      assert.equal((await pick(b, 'launch', 'small')).status, 403);
      assert.equal((await hostPost('start')).status, 200);
      openRound('launch');
      assert.equal((await pick(a, 'launch', 'small')).status, 200);
      assert.equal((await hostState()).players[0].picked, true);
      assert.equal((await hostPost('close', { round: 'launch' })).status, 200);
      assert.equal((await hostPost('remove', { body: { id: a.id } })).status, 409, 'not while a round is closing');
      openRound('growth');
      assert.equal((await hostState()).players[0].picked, false, 'picked is per open round');
      assert.equal((await hostPost('remove', { body: { id: a.id } })).status, 200);
      assert.equal((await state(a)).body.me, null);
    """)


def test_results_are_read_once_per_key_and_cached_and_a_missing_item_is_retried():
    scenario(r"""
      const a = await join('Reader');
      assert.equal((await hostPost('start')).status, 200);
      openRound('growth', { results_round: s('launch') });
      assert.equal((await state(a)).body.results, null, 'not published yet: no results, no error');
      publishResults('launch', [row(a.id, a.name, 1), row('other1', 'Other', 2)], 112);
      const before = log.results_reads;
      const answers = await Promise.all(Array.from({ length: 20 }, () => state(a)));
      assert.ok(answers.every(answer => answer.body.results.market === 112));
      assert.equal(log.results_reads - before, 1, 'twenty simultaneous phones, one read');
      await state(a); await state();
      assert.equal(log.results_reads - before, 1, 'and never again for that key');
      assert.equal((await state(a)).body.results.board.more, 0);
      openRound('blackfriday', { results_round: s('growth') });
      publishResults('growth', [row(a.id, a.name, 1)], 99);
      assert.equal((await state(a)).body.results.round, 'growth');
      assert.equal((await state(a)).body.results.market, 99);
      assert.equal(log.results_reads - before, 2);
      Object.assign(arena(), { phase: s('final'), results_round: s('christmas') });
      publishResults('christmas', [row(a.id, a.name, 1)], 90);
      const done = (await state(a)).body;
      assert.equal(done.phase, 'final');
      assert.equal(done.round_number, 4);
      assert.equal(done.joinable, false);
      assert.equal(done.results.round, 'christmas');
    """)


def test_a_failed_or_timed_out_execution_is_reported_and_closes_the_game():
    scenario(r"""
      assert.equal((await hostPost('start')).status, 200);
      openRound('launch');
      assert.equal((await state()).body.phase, 'open');
      await new Promise(resolve => setTimeout(resolve, 2100));   // the execution status is cached for two seconds
      aws.status = 'FAILED';
      const failed = (await state()).body;
      assert.equal(failed.phase, 'failed');
      assert.equal(failed.joinable, false);
      assert.match(failed.message, /did not complete/);
      Object.assign(arena(), { phase: s('expired'), expired_action: s('close'), round: s('growth') });
      assert.equal((await state()).body.phase, 'expired');
      assert.match((await state()).body.message, /expired/);
    """)


def test_the_timeline_route_is_presenter_only_and_phones_never_see_it():
    scenario(r"""
      const run = arena().run_id.S;
      const a = await join('Anna');
      for (const headers of [{}, { 'x-rehearsal-action': '1' }, as(a), { 'x-presenter-key': 'wrong' }]) {
        assert.equal((await call('GET', '/api/host/timeline', { headers })).status, 403);
      }
      const empty = await call('GET', '/api/host/timeline', { headers: PRESENTER });
      assert.equal(empty.status, 200);
      assert.deepEqual(empty.body, { run_id: run, lines: [] });
      assert.equal((await hostPost('start')).status, 200);
      const shown = await call('GET', '/api/host/timeline', { headers: PRESENTER });
      assert.equal(shown.body.run_id, run);
      assert.deepEqual(shown.body.lines.map(line => line.id), ['web-start']);
      assert.equal(shown.body.lines[0].tag, 'WEB');
      assert.equal(shown.body.lines[0].level, 'info');
      assert.deepEqual(Object.keys(shown.body.lines[0]).sort(), ['detail', 'id', 'level', 'tag', 'text', 't'].sort());
      assert.deepEqual(shown.body.lines[0].detail, { players: 1, run_id: run });
      assert.ok(shown.body.lines[0].text.length <= 100 && Number.isInteger(shown.body.lines[0].t));
      for (const response of [await state(a), await state(), await call('GET', '/api/host/state', { headers: PRESENTER })]) {
        assert.ok(!/timeline|web-start|lines/.test(response.text ?? JSON.stringify(response.body)), 'no timeline data in state');
      }
    """)


def test_start_close_and_reset_each_append_exactly_one_web_line():
    scenario(r"""
      const lines = run => (items.get(`timeline#${run}`)?.lines.L ?? []).map(entry => JSON.parse(entry.S));
      const first = arena().run_id.S;
      const a = await join('Anna'), b = await join('Ben');
      assert.equal((await hostPost('start', { run: 'wrong' })).status, 409);
      assert.deepEqual(lines(first), [], 'a refused Start writes nothing');
      assert.equal((await hostPost('start')).status, 200);
      assert.equal((await hostPost('start')).status, 409);
      assert.deepEqual(lines(first).map(line => line.id), ['web-start']);
      openRound('launch');
      assert.equal((await pick(a, 'launch', 'large')).status, 200);
      assert.equal((await hostPost('close', { round: 'growth' })).status, 409);
      assert.deepEqual(lines(first).map(line => line.id), ['web-start'], 'a refused Close writes nothing');
      assert.equal((await hostPost('close', { round: 'launch' })).status, 200);
      assert.equal((await hostPost('close', { round: 'launch' })).status, 409);
      const written = lines(first);
      assert.deepEqual(written.map(line => line.id), ['web-start', 'web-close-launch']);
      assert.deepEqual(written[1].detail, { round: 'launch', picked: 1, players: 2 });
      assert.ok(written.every(line => line.tag === 'WEB' && line.level === 'info' && !('log_group' in line)));
      assert.ok(!JSON.stringify(written).match(/token|callback|cb-/i));
      const reset = await hostPost('reset');
      assert.equal(reset.status, 200);
      const second = reset.body.id;
      assert.notEqual(second, first);
      assert.deepEqual(lines(second).map(line => line.id), ['web-reset'], 'Reset writes into the new run');
      assert.deepEqual(lines(second)[0].detail, { run_id: second });
      assert.deepEqual([lines(second)[0].tag, lines(second)[0].level], ['WEB', 'warn'], 'a forced reset is a warning');
      assert.equal(lines(first).length, 2, 'the old run is left alone');
      assert.deepEqual((await call('GET', '/api/host/timeline', { headers: PRESENTER })).body.lines.map(line => line.id), ['web-reset']);
    """)


def test_the_timeline_cap_drops_late_lines_without_an_error():
    scenario(r"""
      const run = arena().run_id.S;
      const full = count => items.set(`timeline#${run}`, { pk: s(`timeline#${run}`), run_id: s(run), lines: { L: Array.from({ length: count }, (_, at) => s(JSON.stringify({ id: `x${at}` }))) } });
      full(299);
      assert.equal((await hostPost('start')).status, 200);
      const last = items.get(`timeline#${run}`).lines.L;
      assert.equal(last.length, 300);
      assert.equal(JSON.parse(last[299].S).id, 'web-start');
      openRound('launch');
      assert.equal((await hostPost('close', { round: 'launch' })).status, 200, 'over the cap: dropped, not an error');
      assert.equal(items.get(`timeline#${run}`).lines.L.length, 300);
      assert.equal(log.callbacks.length, 1);
      assert.equal((await call('GET', '/api/host/timeline', { headers: PRESENTER })).body.lines.length, 300);
    """)


def test_a_failing_timeline_write_changes_no_response():
    scenario(r"""
      aws.failTimeline = true;
      const a = await join('Anna');
      const start = await hostPost('start');
      assert.equal(start.status, 200);
      assert.equal(start.body.phase, 'starting');
      assert.equal(log.invokes.length, 1);
      openRound('launch');
      const closed = await hostPost('close', { round: 'launch' });
      assert.equal(closed.status, 200);
      assert.equal(closed.body.phase, 'closing');
      assert.equal(log.callbacks.length, 1, 'the callback still goes out');
      const reset = await hostPost('reset');
      assert.equal(reset.status, 200);
      assert.equal(reset.body.phase, 'ready');
      assert.ok([...items.keys()].every(pk => !pk.startsWith('timeline#')));
      assert.deepEqual((await call('GET', '/api/host/timeline', { headers: PRESENTER })).body.lines, []);
    """)


def test_pages_ask_for_assets_by_content_hash_and_the_url_serves_them():
    scenario(r"""
      const { readFileSync } = await import('node:fs');
      const asset = name => readFileSync(new URL(`./${name}`, import.meta.url), 'utf8');
      const fetched = path => fetch(`http://${process.env.AWS_LAMBDA_HTTP_ENDPOINT}${path}`).then(async response => ({ response, text: await response.text() }));
      const hashes = [];
      for (const path of ['/', '/host']) {
        const { response, text } = await fetched(path);
        assert.equal(response.status, 200);
        assert.match(response.headers.get('content-security-policy'), /script-src 'self'/);
        assert.equal(response.headers.get('cache-control'), 'no-store');
        const script = /<script src="\/round\.js\?v=([0-9a-f]+)" defer><\/script>/.exec(text);
        const style = /<link rel="stylesheet" href="\/round\.css\?v=([0-9a-f]+)">/.exec(text);
        assert.ok(script && style, `${path} references versioned assets`);
        assert.ok(!text.includes('"/round.js"') && !text.includes('"/round.css"'));
        hashes.push([script[1], style[1]]);
      }
      assert.deepEqual(hashes[0], hashes[1]);
      const [js, css] = hashes[0];
      for (const [path, file, type] of [[`/round.js?v=${js}`, 'round.js', 'text/javascript'], ['/round.js', 'round.js', 'text/javascript'],
          [`/round.css?v=${css}`, 'round.css', 'text/css'], ['/round.css', 'round.css', 'text/css']]) {
        const { response, text } = await fetched(path);
        assert.equal(response.status, 200, path);
        assert.equal(text, asset(file), path);
        assert.ok(response.headers.get('content-type').startsWith(type), path);
        assert.equal(response.headers.get('cache-control'), 'public, max-age=3600', path);
      }
      assert.equal((await fetched('/round.js?v=anything')).response.status, 200);
      assert.equal((await fetched('/nope.js?v=1')).response.status, 404);
    """)


def test_the_fake_table_refuses_a_reserved_word_used_as_a_bare_attribute_name():
    scenario(r"""
      const { UpdateItemCommand } = await import('@aws-sdk/client-dynamodb');
      const update = input => aws.send(new UpdateItemCommand({ TableName: 'runs', Key: { pk: s('probe') }, ...input }));
      for (const word of ['lines', 'data', 'name', 'status', 'count', 'size']) {
        await assert.rejects(update({ UpdateExpression: `SET ${word} = :v`, ExpressionAttributeValues: { ':v': s('x') } }), /reserved keyword/, word);
        await update({ UpdateExpression: 'SET #w = :v', ExpressionAttributeNames: { '#w': word }, ExpressionAttributeValues: { ':v': s('x') } });
      }
      await update({ UpdateExpression: 'SET round = :v, phase = :v', ConditionExpression: 'attribute_not_exists(round) OR size(#l) < :v', ExpressionAttributeNames: { '#l': 'lines' }, ExpressionAttributeValues: { ':v': { N: '5' } } }).catch(error => assert.notEqual(error.name, 'Error'));
      await assert.rejects(update({ UpdateExpression: 'SET #unused = :v', ExpressionAttributeValues: { ':v': s('x') } }), /name placeholders/);
    """)


def test_the_timeline_write_uses_a_name_placeholder_for_lines_and_lands():
    scenario(r"""
      const seen = [];
      const send = aws.send;
      aws.send = async function (command) {
        if (command.constructor.name === 'UpdateItemCommand' && command.input.Key.pk.S.startsWith('timeline#')) seen.push(command.input);
        return send.call(this, command);
      };
      assert.equal((await hostPost('start')).status, 200);
      assert.equal(seen.length, 1);
      assert.deepEqual(seen[0].ExpressionAttributeNames, { '#lines': 'lines' });
      assert.ok(!/(^|[^#\w])lines\b/.test(`${seen[0].UpdateExpression} ${seen[0].ConditionExpression}`));
      assert.equal(items.get(`timeline#${arena().run_id.S}`).lines.L.length, 1);
    """)


def test_every_request_prints_exactly_one_access_line_and_nothing_secret():
    out = run_scenario(r"""
      const ana = await join('Zelda Quokka');
      await hostPost('start');
      openRound('launch');
      assert.equal((await pick(ana, 'launch', 'large')).status, 200);
      assert.equal((await state(ana)).status, 200);
      assert.equal((await call('GET', '/api/state?player=Zelda%20Quokka&token=' + ana.token)).status, 404);
      assert.equal((await call('GET', '/api/host/state', { headers: PRESENTER })).status, 200);
      assert.equal((await call('GET', '/api/host/timeline', { headers: PRESENTER })).status, 200);
      assert.equal((await call('GET', '/api/host/timeline')).status, 403);
      assert.equal((await call('GET', '/')).status, 200);
      assert.equal((await call('GET', '/round.js?v=abc')).status, 200);
      assert.equal((await call('GET', `/nope/${ana.id}?token=${ana.token}`)).status, 404);
      assert.equal((await pick(ana, 'growth', 'large')).status, 409);
      console.log('SECRETS ' + [ana.id, ana.token, ana.run_id, 'Zelda', 'Quokka', 'k'.repeat(64)].join(' '));
    """)
    secrets = next(line for line in out.splitlines() if line.startswith('SECRETS ')).split()[1:]
    lines = [line for line in out.splitlines() if not line.startswith('SECRETS ')]
    assert all(line.startswith('{"bf":"access"') for line in lines), out
    assert not any(secret in out.replace(next(l for l in out.splitlines() if l.startswith('SECRETS ')), '') for secret in secrets)
    entries = [json.loads(line) for line in lines]
    assert all(set(entry) <= {'bf', 'level', 'method', 'route', 'status', 'ms', 'phase', 'round'} for entry in entries)
    assert all({'bf', 'level', 'method', 'route', 'status', 'ms'} <= set(entry) for entry in entries)
    assert all(isinstance(entry['ms'], int) and entry['ms'] >= 0 for entry in entries)
    assert all(entry['level'] in {'debug', 'info', 'warn', 'error'} for entry in entries)
    assert all(entry['method'] in {'GET', 'POST', 'HEAD', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'other'} for entry in entries)
    assert all(entry['route'] == 'other' or entry['route'].startswith(('/api/', '/round.', '/art/')) or entry['route'] in {'/', '/host'}
               for entry in entries)
    assert all(e.get('phase', 'ready') in {'ready', 'starting', 'open', 'closing', 'final', 'expired', 'failed'}
               and e.get('round', 'launch') in {'launch', 'growth', 'blackfriday', 'christmas'} for e in entries)
    summary = [(e['method'], e['route'], e['status'], e['level']) for e in entries]
    assert summary.count(('POST', '/api/join', 200, 'info')) == 1
    assert summary.count(('POST', '/api/pick', 200, 'info')) == 1
    assert summary.count(('POST', '/api/pick', 409, 'warn')) == 1
    assert summary.count(('POST', '/api/host/start', 200, 'info')) == 1
    assert summary.count(('GET', '/api/state', 200, 'debug')) == 2
    assert summary.count(('GET', '/api/host/timeline', 200, 'debug')) == 1
    assert summary.count(('GET', '/api/host/timeline', 403, 'warn')) == 1
    assert summary.count(('GET', '/', 200, 'info')) == 1
    assert summary.count(('GET', '/round.js', 200, 'info')) == 1
    assert summary.count(('GET', 'other', 404, 'warn')) == 2, 'unknown paths and API paths with a query are "other"'
    open_state = [e for e in entries if e['route'] == '/api/state' and e.get('phase') == 'open']
    assert open_state and all(e['round'] == 'launch' for e in open_state)
