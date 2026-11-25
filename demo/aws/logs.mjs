import { createHash } from 'node:crypto';
import { GetItemCommand } from '@aws-sdk/client-dynamodb';
import { GetDurableExecutionHistoryCommand } from '@aws-sdk/client-lambda';
import { FilterLogEventsCommand, StartLiveTailCommand } from '@aws-sdk/client-cloudwatch-logs';

// The presenter's live log: one event model built from the game's own records. Every event is
// {id, ts, tag, level, source, text, detail}; id and ts come from the data, never from the clock, so
// two environments that read the same records produce the same ids and a client can dedupe by id.

export const WINDOW_MS = 20 * 60_000;      // how much the shared buffer keeps
export const BACKFILL_MS = 15 * 60_000;    // the first read of a log group
export const OVERLAP_MS = 60_000;          // late-arriving CloudWatch ingestion; also the resume overlap
export const DEBUG_CAP = 3000;
export const PAGE_CAP = 5;
const TEXT_MAX = 500;
const ERROR_MAX = 200;

// ---------------------------------------------------------------- redaction

const SIGNED_URL = /https?:\/\/\S*(?:X-Amz-|Signature=|[Pp]ay)\S*/g;
const HEX_RUN = /(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])/g;
const COOKIE_LINE = /(?:__Host-)?\b(?:set-cookie|cookie|bf_host)\b\s*[:=][^\n]*/gi;
const SECRET_PAIR = /\b(?:token_hash|token|receipt|passphrase|authorization|x-presenter-key|x-team-token|secret)\b["']?\s*[:=]\s*["']?[^\s"',;&}]+/gi;
const SECRET_KEY = /token|receipt|cookie|secret|passphrase|authorization|password|signature/i;

/** Last line of defence for every string that leaves this module: no signed or payment links, no token,
 *  receipt or cookie-shaped values, and a 32-hex run id keeps only its first 8 characters. */
export function redact(value) {
  return String(value ?? '')
    .replace(SIGNED_URL, '[link redacted]')
    .replace(COOKIE_LINE, '[redacted]')
    .replace(SECRET_PAIR, '[redacted]')
    .replace(HEX_RUN, hex => `${hex.slice(0, 8)}…`);
}

function cleanDetail(value, depth = 0) {
  if (typeof value === 'string') return redact(value).slice(0, 1000);
  if (typeof value === 'number' || typeof value === 'boolean' || value === null) return value;
  if (depth >= 4) return undefined;
  if (Array.isArray(value)) return value.slice(0, 20).map(item => cleanDetail(item, depth + 1)).filter(item => item !== undefined);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      if (SECRET_KEY.test(key)) continue;
      const kept = cleanDetail(item, depth + 1);
      if (kept !== undefined) out[key] = kept;
    }
    return out;
  }
  return undefined;
}

const squash = text => redact(String(text ?? '').split(/\s+/).filter(Boolean).join(' '));
const LEVELS = new Set(['debug', 'info', 'warn', 'error']);

function make(id, ts, tag, level, source, text, detail) {
  return {
    id, ts, tag, level: LEVELS.has(level) ? level : 'info', source,
    text: squash(text).slice(0, TEXT_MAX), detail: cleanDetail(detail ?? {}) ?? {},
  };
}

// ---------------------------------------------------------------- CloudWatch lines (ported from watch.py)

const LEVEL_NAMES = { DEBUG: 'debug', INFO: 'info', WARN: 'warn', WARNING: 'warn', ERROR: 'error', CRITICAL: 'error', FATAL: 'error' };
const ERRORISH = /\b(ERROR|Error|Exception|Traceback|FAILED|Task timed out)\b/;
const WARNISH = /\b(WARN|WARNING|warn|warning|retry|retrying|fallback)\b/;
const REPORT = /REPORT RequestId: \S+\s+Duration: ([\d.]+) ms.*?(?:Init Duration: ([\d.]+) ms)?\s*$/s;
const CONTRACT = /(?:^|\s)\[(EVENT|VM|PHP|WORKFLOW)\] ?([^]*)$/;
const PHP_ACCESS = /^([A-Z]+) (\S+) (\d{3}) ([\d.]+) ms\b/;
const STAGE_LINE = /^(\S+) (\S+): /;
const STEP_LINE = /^([^\s:]+): /;
const ACCESS_KEYS = ['method', 'route', 'status', 'ms', 'phase', 'round'];
const ACCESS_IN_TEXT = /\{[^{}]*"bf"\s*:\s*"access"[^{}]*\}/;
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const scalar = value => ['string', 'number'].includes(typeof value);

function jsonOf(text) {
  try { return JSON.parse(text); } catch { return undefined; }
}

function accessLevel(status, given) {
  if (typeof given === 'string' && Object.values(LEVEL_NAMES).includes(given.toLowerCase())) return given.toLowerCase();
  status = Number.isInteger(status) ? status : 0;
  return status >= 500 ? 'error' : status >= 400 ? 'warn' : 'info';
}

/** The web access record in a log message: the whole message, nested in `message`/`record`, or embedded in text. */
export function parseAccess(message, depth = 0) {
  if (typeof message === 'string') {
    const text = message.trim();
    let found;
    if (text.startsWith('{') && depth < 3) found = parseAccess(jsonOf(text), depth + 1);
    if (!found) {
      const match = ACCESS_IN_TEXT.exec(text);
      if (match) found = parseAccess(jsonOf(match[0]), depth + 1);
    }
    return found;
  }
  if (!isRecord(message) || depth > 3) return undefined;
  if (message.bf === 'access') {
    const out = {};
    for (const key of ACCESS_KEYS) if (scalar(message[key])) out[key] = message[key];
    out.level = accessLevel(out.status, message.level);
    return out;
  }
  for (const key of ['message', 'record']) {
    const found = parseAccess(message[key], depth + 1);
    if (found) return found;
  }
  return undefined;
}

function accessText(access) {
  const text = `${access.method ?? '?'} ${access.route ?? '?'} ${access.status ?? '?'} ${access.ms ?? '?'} ms`;
  const extra = ['phase', 'round'].map(key => access[key]).filter(Boolean).join(' ');
  return extra ? `${text} · ${extra}` : text;
}

function contractLevel(tag, text, hint) {
  if (tag === 'PHP') {
    const match = PHP_ACCESS.exec(text);
    if (match) {
      const status = Number(match[3]);
      return status >= 500 ? 'error' : status >= 400 ? 'warn' : 'info';
    }
  }
  if (hint) return hint;
  if (ERRORISH.test(text) || text.includes(' failed')) return 'error';
  return WARNISH.test(text) ? 'warn' : 'info';
}

/** `[EVENT] ...`, `[VM] ...`, `[PHP] ...` or `[WORKFLOW] ...`, also inside a JSON record's `message`. */
export function parseContract(message) {
  let hint;
  message = message.trim();
  if (message.startsWith('{')) {
    const record = jsonOf(message);
    if (isRecord(record) && typeof record.message === 'string') {
      message = record.message.trim();
      hint = LEVEL_NAMES[String(record.level ?? '').toUpperCase()];
    }
  }
  const match = CONTRACT.exec(message);
  if (!match) return undefined;
  const tag = match[1], text = match[2].split(/\s+/).filter(Boolean).join(' ');
  let detail = {};
  if (tag === 'PHP') {
    const php = PHP_ACCESS.exec(text);
    if (php) detail = { method: php[1], path: php[2], status: Number(php[3]), ms: Number(php[4]) };
  } else if (tag === 'EVENT') {
    const stage = STAGE_LINE.exec(text);
    if (stage) detail = { stage: stage[1], option: stage[2] === '-' ? null : stage[2] };
  } else if (tag === 'WORKFLOW') {
    const step = STEP_LINE.exec(text);
    if (step) detail = { step: step[1] };
  }
  return { tag, level: contractLevel(tag, text, hint), text, detail };
}

/** `/aws/lambda/web/black-friday-four` -> `web/black-friday-four`. */
export function logSource(group) {
  return group.replace(/^\/aws\/lambda-microvms\//, '').replace(/^\/aws\/lambda\//, '') || 'logs';
}

/** Which kind of log a group is, from its name: WEB, WORKFLOW, EVENT (the evaluator) or MICROVM. */
export function groupTag(group) {
  if (group.startsWith('/aws/lambda-microvms/')) return 'MICROVM';
  if (group.startsWith('/aws/lambda/web/')) return 'WEB';
  if (/-workflow$/.test(group)) return 'WORKFLOW';
  if (/-evaluator$/.test(group)) return 'EVENT';
  return 'EVENT';
}

const shown = (message, record) => String(record?.message ?? message);
const seconds = ms => (Number(ms) / 1000);

/** One CloudWatch message -> {tag, level, text, detail} or undefined (dropped). Port of watch.py's handle_log. */
export function classify(groupKind, rawMessage) {
  const message = String(rawMessage ?? '').trim();
  if (!message) return undefined;
  const tag = groupKind;
  const failure = (label, text, extra = {}) => ({
    tag: 'ERROR', level: 'error', text: `${label}: ${text}`.slice(0, ERROR_MAX), detail: { raw: message.slice(0, 2000), ...extra },
  });
  if (tag === 'WEB') {
    const access = parseAccess(message);
    if (access) return { tag: 'WEB', level: access.level, text: accessText(access), detail: access };
  } else {
    const line = parseContract(message);
    if (line) return line;
  }
  const record = message.startsWith('{') ? jsonOf(message) : undefined;
  if (tag === 'WEB' && isRecord(record)) {
    const kind = String(record.type ?? ''), body = isRecord(record.record) ? record.record : {};
    if (kind.startsWith('platform.')) {
      const metrics = isRecord(body.metrics) ? body.metrics : {};
      if (kind === 'platform.invokeSummary') {
        return {
          tag: 'WEB', level: 'info', text: `environment served ${Number(metrics.requests) || 0} requests`,
          detail: { requests: Number(metrics.requests) || 0, concurrent: Number(metrics.concurrentRequests) || 0 },
        };
      }
      const bad = ['platform.invokeReport', 'platform.initReport'].includes(kind) && body.status && body.status !== 'success';
      return {
        tag: 'WEB', level: bad ? 'error' : 'debug',
        text: kind + (body.status ? ` ${body.status}` : ''), detail: { type: kind, ...(body.status && { status: body.status }) },
      };
    }
    if (record.level === 'ERROR' || ERRORISH.test(String(record.message ?? ''))) return failure('web', shown(message, record));
    return undefined;
  }
  if (tag === 'WORKFLOW' && isRecord(record)) {
    if (record.type === 'platform.report') {
      const body = isRecord(record.record) ? record.record : {};
      const metrics = isRecord(body.metrics) ? body.metrics : {};
      const cold = metrics.initDurationMs ? ` (cold start ${seconds(metrics.initDurationMs).toFixed(1)} s)` : '';
      const ok = body.status === 'success';
      return {
        tag: 'WORKFLOW', level: ok ? 'info' : 'error',
        text: `Durable invocation ${seconds(metrics.durationMs ?? 0).toFixed(2)} s${cold}${ok ? '' : ` · ${body.status}`}`,
        detail: { duration_s: seconds(metrics.durationMs ?? 0), ...(body.status && { status: body.status }) },
      };
    }
    if (['ERROR', 'CRITICAL'].includes(record.level) || ERRORISH.test(String(record.message ?? ''))) {
      return failure('workflow', shown(message, record));
    }
    if ('fallback' in record) {
      return {
        tag: 'ERROR', level: 'warn', text: `workflow fell back to recorded evidence: ${record.fallback} · ${record.error ?? ''}`.slice(0, ERROR_MAX),
        detail: { fallback: record.fallback, ...(record.action && { action: record.action }) },
      };
    }
    if ('timeline_error' in record) {
      return {
        tag: 'ERROR', level: 'warn', text: `workflow timeline write failed: ${record.timeline_error}`.slice(0, ERROR_MAX),
        detail: {},
      };
    }
    return undefined;
  }
  if (tag === 'EVENT') {
    const match = REPORT.exec(message);
    if (match) {
      const cold = match[2] ? ` (cold start ${(Number(match[2]) / 1000).toFixed(1)} s)` : '';
      return {
        tag: 'EVENT', level: 'info', text: `Event judge done in ${(Number(match[1]) / 1000).toFixed(1)} s${cold}`,
        detail: { duration_s: Number(match[1]) / 1000 },
      };
    }
    if (/^(START|END|INIT_START)/.test(message)) return undefined;
    if (ERRORISH.test(message)) return failure('event', message);
    return { tag: 'EVENT', level: 'info', text: message, detail: {} };
  }
  if (tag === 'MICROVM') {
    let text = message;
    if (isRecord(record)) text = String(record.message ?? record.log ?? message).trim();
    if (text.startsWith('#') && !ERRORISH.test(text)) return undefined;   // image build output
    if (ERRORISH.test(text) || text.includes(' failed with code ') || text.includes('php: PHP ')) return failure('microvm', text);
    return { tag: 'MICROVM', level: 'info', text, detail: {} };
  }
  if (ERRORISH.test(message)) return failure(tag.toLowerCase(), message);
  return undefined;
}

/**
 * The id of a CloudWatch line: a hash of group, timestamp and message. Live Tail results carry no event id,
 * so both sources (Live Tail and FilterLogEvents) derive it from the same three fields and agree on it.
 */
export function cloudWatchId(group, raw) {
  return `cw:${createHash('sha1').update(`${group}\n${Number(raw.timestamp)}\n${raw.message}`).digest('hex').slice(0, 16)}`;
}

/** A CloudWatch event -> an event, or undefined when it is noise. Id and ts come from the log line itself. */
export function fromCloudWatch(group, raw) {
  const line = classify(groupTag(group), raw.message);
  if (!line) return undefined;
  return make(cloudWatchId(group, raw), Number(raw.timestamp), line.tag, line.level, logSource(group), line.text, line.detail);
}

/** `arn:aws:logs:<region>:<account>:log-group:<name>[:*]` or `<account>:<name>` -> `<name>`. */
export const groupOfIdentifier = identifier => String(identifier ?? '').split('log-group:').pop().replace(/:\*$/, '').replace(/^\d{12}:/, '');

// ---------------------------------------------------------------- Durable history (ported from watch.py durable_line)

const SKIP_STARTED = new Set(['StepStarted', 'ContextStarted']);
const stamp = value => new Date(value).getTime();

/** [text, 'ERROR'|null] or null. `started` maps operation id -> start time in seconds, and is updated. */
export function durableLine(event, started) {
  const kind = event.EventType, name = event.Name ?? '', sub = event.SubType ?? '';
  const at = stamp(event.EventTimestamp) / 1000;
  const op = event.Id;
  const elapsed = op in started ? ` ${(at - started[op]).toFixed(1)} s` : '';
  if (kind.endsWith('Started') && op) started[op] = at;
  if (sub === 'ParallelBranch') return null;
  if (kind === 'ExecutionStarted') return ['execution started', null];
  if (kind === 'ExecutionSucceeded') return ['execution finished: game over', null];
  if (['ExecutionFailed', 'ExecutionTimedOut', 'ExecutionStopped'].includes(kind)) return [`execution ${kind.slice(9).toLowerCase()}`, 'ERROR'];
  if (kind === 'InvocationCompleted') return ['invocation ended · workflow suspended; the next one replays from checkpoints', null];
  if (kind === 'ContextStarted' && sub === 'Parallel') return [`${name}: started in parallel`, null];
  if (SKIP_STARTED.has(kind)) return null;
  if (kind === 'ChainedInvokeStarted') return [`${name}: invoke Event judge (MicroVM)`, null];
  if (kind === 'CallbackStarted') return [`${name}: waiting for the presenter to Close`, null];
  if (kind === 'CallbackSucceeded') return [`${name}: Close received after${elapsed}`, null];
  if (kind.endsWith('Succeeded')) {
    const label = { Step: 'step', ChainedInvoke: 'invoke', Parallel: 'parallel' }[sub] ?? (sub.toLowerCase() || 'op');
    return [`${name}: ${label} done${elapsed.trim() === '0.0 s' ? '' : elapsed}`, null];
  }
  if (/(Failed|TimedOut|Stopped)$/.test(kind)) return [`${name}: ${sub || 'op'} ${kind}${elapsed}`, 'ERROR'];
  return [`${name || '-'}: ${kind}`, null];
}

const TERMINAL = new Set(['ExecutionSucceeded', 'ExecutionFailed', 'ExecutionTimedOut', 'ExecutionStopped']);

/** The meaningful history events of one execution as events. Ids: du:<last 12 of the ARN>:<EventId>. */
export function fromDurable(arn, history) {
  const sorted = [...history].sort((a, b) => stamp(a.EventTimestamp) - stamp(b.EventTimestamp)
    || Number(a.EventId ?? 0) - Number(b.EventId ?? 0));
  const started = {}, out = [];
  for (const event of sorted) {
    const line = durableLine(event, started);
    if (!line) continue;
    out.push(make(`du:${arn.slice(-12)}:${event.EventId}`, stamp(event.EventTimestamp), 'DURABLE', line[1] ? 'error' : 'info',
      'durable', line[0], { event_type: event.EventType, name: event.Name, sub_type: event.SubType }));
  }
  return out;
}

// ---------------------------------------------------------------- game state

const TAGS = new Set(['GAME', 'DURABLE', 'EVENT', 'MICROVM', 'WEB', 'WORKFLOW', 'VM', 'PHP', 'ERROR']);
const DEFAULT_SOURCE = { GAME: 'arena', MICROVM: 'arena', DURABLE: 'durable', WEB: 'web' };

function timelineLevel(text) {
  if (ERRORISH.test(text) || text.includes(' failed')) return 'error';
  return WARNISH.test(text) ? 'warn' : 'info';
}

/** `timeline#<run>` lines (written by the workflow and the web function) -> events. Ids: tl:<run8>:<line id>. */
export function fromTimeline(runId, lines) {
  const out = [];
  lines.forEach((line, index) => {
    if (!isRecord(line) || !Number.isFinite(Number(line.t))) return;
    const tag = TAGS.has(line.tag) ? line.tag : 'GAME';
    const source = typeof line.log_group === 'string' && line.log_group ? logSource(line.log_group) : DEFAULT_SOURCE[tag] ?? 'timeline';
    const text = String(line.text ?? '');
    out.push(make(`tl:${runId.slice(0, 8)}:${line.id ?? `n${index}`}`, Number(line.t), tag, timelineLevel(text), source, text,
      isRecord(line.detail) ? line.detail : {}));
  });
  return out;
}

/** {run8, phase, round, players, picked}: counts only, no names and no tokens. */
export function snapshot(item) {
  if (!item) return { run8: null, phase: null, round: null, players: 0, picked: null };
  const text = name => item[name]?.S ?? null;
  const roster = jsonOf(text('teams_data') ?? '[]');
  const players = Array.isArray(roster) ? roster.filter(isRecord) : [];
  const phase = text('phase'), round = text('round');
  const counting = ['open', 'closing'].includes(phase) && round;
  return {
    run8: text('run_id')?.slice(0, 8) ?? null, phase, round,
    players: players.length,
    picked: counting ? players.filter(player => isRecord(player.picks) && player.picks[round]).length : null,
  };
}

// ---------------------------------------------------------------- the shared hub

const byTime = (a, b) => a.ts - b.ts || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

/**
 * One poller per environment, shared by every connection. It runs only while somebody is connected or
 * asked for /events within `idleMs`; AWS calls never scale with viewers. `db`, `lambda` and `logs` are
 * SDK clients (anything with send(command)); `now` is the clock.
 */
export const TAIL_IDLE_MS = 60_000;       // Live Tail is closed after this long without a client
export const FILTER_GAP_MS = 10_000;      // with Live Tail, FilterLogEvents only backfills and fills gaps this often

export function createHub({ db, lambda, logs, table, groups, workflowFunction, accountId, region, now = Date.now, pollMs = 2000, idleMs = 10_000,
  filterMs = FILTER_GAP_MS, tailIdleMs = TAIL_IDLE_MS, tailBackoffMs = 1000, setTimer = setTimeout,
  clearTimer = clearTimeout, report = () => {} }) {
  const known = groups.filter(group => group && !group.endsWith('.local'));
  const buffer = new Map();           // id -> event
  const seenCloudWatch = new Map();   // every inspected CloudWatch line id (dropped ones too) -> ts
  const groupMax = new Map();         // group -> newest timestamp FilterLogEvents has returned
  // Live Tail is the primary CloudWatch source when the account is known; otherwise FilterLogEvents polls alone.
  const arns = new Map(groups.filter(group => group && !group.endsWith('.local'))
    .map(group => [group, `arn:aws:logs:${region}:${accountId}:log-group:${group}`]));
  const tailEnabled = Boolean(accountId && region);
  const tail = { active: false, gen: 0, controller: undefined, check: undefined, sleep: undefined, wake: undefined };
  let lastFilterAt = -Infinity;
  const listeners = new Set();
  const durable = new Map();          // arn -> complete history of a finished execution
  let sorted = [], dirty = false, status = snapshot(undefined);
  let connections = 0, lastTouch = -Infinity, timer, running = false, inflight, polls = 0, lastDone = -Infinity;


  function insert(events, added) {
    const cutoff = now() - WINDOW_MS;
    for (const event of events) {
      if (event.ts < cutoff || buffer.has(event.id)) continue;
      buffer.set(event.id, event);
      added.push(event);
      dirty = true;
    }
  }
  function prune() {
    const cutoff = now() - WINDOW_MS;
    for (const [id, event] of buffer) if (event.ts < cutoff) { buffer.delete(id); dirty = true; }
    for (const [id, ts] of seenCloudWatch) if (ts < cutoff) seenCloudWatch.delete(id);
    if (!dirty) return;
    sorted = [...buffer.values()].sort(byTime);
    let debug = sorted.reduce((count, event) => count + (event.level === 'debug'), 0);
    if (debug > DEBUG_CAP) {
      for (const event of sorted) {
        if (debug <= DEBUG_CAP) break;
        if (event.level === 'debug') { buffer.delete(event.id); debug--; }
      }
      sorted = sorted.filter(event => buffer.has(event.id));
    }
    dirty = false;
  }

  async function getItem(pk) {
    const { Item } = await db.send(new GetItemCommand({ TableName: table, Key: { pk: { S: pk } }, ConsistentRead: true }));
    return Item;
  }
  async function history(arn) {
    if (durable.has(arn)) return durable.get(arn);
    const events = [];
    let marker;
    do {
      const page = await lambda.send(new GetDurableExecutionHistoryCommand({
        DurableExecutionArn: arn, IncludeExecutionData: false, MaxItems: 1000, ...(marker && { Marker: marker }),
      }));
      events.push(...(page.Events ?? []));
      marker = page.NextMarker;
    } while (marker);
    if (events.some(event => TERMINAL.has(event.EventType))) {
      if (durable.size > 4) durable.clear();
      durable.set(arn, events);
    }
    return events;
  }

  async function gameTask() {
    const item = await getItem('arena');
    status = snapshot(item);
    const runId = item?.run_id?.S;
    // Only this game's workflow: the ARN names a function, and the role may read no other.
    const named = item?.execution_arn?.S;
    const arn = named && (!workflowFunction || named.includes(`:function:${workflowFunction}:`)) ? named : undefined;
    const [lines, steps] = await Promise.all([
      runId ? getItem(`timeline#${runId}`).then(row => (row?.lines?.L ?? []).map(entry => jsonOf(entry.S))) : [],
      arn ? history(arn) : [],
    ]);
    return [...(runId ? fromTimeline(runId, lines) : []), ...(arn ? fromDurable(arn, steps) : [])];
  }

  async function groupTask(group) {
    const first = now();
    const seen = groupMax.get(group);
    const startTime = Math.max(seen === undefined ? -Infinity : seen - OVERLAP_MS, first - BACKFILL_MS);
    const out = [];
    let nextToken, newest = seen;
    for (let page = 0; page < PAGE_CAP; page++) {
      const reply = await logs.send(new FilterLogEventsCommand({
        logGroupName: group, startTime: Math.floor(startTime), limit: 10000, ...(nextToken && { nextToken }),
      }));
      for (const raw of reply.events ?? []) {
        newest = Math.max(newest ?? -Infinity, raw.timestamp);
        const id = cloudWatchId(group, raw);
        if (seenCloudWatch.has(id)) continue;
        seenCloudWatch.set(id, raw.timestamp);
        const event = fromCloudWatch(group, raw);
        if (event) out.push(event);
      }
      nextToken = reply.nextToken;
      if (!nextToken) break;
    }
    if (newest !== undefined) groupMax.set(group, newest);
    return out;
  }

  function failureEvent(source, error) {
    const at = now();
    return make(`wt:${source}:${error?.name ?? 'Error'}:${Math.floor(at / 60_000)}`, at, 'WATCH', 'warn', 'logs',
      `${source} unavailable: ${error?.name ?? 'Error'}`, {});
  }

  function notify(added, snapshotNow) {
    for (const listener of [...listeners]) {
      try { listener(added, snapshotNow); } catch { listeners.delete(listener); }
    }
  }

  async function runPoll() {
    // With Live Tail running, the group reads are only the backfill and a gap filler every `filterMs`.
    const filterDue = !tailEnabled || now() - lastFilterAt >= filterMs;
    if (filterDue) lastFilterAt = now();
    const tasks = [['game', gameTask()], ...(filterDue ? known.map(group => [logSource(group), groupTask(group)]) : [])];
    const results = await Promise.allSettled(tasks.map(([, task]) => task));
    const events = [];
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') { events.push(...result.value); return; }
      const error = result.reason;
      if (error?.name === 'ResourceNotFoundException') return;   // a group that does not exist yet
      report(tasks[index][0], error);
      events.push(failureEvent(tasks[index][0], error));
    });
    const added = [];
    insert(events, added);
    added.sort(byTime);
    prune();
    polls++;
    lastDone = now();
    notify(added.filter(event => buffer.has(event.id)), status);
  }

  // ---- Live Tail: one session per environment, results pushed to clients as they arrive
  function pushLive(events) {
    const added = [];
    insert(events, added);
    if (!added.length) return;
    added.sort(byTime);
    prune();
    notify(added.filter(event => buffer.has(event.id)), undefined);
  }
  function takeTail(results) {
    const events = [];
    for (const raw of results ?? []) {
      const group = groupOfIdentifier(raw.logGroupIdentifier);
      if (!arns.has(group) || !Number.isFinite(Number(raw.timestamp))) continue;
      const id = cloudWatchId(group, raw);
      if (seenCloudWatch.has(id)) continue;
      seenCloudWatch.set(id, Number(raw.timestamp));
      const event = fromCloudWatch(group, raw);
      if (event) events.push(event);
    }
    pushLive(events);
  }
  const pause = delay => new Promise(resolve => { tail.wake = resolve; tail.sleep = setTimer(resolve, delay); });
  async function tailLoop(generation) {
    let failures = 0;
    while (generation === tail.gen) {
      const controller = new AbortController();
      tail.controller = controller;
      try {
        const { responseStream } = await logs.send(new StartLiveTailCommand({ logGroupIdentifiers: [...arns.values()] }),
          { abortSignal: controller.signal });
        for await (const update of responseStream) {
          if (generation !== tail.gen) return;
          if (update.sessionStart) failures = 0;
          if (update.sessionUpdate) takeTail(update.sessionUpdate.sessionResults);
        }
        failures = 0;   // the session ended on its own (the 3 hour cap): start the next one
      } catch (error) {
        if (generation !== tail.gen) return;
        failures++;
        report('live tail', error);
        pushLive([failureEvent('live tail', error)]);
      }
      if (generation !== tail.gen) return;
      await pause(Math.min(30_000, tailBackoffMs * 2 ** Math.max(0, failures - 1)));
    }
  }
  function armTailCheck() {
    clearTimer(tail.check);
    const quiet = connections > 0 ? 0 : now() - lastTouch;
    tail.check = setTimer(() => {
      tail.check = undefined;
      if (connections > 0 || now() - lastTouch < tailIdleMs) armTailCheck(); else stopTail();
    }, connections > 0 ? tailIdleMs : Math.max(10, tailIdleMs - quiet));
  }
  function ensureTail() {
    if (!tailEnabled || tail.active) return;
    tail.active = true;
    tailLoop(++tail.gen);
    armTailCheck();
  }
  function stopTail() {
    tail.active = false;
    tail.gen++;
    tail.controller?.abort();
    clearTimer(tail.check);
    clearTimer(tail.sleep);
    tail.wake?.();
  }

  function poll() {
    inflight ??= runPoll().catch(error => report('poll', error)).finally(() => { inflight = undefined; });
    return inflight;
  }

  const interested = () => connections > 0 || now() - lastTouch < idleMs;
  async function loop() {
    running = true;
    try {
      for (;;) {
        await poll();
        if (!interested()) break;
        await new Promise(resolve => { timer = setTimer(resolve, pollMs); });
        if (!interested()) break;
      }
    } finally { running = false; timer = undefined; }
  }
  function ensureRunning() {
    ensureTail();
    if (!running) { loop(); }
  }

  return {
    /** A /events request or status check: keeps the poller alive and returns once data is fresh. */
    async touch() {
      lastTouch = now();
      const wasRunning = running;
      ensureRunning();
      if (!wasRunning || polls === 0 || now() - lastDone > pollMs * 1.5) await (inflight ?? poll());
    },
    /** A connected client: events added by a poll or by Live Tail go to `listener(added, status)`; status is
     *  undefined for a Live Tail push. Returns release(). */
    attach(listener) {
      connections++;
      lastTouch = now();
      listeners.add(listener);
      ensureRunning();
      let released = false;
      return () => {
        if (released) return;
        released = true;
        connections--;
        lastTouch = now();
        listeners.delete(listener);
        if (tail.active && connections === 0) armTailCheck();
      };
    },
    /** Buffered events at or after cursor - 60 s (or all of them without a cursor). */
    eventsAfter(cursor, debug) {
      let from = 0;
      if (Number.isFinite(cursor)) {
        const floor = cursor - OVERLAP_MS;
        let low = 0, high = sorted.length;
        while (low < high) { const mid = (low + high) >> 1; if (sorted[mid].ts < floor) low = mid + 1; else high = mid; }
        from = low;
      }
      const out = [];
      for (let index = from; index < sorted.length; index++) if (debug || sorted[index].level !== 'debug') out.push(sorted[index]);
      return out;
    },
    get status() { return status; },
    get polls() { return polls; },
    get active() { return running; },
    poll,
    get tailActive() { return tail.active; },
    stop() { clearTimer(timer); stopTail(); },
  };
}
