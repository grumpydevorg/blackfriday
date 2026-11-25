import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { gzipSync } from 'node:zlib';
import { CloudWatchLogsClient } from '@aws-sdk/client-cloudwatch-logs';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { LambdaClient } from '@aws-sdk/client-lambda';
import { GetParametersCommand, SSMClient } from '@aws-sdk/client-ssm';
import { RequestError } from './game.mjs';
import { memoryThrottle, presenterAuth } from './host_auth.mjs';
import { createHub, parseCursor } from './logs.mjs';

// The presenter's live log as its own streaming Web Function. Only the sign-in page and the two small
// files it needs are public; the viewer, its files and every data route need the presenter cookie or key.
const { RUN_TABLE: table, WORKFLOW_FUNCTION: workflowFunction, LOG_GROUPS: groupList } = process.env;
if (!table || !groupList) throw new Error('Set RUN_TABLE and LOG_GROUPS');
const STREAM_SECONDS = 900 - 2;
const PING_MS = 15_000;
const MAX_BACKLOG_BYTES = 2_000_000;

const hub = createHub({
  db: new DynamoDBClient({}), lambda: new LambdaClient({}), logs: new CloudWatchLogsClient({}), table,
  groups: groupList.split(',').map(group => group.trim()).filter(Boolean), workflowFunction,
  // Live Tail needs the log group ARNs; without the account the hub falls back to polling FilterLogEvents alone.
  accountId: process.env.AWS_ACCOUNT_ID, region: process.env.AWS_REGION,
  pollMs: Number(process.env.POLL_MS) || 2000,
  report: (source, error) => console.error('Log source failed', source, error?.name),
});

const ssm = new SSMClient({});
async function loadSecrets() {
  const names = [process.env.PRESENTER_PARAMETER, process.env.PASSPHRASE_PARAMETER];
  const { Parameters = [] } = await ssm.send(new GetParametersCommand({ Names: names, WithDecryption: true }));
  const value = name => Parameters.find(parameter => parameter.Name === name)?.Value;
  return { key: value(names[0]), passphrase: value(names[1]) };
}
// In memory: at most 10 failed sign-ins per 10 minutes per environment.
const auth = presenterAuth({
  load: loadSecrets, throttle: memoryThrottle(), origin: process.env.PUBLIC_HOST && `https://${process.env.PUBLIC_HOST}`,
});

// ---------------------------------------------------------------- static files

const digest = bytes => createHash('sha256').update(bytes).digest('hex').slice(0, 10);
const asset = name => readFileSync(new URL(`./logs_page/${name}`, import.meta.url));
const assets = {
  '/signin.css': ['text/css; charset=utf-8', asset('signin.css')],
  '/signin.js': ['text/javascript; charset=utf-8', asset('signin.js')],
  '/logs.css': ['text/css; charset=utf-8', asset('logs.css')],
  '/logs.js': ['text/javascript; charset=utf-8', asset('logs.js')],
};
const versioned = new Map(Object.entries(assets).map(([path, [, bytes]]) => [path, digest(bytes)]));
/** The page with each asset reference rewritten to its content hash, so a redeploy cannot pair old script with new HTML. */
function page(name, paths) {
  let html = asset(name).toString();
  for (const path of paths) {
    const reference = new RegExp(`(?:href|src)="${path.replace('.', '\\.')}"`);
    if (!reference.test(html)) throw new Error(`${name} does not reference ${path}`);
    html = html.replace(reference, match => match.replace(path, `${path}?v=${versioned.get(path)}`));
  }
  return Buffer.from(html);
}
const HTML = 'text/html; charset=utf-8';
const signinPage = page('signin.html', ['/signin.css', '/signin.js']);
const viewerPage = page('index.html', ['/logs.css', '/logs.js']);
const publicCache = { 'Cache-Control': 'public, max-age=3600' };
const privateCache = { 'Cache-Control': 'private, max-age=3600' };
const PUBLIC_ASSETS = new Set(['/signin.css', '/signin.js']);

// ---------------------------------------------------------------- responses

const headersFor = (type, extra = {}) => ({
  'Content-Type': type, 'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
  'Strict-Transport-Security': 'max-age=31536000', ...extra,
  'Content-Security-Policy': "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
});

function send(response, code, type, body, extra = {}) {
  const headers = headersFor(type, extra);
  if (/^(application\/json|text\/)/.test(type) && Buffer.byteLength(body) >= 1024
    && /\bgzip\b/.test(response.req?.headers['accept-encoding'] ?? '')) {
    body = gzipSync(body);
    headers['Content-Encoding'] = 'gzip';
    headers.Vary = 'Accept-Encoding';
  }
  response.writeHead(code, headers);
  response.end(body);
}
const json = (response, code, body, extra) => send(response, code, 'application/json; charset=utf-8', JSON.stringify(body), extra);

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

// ---------------------------------------------------------------- access log (same JSON as web.mjs)

const POLLS = new Set(['/events', '/api/status', '/api/players']);
const ROUTES = new Set([
  'GET /', 'GET /viewer', 'GET /stream', 'GET /events', 'GET /api/status', 'GET /api/players', 'POST /api/login', 'POST /api/logout',
  ...Object.keys(assets).map(path => `GET ${path}`),
]);
const routeTemplate = (method, path) => (ROUTES.has(`${method} ${path}`) ? path : 'other');
function accessLine(entry, status) {
  const level = status >= 500 ? 'error' : status >= 400 ? 'warn' : POLLS.has(entry.route) ? 'debug' : 'info';
  return JSON.stringify({ bf: 'access', level, method: entry.method, route: entry.route, status, ms: Date.now() - entry.start });
}

// ---------------------------------------------------------------- streaming

const message = (ts, event) => `id: ${ts}\ndata: ${JSON.stringify(event)}\n\n`;
const statusMessage = status => `event: status\ndata: ${JSON.stringify(status)}\n\n`;

function stream(request, response, query) {
  const debug = query.get('debug') === '1';
  const cursor = parseCursor(request.headers['last-event-id'], query.get('after'));
  response.writeHead(200, headersFor('text/event-stream', { 'X-Accel-Buffering': 'no' }));
  response.flushHeaders();
  request.socket.setNoDelay(true);
  request.socket.setTimeout(0);
  let closed = false;
  const write = text => {
    if (closed) return;
    response.write(text);
    // A client that cannot keep up is dropped; EventSource reconnects with its cursor and the overlap refills it.
    if (response.writableLength > MAX_BACKLOG_BYTES) response.destroy();
  };
  // The first bytes go out before any AWS call is made or awaited.
  write(`retry: 2000\nevent: hello\ndata: ${JSON.stringify({ now: Date.now(), debug })}\n\n`);
  const backlog = hub.eventsAfter(cursor, debug);
  if (backlog.length) write(backlog.map(event => message(event.ts, event)).join(''));
  if (hub.polls) write(statusMessage(hub.status));
  // Story cards carry no SSE id: the resume cursor stays the newest log event. The client replaces a card by its id.
  const cardsMessage = cards => cards.map(card => `event: story\ndata: ${JSON.stringify(card)}\n\n`).join('');
  write(cardsMessage(hub.storyCards()));
  const release = hub.attach((added, status, story = []) => {
    const fresh = added.filter(event => debug || event.level !== 'debug');
    write(fresh.map(event => message(event.ts, event)).join('') + (status ? statusMessage(status) : '') + cardsMessage(story));
  });
  const ping = setInterval(() => write(': ping\n\n'), PING_MS);
  const stop = setTimeout(() => response.end(), STREAM_SECONDS * 1000);
  response.on('close', () => {
    closed = true;
    release();
    clearInterval(ping);
    clearTimeout(stop);
  });
}

// ---------------------------------------------------------------- routes

async function route(request, response, path, query) {
  const { method, headers } = request;
  if (method === 'POST' && path === '/api/login') {
    auth.checkOrigin(headers);
    const cookie = await auth.login((await body(request)).passphrase);
    return json(response, 200, { signed_in: true }, { 'Set-Cookie': cookie });
  }
  if (method === 'GET' && path === '/') {
    // Signed in already: straight to the viewer. Anything else, including a failure to check, is the form.
    if (await auth.authorize(headers, method).then(() => true, () => false)) {
      return send(response, 303, HTML, '', { Location: '/viewer' });
    }
    return send(response, 200, HTML, signinPage);
  }
  if (method === 'GET' && PUBLIC_ASSETS.has(path)) return send(response, 200, ...assets[path], publicCache);

  // Everything below is presenter-only; a signed-out request learns nothing but that it must sign in.
  try {
    await auth.authorize(headers, method);
  } catch (error) {
    if (method === 'GET' && path === '/viewer' && error instanceof RequestError && error.status === 403) {
      return send(response, 303, HTML, '', { Location: '/' });
    }
    throw error;
  }
  if (method === 'POST' && path === '/api/logout') return json(response, 200, { signed_in: false }, { 'Set-Cookie': auth.logout() });
  if (method !== 'GET') return json(response, 404, { error: 'Not found' });
  if (path === '/viewer') return send(response, 200, HTML, viewerPage);
  if (path === '/logs.js' || path === '/logs.css') return send(response, 200, ...assets[path], privateCache);
  if (path === '/stream') return stream(request, response, query);
  if (path === '/events') {
    await hub.touch();
    const events = hub.eventsAfter(parseCursor(query.get('after')), query.get('debug') === '1');
    return json(response, 200, { now: Date.now(), events, status: hub.status, story: hub.storyCards() });
  }
  if (path === '/api/status') {
    await hub.touch();
    return json(response, 200, { now: Date.now(), status: hub.status });
  }
  if (path === '/api/players') {
    await hub.touch();
    return json(response, 200, { players: hub.players });
  }
  return json(response, 404, { error: 'Not found' });
}

const endpoint = process.env.AWS_LAMBDA_HTTP_ENDPOINT ?? 'localhost:3000';
const separator = endpoint.lastIndexOf(':');
const host = endpoint.slice(0, separator).replace(/^\[|\]$/g, '');
const port = Number(endpoint.slice(separator + 1));
if (!host || separator < 1 || !Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('AWS_LAMBDA_HTTP_ENDPOINT must be host:port');
}
createServer((request, response) => {
  const [path, search = ''] = request.url.split('?', 2);
  const entry = { method: request.method, route: routeTemplate(request.method, path), start: Date.now() };
  // 'close' fires once per response, finished or cut off by the client (499); a stream is logged when it ends.
  response.on('close', () => console.log(accessLine(entry, response.writableFinished ? response.statusCode : 499)));
  route(request, response, path, new URLSearchParams(search)).catch(error => {
    if (response.headersSent) return response.destroy();
    if (error instanceof RequestError) return json(response, error.status, { error: error.message });
    console.error('Logs request failed', request.headers['lambda-web-request-id'], error?.name);
    return json(response, 503, { error: 'The live log could not read AWS. Try again.' });
  });
}).listen(port, host);
