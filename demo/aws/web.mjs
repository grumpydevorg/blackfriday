import { AsyncLocalStorage } from 'node:async_hooks';
import { createHash, randomInt, randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { gzipSync } from 'node:zlib';
import { DynamoDBClient, GetItemCommand, PutItemCommand, UpdateItemCommand } from '@aws-sdk/client-dynamodb';
import {
  GetDurableExecutionCommand,
  InvokeCommand,
  LambdaClient,
  ListDurableExecutionsByFunctionCommand,
  SendDurableExecutionCallbackSuccessCommand,
} from '@aws-sdk/client-lambda';
import { SSMClient, GetParametersCommand } from '@aws-sdk/client-ssm';
import {
  PLAYER_CAP, ROUND_IDS, RequestError, board, closePayload, hostPlayers, join, joinable, me, pick, publicRounds, remove,
  rename, renamable,
} from './game.mjs';
import { presenterAuth, routeKind, MAX_FAILURES, WINDOW_MS } from './host_auth.mjs';
import { s, n, writeRoster, closeRound, retryClose, releaseClose } from './roster.mjs';
import QRCode from 'qrcode';

const { RUN_TABLE: table, WORKFLOW_ARN: workflow } = process.env;
if (!table || !/:\d+$/.test(workflow ?? '')) throw new Error('Set RUN_TABLE and a numbered WORKFLOW_ARN');
const db = new DynamoDBClient({});
const lambda = new LambdaClient({});
const key = { pk: { S: 'arena' } };
const startWindow = 90_000;
const callbackLease = 20_000;
const id = () => randomUUID().replaceAll('-', '');
const terminal = new Set(['final', 'expired', 'failed']);
const immutable = { 'Cache-Control': 'public, max-age=3600' };
// The two assets live at fixed URLs with an hour of cache: the pages ask for them by content hash so a
// redeploy cannot pair an old script with new HTML. The bare URL serves the same bytes.
const roundJs = readFileSync(new URL('./round.js', import.meta.url));
const roundCss = readFileSync(new URL('./round.css', import.meta.url));
const digest = bytes => createHash('sha256').update(bytes).digest('hex').slice(0, 10);
const versioned = new Map([['/round.css', digest(roundCss)], ['/round.js', digest(roundJs)]]);
function page(name, file) {
  let html = file.toString();
  for (const [path, hash] of versioned) {
    const reference = new RegExp(`(?:href|src)="${path.replace('.', '\\.')}"`);
    if (!reference.test(html)) throw new Error(`${name} does not reference ${path}`);
    html = html.replace(reference, match => match.replace(path, `${path}?v=${hash}`));
  }
  return Buffer.from(html);
}
const staticFiles = new Map([
  ['/', ['text/html; charset=utf-8', page('player.html', readFileSync(new URL('./player.html', import.meta.url)))]],
  ['/host', ['text/html; charset=utf-8', page('host.html', readFileSync(new URL('./host.html', import.meta.url)))]],
  ['/round.js', ['text/javascript; charset=utf-8', roundJs, immutable]],
  ['/round.css', ['text/css; charset=utf-8', roundCss, immutable]],
  ['/art/cloud-surge.jpg', ['image/jpeg', readFileSync(new URL('./art/cloud-surge.jpg', import.meta.url)), immutable]],
]);

// Presenter-only "Behind the scenes" lines, best effort: a failed write is logged and never changes a response.
const TIMELINE_CAP = 300;
async function timeline(runId, line) {
  try {
    await db.send(new UpdateItemCommand({
      TableName: table, Key: { pk: s(`timeline#${runId}`) },
      // `lines` is a DynamoDB reserved word: it must go through a name placeholder.
      UpdateExpression: 'SET run_id = :run, #lines = list_append(if_not_exists(#lines, :empty), :new)',
      ConditionExpression: 'attribute_not_exists(#lines) OR size(#lines) < :cap',
      ExpressionAttributeNames: { '#lines': 'lines' },
      ExpressionAttributeValues: {
        ':run': s(runId), ':cap': n(TIMELINE_CAP), ':empty': { L: [] },
        ':new': { L: [s(JSON.stringify({
          t: Date.now(), tag: 'WEB', ...line,
          ...(process.env.AWS_LAMBDA_LOG_GROUP_NAME && { log_group: process.env.AWS_LAMBDA_LOG_GROUP_NAME }),
        }))] },
      },
    }), { abortSignal: AbortSignal.timeout(3_000) });
  } catch (error) {
    if (error.name !== 'ConditionalCheckFailedException') console.error('Timeline write failed', line.id, error.name);
  }
}
async function timelineLines(runId) {
  const { Item } = await db.send(new GetItemCommand({ TableName: table, Key: { pk: s(`timeline#${runId}`) } }));
  return (Item?.lines?.L ?? []).flatMap(entry => {
    try { return [JSON.parse(entry.S)]; } catch { return []; }
  });
}
const loginKey = { pk: { S: 'login' } };
// At most MAX_FAILURES failed sign-ins per window across every environment: each attempt
// reserves a slot before the passphrase is compared and a success hands it back.
const throttle = {
  async reserve(now) {
    const cutoff = now - WINDOW_MS;
    for (let attempt = 0; attempt < 5; attempt++) {
      if (await conditional(new UpdateItemCommand({
        TableName: table, Key: loginKey, UpdateExpression: 'SET window_start = :now, failures = :one',
        ConditionExpression: 'attribute_not_exists(window_start) OR window_start <= :cutoff',
        ExpressionAttributeValues: { ':now': n(now), ':one': n(1), ':cutoff': n(cutoff) },
      }))) return true;
      if (await conditional(new UpdateItemCommand({
        TableName: table, Key: loginKey, UpdateExpression: 'ADD failures :one',
        ConditionExpression: 'window_start > :cutoff AND failures < :max',
        ExpressionAttributeValues: { ':one': n(1), ':cutoff': n(cutoff), ':max': n(MAX_FAILURES) },
      }))) return true;
      const { Item } = await db.send(new GetItemCommand({ TableName: table, Key: loginKey, ConsistentRead: true }));
      if (Item && Number(Item.window_start.N) > cutoff && Number(Item.failures.N) >= MAX_FAILURES) return false;
    }
    return false;
  },
  release(now) {
    return conditional(new UpdateItemCommand({
      TableName: table, Key: loginKey, UpdateExpression: 'ADD failures :minus',
      ConditionExpression: 'window_start > :cutoff AND failures > :zero',
      ExpressionAttributeValues: { ':minus': n(-1), ':cutoff': n(now - WINDOW_MS), ':zero': n(0) },
    }));
  },
};
const ssm = new SSMClient({});
async function loadSecrets() {
  const names = [process.env.PRESENTER_PARAMETER, process.env.PASSPHRASE_PARAMETER];
  const { Parameters = [] } = await ssm.send(new GetParametersCommand({ Names: names, WithDecryption: true }));
  const value = name => Parameters.find(parameter => parameter.Name === name)?.Value;
  return { key: value(names[0]), passphrase: value(names[1]) };
}
const auth = presenterAuth({
  load: loadSecrets, throttle, origin: process.env.PUBLIC_HOST && `https://${process.env.PUBLIC_HOST}`,
});
// The house and the bots are not on the roster: the workflow adds them when it plans the launch round.
const fresh = () => ({ phase: s('ready'), run_id: s(id()), teams_data: s('[]') });
// Every phone poll reads the roster; parse it once per distinct value.
let parsedPlayers = { raw: undefined, value: [] };
function playersOf(row) {
  if (parsedPlayers.raw !== row.teams_data.S) parsedPlayers = { raw: row.teams_data.S, value: JSON.parse(row.teams_data.S) };
  return parsedPlayers.value;
}
async function initialized() {
  let row = await stored();
  if (!row) {
    await replace(fresh(), 'attribute_not_exists(pk)');
    row = await stored();
  }
  return row;
}

class ActionConflict extends Error {
  constructor() { super('This action no longer matches the saved rehearsal.'); }
}

async function stored() {
  const { Item } = await db.send(new GetItemCommand({ TableName: table, Key: key, ConsistentRead: true }));
  note(Item?.phase?.S, Item?.round?.S);
  return Item;
}

async function conditional(command) {
  try {
    await db.send(command);
    return true;
  } catch (error) {
    if (error.name === 'ConditionalCheckFailedException') return false;
    throw error;
  }
}

function update(expression, condition, values) {
  return conditional(new UpdateItemCommand({
    TableName: table, Key: key, UpdateExpression: expression,
    ConditionExpression: condition, ExpressionAttributeValues: values,
  }));
}

async function claim(expression, condition, values) {
  try {
    const { Attributes } = await db.send(new UpdateItemCommand({
      TableName: table, Key: key, UpdateExpression: expression, ConditionExpression: condition,
      ExpressionAttributeValues: values, ReturnValues: 'ALL_NEW',
    }));
    return Attributes;
  } catch (error) {
    if (error.name === 'ConditionalCheckFailedException') return null;
    throw error;
  }
}
const rosterStore = { update, claim };

function replace(item, condition, values) {
  return conditional(new PutItemCommand({
    TableName: table, Item: { ...key, ...item }, ConditionExpression: condition,
    ...(values && { ExpressionAttributeValues: values }),
  }));
}

function attachArn(run, arn) {
  return update('SET execution_arn = :arn',
    'run_id = :id AND phase = :starting AND attribute_not_exists(execution_arn)',
    { ':arn': s(arn), ':id': run.run_id, ':starting': s('starting') });
}

async function start(token, row) {
  if (row.phase.S !== 'ready' || token !== row.run_id.S) throw new ActionConflict();
  const run = row;
  if (!await update('SET phase = :starting, workflow_arn = :workflow, start_deadline = :deadline',
    'phase = :ready AND run_id = :id', {
      ':starting': s('starting'), ':workflow': s(workflow), ':deadline': n(Date.now() + startWindow),
      ':ready': s('ready'), ':id': row.run_id,
    })) throw new ActionConflict();

  // Reserve before invoking. Both this response and the first workflow step can
  // record the ARN; neither can overwrite a later stage or a different run.
  const runId = run.run_id.S;
  // Written before the invoke so it precedes the workflow's own lines.
  await timeline(runId, {
    id: 'web-start', text: `Presenter pressed Start: ${playersOf(row).length} players in the room`,
    detail: { players: playersOf(row).length, run_id: runId },
  });
  try {
    const { DurableExecutionArn } = await lambda.send(new InvokeCommand({
      FunctionName: workflow, InvocationType: 'Event', DurableExecutionName: runId,
      Payload: Buffer.from(JSON.stringify({ id: runId })),
    }), { abortSignal: AbortSignal.timeout(10_000) });
    if (DurableExecutionArn) await attachArn(run, DurableExecutionArn);
  } catch (error) {
    // Do not release the slot on a lost or ambiguous response. Discovery below
    // reconciles a missing ARN before declaring that the start expired.
    console.error('Rehearsal start could not be confirmed', runId, error.name);
  }
}

// A room of phones polls /api/state, and each poll used to ask Lambda for the execution's status:
// at 40 players that is enough to be throttled. Ask at most once per two seconds per environment,
// sharing one in-flight answer between concurrent requests. The phase itself is always read fresh.
const statusCache = new Map();
function durableStatus(arn) {
  const hit = statusCache.get(arn);
  if (hit && Date.now() - hit.at < 2000) return hit.promise;
  if (statusCache.size > 16) statusCache.clear();
  const promise = lambda.send(new GetDurableExecutionCommand({ DurableExecutionArn: arn, IncludeExecutionData: false }))
    .then(({ Status }) => ({ status: Status }), error => {
      if (error.name !== 'ResourceNotFoundException') throw error;
      return { missing: true };
    });
  statusCache.set(arn, { at: Date.now(), promise });
  promise.catch(() => statusCache.delete(arn));
  return promise;
}

async function recoverStart(row) {
  if (row?.phase.S !== 'starting' || row.execution_arn || Date.now() < Number(row.start_deadline.N)) return row;
  // AWS refuses a name filter once a version is given, as a Qualifier or inside the ARN, so list
  // by the unqualified function: a run id is unique across versions.
  const arn = row.workflow_arn.S;
  const { DurableExecutions } = await lambda.send(new ListDurableExecutionsByFunctionCommand({
    FunctionName: arn.slice(0, arn.lastIndexOf(':')), DurableExecutionName: row.run_id.S,
  }));
  const found = DurableExecutions?.find(result => result.DurableExecutionName === row.run_id.S);
  if (found?.DurableExecutionArn) {
    await attachArn(row, found.DurableExecutionArn);
  } else {
    await update('SET phase = :expired REMOVE start_deadline',
      'phase = :starting AND run_id = :id AND attribute_not_exists(execution_arn)',
      { ':expired': s('expired'), ':starting': s('starting'), ':id': row.run_id });
  }
  return stored();
}

/** The saved row plus what the Durable execution says: the phase everyone sees, a message and the host's retry. */
async function describe(row) {
  if (row.phase.S === 'ready') {
    return { mode: 'aws', phase: 'ready', id: row.run_id.S, action_token: row.run_id.S, row };
  }
  const arn = row.execution_arn?.S;
  let phase = row.phase.S;
  if (arn && !terminal.has(phase)) {
    const answer = await durableStatus(arn);
    let status;
    if (!answer.missing) {
      status = answer.status;
    } else if (phase === 'starting' && row.start_deadline && Date.now() >= Number(row.start_deadline.N)) {
      // Immediately after Invoke, the execution may not be visible to Get yet; after the window it is lost.
      await update('SET phase = :expired REMOVE start_deadline',
        'phase = :starting AND run_id = :id AND execution_arn = :arn AND attribute_exists(start_deadline)',
        { ':expired': s('expired'), ':starting': s('starting'), ':id': row.run_id, ':arn': row.execution_arn });
      return describe(await stored());
    }
    if (['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'STOPPED'].includes(status)) {
      // Get can observe completion after we read the pre-terminal DynamoDB row.
      const latest = await stored();
      if (latest.run_id.S !== row.run_id.S) return describe(latest);
      row = latest;
      phase = row.phase.S;
    }
    if (!terminal.has(phase)) {
      if (status === 'TIMED_OUT') phase = 'expired';
      else if (['FAILED', 'STOPPED', 'DELETING', 'SUCCEEDED'].includes(status)) phase = 'failed';
    }
  }
  // `row` is the item this answer was built from (re-read above when the execution finished), so a view never mixes rows.
  const result = { mode: 'aws', id: row.run_id.S, phase, action_token: row.run_id.S, execution_arn: arn, row };
  if (phase === 'expired') {
    result.message = row.phase.S === 'expired' && !arn ? 'AWS did not start the reserved execution in time.'
      : row.round ? 'The round expired before it could continue.' : 'The Durable execution exceeded its deadline.';
  } else if (phase === 'failed') {
    result.message = 'The Durable execution did not complete the game. Use its ARN to inspect it in Lambda.';
  }
  if (phase === 'closing' && row.callback_id && Number(row.lease_until?.N) <= Date.now()) result.retry_action = 'close';
  return result;
}

/**
 * Close the open round: ONE conditional write moves open -> closing for this run, callback and round
 * and copies the roster into frozen_data. A pick that raced it either committed first (it is in
 * frozen_data) or fails its own condition. Then the Durable callback carries that frozen roster.
 */
async function close(token, round, row, state) {
  if (token !== state.id || !ROUND_IDS.includes(round) || round !== row.round?.S) throw new ActionConflict();
  const now = Date.now();
  const lease = n(now + callbackLease);
  let claimed;
  if (state.phase === 'open') claimed = await closeRound(rosterStore, row, round, lease);
  else if (state.retry_action === 'close') claimed = await retryClose(rosterStore, row, round, lease, now);
  if (!claimed) throw new ActionConflict();
  if (state.phase === 'open') {
    const frozen = JSON.parse(claimed.frozen_data.S);
    const picked = frozen.filter(player => player.picks?.[round]).length;
    await timeline(state.id, {
      id: `web-close-${round}`, text: `Presenter closed ${round}: ${picked} of ${frozen.length} players picked`,
      detail: { round, picked, players: frozen.length },
    });
  }
  try {
    await lambda.send(new SendDurableExecutionCallbackSuccessCommand({
      CallbackId: row.callback_id.S,
      Result: Buffer.from(JSON.stringify(closePayload(round, JSON.parse(claimed.frozen_data.S)))),
    }), { abortSignal: AbortSignal.timeout(10_000) });
  } catch (error) {
    // AWS uses the same exception for expiration and an already-consumed token.
    // In either case the workflow, not the Web request, decides what is next.
    if (error.name !== 'CallbackTimeoutException') {
      console.error('Rehearsal callback could not be confirmed', state.id, round, error.name);
      return;
    }
  }
  await releaseClose(rosterStore, row, lease);
}

// One structured access line per request: a fixed route template, status, duration and the phase and round of the
// state this request already loaded. Never ids, names, tokens, bodies, query strings or headers.
const requestLog = new AsyncLocalStorage();
function note(phase, round) {
  const entry = requestLog.getStore();
  if (entry && phase) { entry.phase = phase; entry.round = round ?? undefined; }
}
const POLLS = new Set(['/api/state', '/api/host/state', '/api/host/timeline']);
const API_ROUTES = new Set([
  'GET /api/state', 'GET /api/host/state', 'GET /api/host/timeline', 'GET /api/host/qr.svg',
  'POST /api/join', 'POST /api/rename', 'POST /api/pick', 'POST /api/host/remove',
  'POST /api/host/login', 'POST /api/host/logout', 'POST /api/host/start', 'POST /api/host/close', 'POST /api/host/reset',
]);
function routeTemplate(method, url) {
  if (API_ROUTES.has(`${method} ${url}`)) return url;
  const path = url.split('?', 1)[0];
  return method === 'GET' && staticFiles.has(path) ? path : 'other';
}
function accessLine(entry, status) {
  const level = status >= 500 ? 'error' : status >= 400 ? 'warn' : POLLS.has(entry.route) ? 'debug' : 'info';
  return JSON.stringify({
    bf: 'access', level, method: entry.method, route: entry.route, status, ms: Date.now() - entry.start,
    ...(entry.phase && { phase: entry.phase }), ...(entry.round && { round: entry.round }),
  });
}

function send(response, code, type, body, extra = {}) {
  const headers = { 'Content-Type': type, 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
    'Strict-Transport-Security': 'max-age=31536000', ...extra,
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  };
  // A room of phones polls this: send the roster and the board gzipped when the client accepts it.
  if (/^(application\/json|text\/|image\/svg)/.test(type) && Buffer.byteLength(body) >= 1024
    && /\bgzip\b/.test(response.req?.headers['accept-encoding'] ?? '')) {
    body = gzipSync(body);
    headers['Content-Encoding'] = 'gzip';
    headers.Vary = 'Accept-Encoding';
  }
  response.writeHead(code, headers);
  response.end(body);
}

function json(response, code, body, extra) {
  send(response, code, 'application/json; charset=utf-8', JSON.stringify(body), extra);
}

async function body(request) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 2048) throw new RequestError(413, 'Request body limit is 2 KiB');
    chunks.push(chunk);
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString());
    if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error();
    return value;
  } catch { throw new RequestError(400, 'JSON object required'); }
}

const rounds = publicRounds();
// A results item is immutable per (run, round): read it once per environment and share the answer, even
// between phones that ask at the same instant. A missing item is not cached; the workflow publishes it
// before it opens the next round, so this only happens on a race.
const resultsCache = new Map();
function loadResults(runId, round) {
  const cacheKey = `results#${runId}#${round}`;
  if (!resultsCache.has(cacheKey)) {
    if (resultsCache.size >= 16) resultsCache.clear();
    const promise = db.send(new GetItemCommand({ TableName: table, Key: { pk: s(cacheKey) }, ConsistentRead: true }))
      .then(({ Item }) => {
        if (!Item) resultsCache.delete(cacheKey);
        return Item && JSON.parse(Item.data.S);
      }, error => { resultsCache.delete(cacheKey); throw error; });
    resultsCache.set(cacheKey, promise);
  }
  return resultsCache.get(cacheKey);
}
async function resultsFor(row, ownId) {
  const round = row.results_round?.S;
  const data = round && await loadResults(row.run_id.S, round);
  if (!data) return null;
  return {
    round: data.round, market: data.market, summary: data.summary, board: board(data.rows, ownId),
    mine: ownId ? data.rows.find(value => value.id === ownId) ?? null : null,
  };
}
let evidenceMemo = { raw: undefined, value: null };
function evidenceOf(row) {
  if (!row.launch_data || !row.evidence_data) return null;
  const raw = `${row.launch_data.S}|${row.evidence_data.S}`;
  if (evidenceMemo.raw !== raw) {
    evidenceMemo = { raw, value: { launch: JSON.parse(row.launch_data.S), ...JSON.parse(row.evidence_data.S) } };
  }
  return evidenceMemo.value;
}

async function view(row, headers, hostView = false, mode) {
  const state = await describe(row);
  row = state.row;
  const players = playersOf(row);
  const { phase } = state;
  const round = ['open', 'closing'].includes(phase) ? row.round?.S ?? null : phase === 'final' ? ROUND_IDS.at(-1) : null;
  note(phase, round);
  const own = hostView ? null : me(players, headers['x-team-id'], headers['x-team-token']);
  const result = {
    mode: 'aws', id: state.id, phase, round, round_number: round ? ROUND_IDS.indexOf(round) + 1 : 0, rounds,
    joinable: joinable(phase, round), renamable: renamable(phase, round), player_count: players.length,
    player_cap: PLAYER_CAP, me: own, results: phase === 'ready' ? null : await resultsFor(row, own?.id),
    evidence: evidenceOf(row), message: state.message ?? '',
  };
  if (hostView) {
    // Never derived from request headers: unset PUBLIC_HOST means no URL, no QR code.
    result.players = hostPlayers(players, phase === 'open' ? round : null);
    result.public_url = process.env.PUBLIC_HOST ? `https://${process.env.PUBLIC_HOST}` : null;
    result.auth_mode = mode;
    result.execution_arn = state.execution_arn;
    result.action_token = state.action_token;
    if (state.retry_action) result.retry_action = state.retry_action;
  }
  return result;
}

async function changeRoster(action, input, token) {
  for (let attempt = 0; attempt < 60; attempt++) {
    // A room joining at once rewrites the one roster item: losers re-read and retry after a short jitter.
    if (attempt) await new Promise(resolve => setTimeout(resolve, randomInt(10, 40 + attempt * 10)));
    const row = await initialized(), players = playersOf(row);
    const phase = row.phase.S, round = row.round?.S;
    let next, reply;
    if (action === 'join') {
      const { player, token: secret } = join(players, phase, round, input.name);
      next = [...players, player];
      reply = { id: player.id, token: secret, run_id: row.run_id.S, name: player.name };
    } else if (action === 'rename') {
      const player = rename(players, phase, round, input);
      next = players.map(value => (value.id === player.id ? player : value));
      reply = { name: player.name };
    } else if (action === 'pick') {
      const player = pick(players, phase, round, input);
      next = players.map(value => (value.id === player.id ? player : value));
      reply = { saved: true };
    } else {
      if (token !== row.run_id.S) throw new ActionConflict();
      next = remove(players, phase, input.id);
      reply = { removed: true };
    }
    if (await writeRoster(rosterStore, row, next)) return reply;
  }
  throw new ActionConflict();
}

const hostActions = new Set(['start', 'close', 'reset']);

async function route(request, response) {
  const { url, method, headers } = request;
  const kind = routeKind(method, url);
  const presenterRoute = kind === 'presenter';
  if (kind === 'login') {
    auth.checkOrigin(headers);
    const cookie = await auth.login((await body(request)).passphrase);
    return json(response, 200, { signed_in: true }, { 'Set-Cookie': cookie });
  }
  if (kind === 'logout') {
    auth.checkOrigin(headers);
    return json(response, 200, { signed_in: false }, { 'Set-Cookie': auth.logout() });
  }
  const mode = presenterRoute ? await auth.authorize(headers, method) : undefined;
  if (method === 'GET' && url === '/api/host/qr.svg') {
    return send(response, 200, 'image/svg+xml', await QRCode.toString(auth.publicOrigin(), { type: 'svg', margin: 4 }));
  }
  if (method === 'GET' && url === '/api/host/timeline') {
    const run = (await initialized()).run_id.S;
    return json(response, 200, { run_id: run, lines: await timelineLines(run) });
  }
  const path = url.split('?', 1)[0];
  if (method === 'GET' && staticFiles.has(path)) return send(response, 200, ...staticFiles.get(path));
  if (method === 'GET' && ['/api/state', '/api/host/state'].includes(url)) {
    return json(response, 200, await view(await recoverStart(await initialized()), headers, presenterRoute, mode));
  }
  if (method === 'POST' && ['/api/join', '/api/rename', '/api/pick'].includes(url)) {
    return json(response, 200, await changeRoster(url.slice(5), await body(request)));
  }
  if (method === 'POST' && url === '/api/host/remove') {
    const input = await body(request);
    return json(response, 200, await changeRoster('remove', input, headers['x-rehearsal-run']));
  }
  const action = method === 'POST' && url.startsWith('/api/host/') ? url.slice('/api/host/'.length) : undefined;
  if (!hostActions.has(action)) return json(response, 404, { error: 'Not found' });
  if (Number(headers['content-length'] ?? 0) > 0 || headers['transfer-encoding']) throw new RequestError(413, 'Presenter actions have no body');
  const token = headers['x-rehearsal-run'];
  const row = await initialized();
  if (action === 'start') {
    await start(token, row);
  } else if (action === 'reset') {
    // Force: any phase, as long as the presenter's token names this run.
    const next = fresh();
    if (token !== row.run_id.S || !await replace(next, 'run_id = :id', { ':id': row.run_id })) throw new ActionConflict();
    await timeline(next.run_id.S, {
      id: 'web-reset', text: 'Presenter reset the game', detail: { run_id: next.run_id.S },
    });
  } else {
    await close(token, headers['x-rehearsal-round'], row, await describe(row));
  }
  return json(response, 200, await view(await stored(), headers, true, mode));
}

const endpoint = process.env.AWS_LAMBDA_HTTP_ENDPOINT ?? 'localhost:3000';
const separator = endpoint.lastIndexOf(':');
const host = endpoint.slice(0, separator).replace(/^\[|\]$/g, '');
const port = Number(endpoint.slice(separator + 1));
if (!host || separator < 1 || !Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('AWS_LAMBDA_HTTP_ENDPOINT must be host:port');
}
createServer((request, response) => {
  const entry = { method: request.method, route: routeTemplate(request.method, request.url), start: Date.now() };
  // 'close' fires once per response, finished or cut off by the client (499).
  response.on('close', () => console.log(accessLine(entry, response.writableFinished ? response.statusCode : 499)));
  requestLog.run(entry, () => route(request, response)).catch(error => {
    if (error instanceof ActionConflict) return json(response, 409, { error: error.message });
    if (error instanceof RequestError) return json(response, error.status, { error: error.message });
    console.error('Arena request failed', request.headers['lambda-web-request-id'], error.name);
    return json(response, 503, { error: 'AWS could not confirm the saved stage. Refresh the rehearsal state.' });
  });
}).listen(port, host);
