import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { DynamoDBClient, GetItemCommand, PutItemCommand, UpdateItemCommand } from '@aws-sdk/client-dynamodb';
import {
  GetDurableExecutionCommand,
  InvokeCommand,
  LambdaClient,
  ListDurableExecutionsByFunctionCommand,
  SendDurableExecutionCallbackSuccessCommand,
} from '@aws-sdk/client-lambda';
import { SSMClient, GetParametersCommand } from '@aws-sdk/client-ssm';
import { RequestError, houses, join, pick, remove, publicTeams, frozen, open } from './game.mjs';
import { presenterAuth, routeKind, MAX_FAILURES, WINDOW_MS } from './host_auth.mjs';
import { s, n, writeRoster, closeRound } from './roster.mjs';
import QRCode from 'qrcode';

const { RUN_TABLE: table, WORKFLOW_ARN: workflow } = process.env;
if (!table || !/:\d+$/.test(workflow ?? '')) throw new Error('Set RUN_TABLE and a numbered WORKFLOW_ARN');
const db = new DynamoDBClient({});
const lambda = new LambdaClient({});
const key = { pk: { S: 'arena' } };
const startWindow = 90_000;
const callbackLease = 20_000;
const id = () => randomUUID().replaceAll('-', '');
const terminal = new Set(['compared', 'expired', 'failed']);
const callbacks = { migrate: ['launch', 'migrating'], compare: ['migrated', 'comparing'] };
const immutable = { 'Cache-Control': 'public, max-age=3600' };
const staticFiles = new Map([
  ['/', ['text/html; charset=utf-8', readFileSync(new URL('./player.html', import.meta.url))]],
  ['/host', ['text/html; charset=utf-8', readFileSync(new URL('./host.html', import.meta.url))]],
  ['/round.js', ['text/javascript; charset=utf-8', readFileSync(new URL('./round.js', import.meta.url)), immutable]],
  ['/round.css', ['text/css; charset=utf-8', readFileSync(new URL('./round.css', import.meta.url)), immutable]],
  ['/art/cloud-surge.jpg', ['image/jpeg', readFileSync(new URL('./art/cloud-surge.jpg', import.meta.url)), immutable]],
]);
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
const fresh = () => ({ phase: s('ready'), run_id: s(id()), teams_data: s(JSON.stringify(houses())) });
const teamsOf = row => JSON.parse(row.teams_data.S);
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
    { ':arn': s(arn), ':id': run.run_id, ':starting': s('launching') });
}

async function start(token, row) {
  if (row.phase.S !== 'ready' || token !== row.run_id.S) throw new ActionConflict();
  const run = row;
  if (!await update('SET phase = :launching, workflow_arn = :workflow, start_deadline = :deadline',
    'phase = :ready AND run_id = :id', {
      ':launching': s('launching'), ':workflow': s(workflow), ':deadline': n(Date.now() + startWindow),
      ':ready': s('ready'), ':id': row.run_id,
    })) throw new ActionConflict();

  // Reserve before invoking. Both this response and the first workflow step can
  // record the ARN; neither can overwrite a later stage or a different run.
  const runId = run.run_id.S;
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

async function recoverStart(row) {
  if (row?.phase.S !== 'launching' || row.execution_arn || Date.now() < Number(row.start_deadline.N)) return row;
  const arn = row.workflow_arn.S;
  const separator = arn.lastIndexOf(':');
  const { DurableExecutions } = await lambda.send(new ListDurableExecutionsByFunctionCommand({
    FunctionName: arn.slice(0, separator), Qualifier: arn.slice(separator + 1), DurableExecutionName: row.run_id.S,
  }));
  const found = DurableExecutions?.find(result => result.DurableExecutionName === row.run_id.S);
  if (found?.DurableExecutionArn) {
    await attachArn(row, found.DurableExecutionArn);
  } else {
    await update('SET phase = :expired REMOVE start_deadline',
      'phase = :starting AND run_id = :id AND attribute_not_exists(execution_arn)',
      { ':expired': s('expired'), ':starting': s('launching'), ':id': row.run_id });
  }
  return stored();
}

async function describe(row) {
  if (!row || row.phase.S === 'ready') {
    return { mode: 'aws', phase: 'ready', id: row.run_id.S, action_token: row.run_id.S };
  }
  const arn = row.execution_arn?.S;
  let phase = row.phase.S;
  let status;
  if (arn && (phase !== 'expired' || row.expired_action)) {
    try {
      ({ Status: status } = await lambda.send(new GetDurableExecutionCommand({
        DurableExecutionArn: arn, IncludeExecutionData: false,
      })));
    } catch (error) {
      if (error.name !== 'ResourceNotFoundException') throw error;
      // Immediately after Invoke, the execution may not be visible to Get yet.
      // A terminal state already recorded by the workflow remains authoritative
      // after execution history retention expires.
      status = phase === 'compared' ? 'SUCCEEDED' : undefined;
      if (phase === 'launching' && row.start_deadline && Date.now() >= Number(row.start_deadline.N)) {
        await update('SET phase = :expired REMOVE start_deadline',
          'phase = :launching AND run_id = :id AND execution_arn = :arn AND attribute_exists(start_deadline)',
          { ':expired': s('expired'), ':launching': s('launching'), ':id': row.run_id, ':arn': row.execution_arn });
        return describe(await stored());
      }
    }
    if (['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'STOPPED'].includes(status) && !terminal.has(phase)) {
      // Get can observe completion after we read the pre-terminal DynamoDB row.
      const latest = await stored();
      if (latest.run_id.S !== row.run_id.S) return describe(latest);
      row = latest;
      phase = row.phase.S;
    }
    if (phase === 'expired' && ['RUNNING', 'PAUSING', 'PAUSED'].includes(status)) {
      phase = row.expired_action.S === 'migrate' ? 'migrating' : 'comparing';
    } else if (phase !== 'expired') {
      if (status === 'TIMED_OUT') phase = 'expired';
      else if (['FAILED', 'STOPPED', 'DELETING'].includes(status)) phase = 'failed';
      else if (phase === 'compared' && status !== 'SUCCEEDED') phase = 'comparing';
      else if (phase !== 'compared' && status === 'SUCCEEDED') phase = 'failed';
    }
  }
  const result = { mode: 'aws', id: row.run_id.S, phase, action_token: row.run_id.S, execution_arn: arn };
  if (phase === 'expired') {
    result.message = row.expired_action
      ? `The ${row.expired_action.S === 'migrate' ? 'deployment decision' : 'shopper reveal'} expired before it could continue.`
      : row.phase.S === 'expired' ? 'AWS did not start the reserved execution in time.' : 'The Durable execution exceeded its deadline.';
  } else if (phase === 'failed') {
    result.message = 'The Durable execution did not complete the rehearsal. Use its ARN to inspect it in Lambda.';
  }
  if (row.launch_data) {
    result.launch = JSON.parse(row.launch_data.S);
    result.checkpoint = row.checkpoint.S;
    result.launch_vm_id = row.launch_vm_id.S;
    result.launch_vm_state = row.launch_vm_state.S;
  }
  if (phase === 'compared') {
    result.branches = JSON.parse(row.outcomes_data.S);
    result.return_draw = 42;
  } else if (row.migrations_data) {
    // The schemas and evaluator metadata identify which migration preserved
    // carts. Until the reveal, only send the two shared checks to the browser.
    result.branches = JSON.parse(row.migrations_data.S).map(({ health_status, history_status }) => ({
      health_status, history_status,
    }));
  }
  if (row.callback_id && Number(row.lease_until?.N) <= Date.now()) {
    if (phase === 'migrating') result.retry_action = 'migrate';
    if (phase === 'comparing') result.retry_action = 'compare';
  }
  return result;
}

async function resume(action, token, row, state) {
  const [waiting, busy] = callbacks[action];
  if (token !== state.id || (state.phase !== waiting && state.retry_action !== action)) throw new ActionConflict();
  const now = Date.now();
  const lease = n(now + callbackLease);
  const values = { ':id': row.run_id, ':callback': row.callback_id, ':busy': s(busy), ':lease': lease };
  let claimed;
  if (state.phase !== waiting) {
    // Retry of a callback AWS never confirmed: the roster was frozen by the first claim.
    claimed = await claim('SET phase = :busy, lease_until = :lease',
      'run_id = :id AND callback_id = :callback AND phase = :busy AND lease_until <= :now', { ...values, ':now': n(now) });
  } else if (action === 'migrate') {
    // One write ends joining and picking and copies the roster at that instant. A pick that
    // raced it either committed before (it is in frozen_data) or fails its own condition.
    claimed = await closeRound(rosterStore, row, lease);
  } else {
    claimed = await claim('SET phase = :busy, lease_until = :lease',
      'run_id = :id AND callback_id = :callback AND phase = :waiting', { ...values, ':waiting': s(waiting) });
  }
  if (!claimed) throw new ActionConflict();
  try {
    await lambda.send(new SendDurableExecutionCallbackSuccessCommand({
      CallbackId: row.callback_id.S,
      Result: Buffer.from(JSON.stringify({
        action, ...(action === 'migrate' ? { teams: frozen(JSON.parse(claimed.frozen_data.S)) } : {}),
      })),
    }), { abortSignal: AbortSignal.timeout(10_000) });
  } catch (error) {
    // AWS uses the same exception for expiration and an already-consumed token.
    // In either case the workflow, not the Web request, decides what is next.
    if (error.name !== 'CallbackTimeoutException') {
      console.error('Rehearsal callback could not be confirmed', state.id, action, error.name);
      return;
    }
  }
  await update('REMOVE lease_until',
    'run_id = :id AND callback_id = :callback AND phase = :busy AND lease_until = :lease', values);
}

function send(response, code, type, body, extra = {}) {
  response.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
    'Strict-Transport-Security': 'max-age=31536000', ...extra,
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  });
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

async function view(row, headers, hostView = false, mode) {
  const result = await describe(row);
  result.open = open(result.phase);
  result.teams = publicTeams(teamsOf(row), result.phase, headers['x-team-id'], headers['x-team-token']);
  result.max_audience_teams = 8;
  result.audience_count = result.teams.filter(team => !team.house).length;
  if (hostView) {
    // Never derived from request headers: unset PUBLIC_HOST means no URL, no QR code.
    result.public_url = process.env.PUBLIC_HOST ? `https://${process.env.PUBLIC_HOST}` : null;
    result.auth_mode = mode;
  } else {
    delete result.execution_arn;
    delete result.action_token;
  }
  return result;
}

async function changeTeam(input, action) {
  for (let attempt = 0; attempt < 8; attempt++) {
    const row = await initialized(), teams = teamsOf(row);
    let next = teams, reply;
    if (action === 'join') {
      const { team, token } = join(teams, row.phase.S, input.name);
      next = [...teams, team];
      reply = { id: team.id, token, run_id: row.run_id.S };
    } else if (action === 'pick') {
      const team = pick(teams, row.phase.S, input);
      next = teams.map(value => (value.id === team.id ? team : value));
      reply = { saved: true };
    } else {
      if (input.run !== row.run_id.S) throw new ActionConflict();
      next = remove(teams, row.phase.S, input.id);
      reply = { removed: true };
    }
    if (await writeRoster(rosterStore, row, next)) return reply;
  }
  throw new ActionConflict();
}

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
  if (method === 'GET' && staticFiles.has(url)) return send(response, 200, ...staticFiles.get(url));
  if (method === 'GET' && ['/api/state', '/api/host/state'].includes(url)) {
    return json(response, 200, await view(await recoverStart(await initialized()), headers, presenterRoute, mode));
  }
  if (method === 'POST' && url === '/api/host/remove') {
    const input = await body(request);
    return json(response, 200, await changeTeam({ id: input.id, run: headers['x-rehearsal-run'] }, 'remove'));
  }
  if (method === 'POST' && ['/api/join', '/api/pick'].includes(url)) {
    return json(response, 200, await changeTeam(await body(request), url.slice(5)));
  }
  const action = url?.match(/^\/api\/(launch|migrate|compare|reset)$/)?.[1];
  if (method !== 'POST' || !action) return json(response, 404, { error: 'Not found' });
  if (Number(headers['content-length'] ?? 0) > 0 || headers['transfer-encoding']) throw new RequestError(413, 'Presenter actions have no body');
  const token = headers['x-rehearsal-run'];
  const row = await initialized();
  if (action === 'launch') {
    await start(token, row);
  } else {
    const state = await describe(row);
    if (action === 'reset') {
      if (token !== state.id || (!terminal.has(state.phase) && state.phase !== 'ready')) throw new ActionConflict();
      if (!await replace(fresh(), 'run_id = :id AND phase = :phase',
        { ':id': row.run_id, ':phase': row.phase })) throw new ActionConflict();
    } else {
      await resume(action, token, row, state);
    }
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
  route(request, response).catch(error => {
    if (error instanceof ActionConflict) return json(response, 409, { error: error.message });
    if (error instanceof RequestError) return json(response, error.status, { error: error.message });
    console.error('Arena request failed', request.headers['lambda-web-request-id'], error.name);
    return json(response, 503, { error: 'AWS could not confirm the saved stage. Refresh the rehearsal state.' });
  });
}).listen(port, host);
