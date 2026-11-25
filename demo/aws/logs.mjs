import { ROUNDS } from './game.mjs';
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

// ---------------------------------------------------------------- CloudWatch lines

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

/** One CloudWatch message -> {tag, level, text, detail} or undefined (dropped). */
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

/** The short `source` of a CloudWatch line: web, workflow, evaluator or vm ([VM]/[PHP] lines and the MicroVM image group). */
export function sourceOf(group, tag) {
  if (tag === 'VM' || tag === 'PHP') return 'vm';
  return { WEB: 'web', WORKFLOW: 'workflow', EVENT: 'evaluator', MICROVM: 'vm' }[groupTag(group)];
}

const STAGE_IN_TEXT = /(?:^|\[|\s)(launch|migrate|compare)(?:\s+(careful|fast|-))?(?=[\]:\s]|$)/;

/** Which launch, migrate or compare stage (and option) a VM, PHP, EVENT or MicroVM line is about, from its own label. */
export function stageOf(text) {
  const match = STAGE_IN_TEXT.exec(String(text).slice(0, 80));
  return match ? { stage: match[1], ...(match[2] && match[2] !== '-' && { option: match[2] }) } : undefined;
}

/** A CloudWatch event -> an event, or undefined when it is noise. Id and ts come from the log line itself. */
export function fromCloudWatch(group, raw) {
  const line = classify(groupTag(group), raw.message);
  if (!line) return undefined;
  const detail = ['VM', 'PHP', 'EVENT', 'MICROVM'].includes(line.tag) && !line.detail.stage ? { ...line.detail, ...stageOf(line.text) } : line.detail;
  return make(cloudWatchId(group, raw), Number(raw.timestamp), line.tag, line.level, sourceOf(group, line.tag), line.text, detail);
}

/** `arn:aws:logs:<region>:<account>:log-group:<name>[:*]` or `<account>:<name>` -> `<name>`. */
export const groupOfIdentifier = identifier => String(identifier ?? '').split('log-group:').pop().replace(/:\*$/, '').replace(/^\d{12}:/, '');

// ---------------------------------------------------------------- Durable history

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

// The workflow's timeline lines for rounds (registered, open-, close-, plan-, score-, results-, final) repeat what the
// Durable history and the [WORKFLOW] log lines already say, so only the real-VM evidence and the web function's own
// lines are kept. Nothing from the timeline is ever tagged EVENT or GAME.
const VM_EVIDENCE = /^(launch|migrate-(careful|fast)|compare-(careful|fast))$/;
const WEB_LINE = /^web-(start|close-.+|reset)$/;

function evidenceLevel(text) {
  if (ERRORISH.test(text) || text.includes(' failed')) return 'error';
  return WARNISH.test(text) || /\bLOST\b/.test(text) ? 'warn' : 'info';
}

/** `timeline#<run>` lines -> events. Ids: tl:<run8>:<line id>. */
export function fromTimeline(runId, lines) {
  const out = [];
  for (const line of lines) {
    if (!isRecord(line) || !Number.isFinite(Number(line.t)) || typeof line.id !== 'string') continue;
    const web = WEB_LINE.test(line.id);
    if (!web && !VM_EVIDENCE.test(line.id)) continue;
    const text = String(line.text ?? '');
    const detail = {
      ...(isRecord(line.detail) ? line.detail : {}),
      ...(typeof line.log_group === 'string' && line.log_group && { log_group: line.log_group }),
    };
    out.push(make(`tl:${runId.slice(0, 8)}:${line.id}`, Number(line.t), web ? 'WEB' : 'MICROVM', web ? 'info' : evidenceLevel(text),
      web ? 'web' : 'workflow', text, detail));
  }
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

// ---------------------------------------------------------------- roster, story cards and follow lines

const ROUND_LIST = ROUNDS.map(round => round.id);
const ROUND = new Map(ROUNDS.map(round => [round.id, round]));
const optionLabel = (round, option) => ROUND.get(round)?.options.find(candidate => candidate.id === option)?.label ?? String(option);
const roundName = round => String(ROUND.get(round)?.title ?? round).split('·').pop().trim();
const ordinal = n => { const suffix = ['th', 'st', 'nd', 'rd'], v = n % 100; return `${n}${suffix[(v - 20) % 10] || suffix[v] || suffix[0]}`; };
const dollars = cents => `${cents < 0 ? '-' : ''}$${Math.abs(Math.round(cents / 100)).toLocaleString('en-US')}`;
const sentence = text => (/[.!?]$/.test(text) ? text : `${text}.`);
const hash8 = text => createHash('sha1').update(text).digest('hex').slice(0, 8);
const earliest = (...values) => { const known = values.filter(Number.isFinite); return known.length ? Math.min(...known) : undefined; };

/** The roster as the log may show it: id, display name and picks. Never token_hash, never anything else. */
export function parseRoster(item) {
  const roster = jsonOf(item?.teams_data?.S ?? '[]');
  return (Array.isArray(roster) ? roster : []).filter(player => isRecord(player) && typeof player.id === 'string').map(player => ({
    id: player.id, name: String(player.name ?? '?').slice(0, 40),
    picks: Object.fromEntries(Object.entries(isRecord(player.picks) ? player.picks : {}).filter(([, option]) => typeof option === 'string')),
  }));
}

function playerEvent(run8, player, kind, at, level, text, detail) {
  return make(`pl:${run8}:${player}:${kind}`, at, 'PLAYER', level, 'arena', text, { player, ...detail });
}

/** What changed between two rosters of one run. `previous` is a Map id -> player, or undefined for the first look. */
export function diffRoster(previous, players, { run8, at, announceJoins }) {
  const out = [];
  if (!previous) {
    if (announceJoins) {
      players.forEach(player => out.push(playerEvent(run8, player.id, 'join', at, 'info', `${player.name} joined (${players.length} players)`, {})));
    }
    return out;
  }
  const ids = new Set(players.map(player => player.id));
  for (const player of players) {
    const before = previous.get(player.id);
    if (!before) {
      out.push(playerEvent(run8, player.id, 'join', at, 'info', `${player.name} joined (${players.length} players)`, {}));
      continue;
    }
    if (before.name !== player.name) {
      out.push(playerEvent(run8, player.id, `ren:${hash8(`${before.name}>${player.name}`)}`, at, 'info', `${before.name} renamed to ${player.name}`, {}));
    }
    for (const [round, option] of Object.entries(player.picks)) {
      const old = before.picks[round];
      if (old === option) continue;
      const where = `in ${roundName(round)}`;
      out.push(old === undefined
        ? playerEvent(run8, player.id, `pick:${round}`, at, 'debug', `${player.name} picked ${optionLabel(round, option)} ${where}`, { round, option })
        : playerEvent(run8, player.id, `pick:${round}:${hash8(`${old}>${option}`)}`, at, 'debug',
          `${player.name} changed their pick ${where} from ${optionLabel(round, old)} to ${optionLabel(round, option)}`, { round, option, was: old }));
    }
  }
  for (const [id, before] of previous) {
    if (!ids.has(id)) out.push(playerEvent(run8, id, 'rm', at, 'info', `${before.name} was removed (${players.length} players left)`, {}));
  }
  return out;
}

/** The published rows of a results item, cut down to what the lines need. */
export function parseResults(item) {
  const data = jsonOf(item?.data?.S ?? '');
  if (!isRecord(data) || !Array.isArray(data.rows)) return undefined;
  const num = value => (Number.isFinite(value) ? value : undefined);
  return {
    market: num(data.market),
    rows: data.rows.filter(row => isRecord(row) && !row.house && typeof row.id === 'string').map(row => ({
      id: row.id, name: String(row.name ?? '?').slice(0, 40), bot: Boolean(row.bot), option: String(row.option ?? ''),
      roll: num(row.roll), risk: num(row.risk), event: typeof row.event === 'string' ? row.event : '',
      rank: num(row.rank), cumulative_cents: num(row.cumulative_cents), evidence: typeof row.evidence === 'string' ? row.evidence : '',
    })),
  };
}

/** One player's lines about one round's result: dice against risk, then rank and running total. */
export function rowLines(row, round) {
  const out = [];
  if (row.risk > 0 && row.roll !== undefined) {
    out.push(sentence(`${row.name} rolled ${row.roll} against ${optionLabel(round, row.option)}'s ${row.risk}${row.event ? `: ${row.event}` : ''}`));
  } else if (row.event) {
    out.push(sentence(`${row.name}: ${row.event}`));
  }
  if (Number.isInteger(row.rank) && row.cumulative_cents !== undefined) {
    out.push(sentence(`${row.name} is ${ordinal(row.rank)} on ${dollars(row.cumulative_cents)}${row.evidence ? ` (evidence: ${row.evidence})` : ''}`));
  }
  return out;
}

function pickLine(player, round, option, evidence) {
  let line = `${player.name} picked ${optionLabel(round, option)}`;
  const result = evidence?.[option];
  if (round === 'growth' && isRecord(result) && result.source === 'real' && typeof result.cart_kept === 'boolean') {
    line += `: their shop is the real VM that ${result.cart_kept ? 'kept' : 'lost'} Maya's cart`;
  }
  return sentence(line);
}

const SEQ = { lobby: 0, start: 1, launch: 2, migrations: 3, checks: 4, final: 100 };

/** Which operations Durable has started and finished, by name, with the history's own timestamps. */
function operations(history) {
  const ops = new Map();
  let runs = 0, microvms = 0, ended, begun;
  for (const event of history) {
    const at = stamp(event.EventTimestamp), kind = String(event.EventType);
    if (kind === 'InvocationCompleted') runs++;
    if (kind === 'ChainedInvokeStarted') microvms++;
    if (kind === 'ExecutionSucceeded') ended = at;
    if (kind === 'ExecutionStarted') begun = at;
    if (!event.Name) continue;
    const op = ops.get(event.Name) ?? {};
    if (kind.endsWith('Started')) op.started = Math.min(op.started ?? at, at);
    if (kind.endsWith('Succeeded')) op.succeeded = Math.max(op.succeeded ?? at, at);
    ops.set(event.Name, op);
  }
  return { ops, runs, microvms, ended, begun };
}
const seconds1 = (from, to) => (Number.isFinite(from) && Number.isFinite(to) && to >= from ? Math.round((to - from) / 100) / 10 : undefined);

/**
 * The story cards of the current run, in order. Every number comes from a field of the arena item, the Durable
 * history, the timeline lines or a results item; a number that is not there is left out of the sentence.
 * `firstSeen` (a Map) pins the time of a card that has no timestamp in the data.
 */
export function buildStory({ item, players, history, lines, results, firstSeen, at }) {
  const runId = item?.run_id?.S;
  if (!runId) return [];
  const run8 = runId.slice(0, 8), phase = item.phase?.S, round = item.round?.S;
  const { ops, runs, microvms, ended, begun } = operations(history);
  const byId = new Map(lines.filter(isRecord).map(line => [line.id, line]));
  const launch = jsonOf(item.launch_data?.S), evidence = jsonOf(item.evidence_data?.S);
  const cards = [];
  const add = (stage, seq, candidate, title, text, numbers = {}, byPlayer) => {
    const id = `st:${run8}:${stage}`;
    if (!Number.isFinite(candidate)) { if (!firstSeen.has(id)) firstSeen.set(id, at); candidate = firstSeen.get(id); }
    cards.push({ id, ts: candidate, seq, stage, title, text, numbers, ...(byPlayer && { players: byPlayer }) });
  };
  const started = phase !== 'ready';
  const webStart = byId.get('web-start');
  const joined = started ? webStart?.detail?.players : players.length;
  add('lobby', SEQ.lobby, undefined, 'Lobby', started
    ? `The lobby closed${Number.isInteger(joined) ? ` with ${joined} players` : ''}. Until Start, nothing runs in AWS: the Web Function only answers phones.`
    : `Players are joining: ${players.length} so far. Nothing is running in AWS yet. The Web Function only answers phones.`,
  Number.isInteger(joined) ? { players: joined } : {});
  if (started) {
    add('start', SEQ.start, begun ?? webStart?.t, 'Start', "The Web Function started one Durable execution: the game's memory for the whole night.");
  }
  const shop = ops.get('launch-shop');
  if (shop || byId.has('launch')) {
    const clauses = [];
    const real = isRecord(launch) && launch.source === 'real';
    if (real && Array.isArray(launch.checkout_statuses) && launch.checkout_statuses.length) clauses.push('sold a mug');
    if (real && launch.charges === 1) clauses.push('charged exactly once');
    else if (real && Number.isInteger(launch.charges)) clauses.push(`charged ${launch.charges} times`);
    if (shop?.succeeded !== undefined) clauses.push('the VM was destroyed');
    const secs = seconds1(shop?.started, shop?.succeeded);
    // "It sold a mug, charged exactly once, and the VM was destroyed"; with only the VM clause known: "The VM was destroyed".
    const vmOnly = clauses.length === 1 && clauses[0] === 'the VM was destroyed';
    const outcome = !clauses.length ? '' : vmOnly ? 'The VM was destroyed'
      : `It ${clauses.length > 1 ? `${clauses.slice(0, -1).join(', ')}, and ${clauses.at(-1)}` : clauses[0]}`;
    const fallback = isRecord(launch) && launch.source && !real ? ' The real run did not complete, so recorded evidence is used.' : '';
    add('launch', SEQ.launch, shop?.started ?? byId.get('launch')?.t, 'Launch shop',
      `Durable asked an Event judge, which booted a MicroVM running the real PHP shop.${outcome ? ` ${sentence(outcome + (secs !== undefined ? ` (${secs} s)` : ''))}` : ''}${fallback}`,
      { ...(secs !== undefined && { seconds: secs }), ...(real && Number.isInteger(launch.charges) && { charges: launch.charges }) });
  }
  const careful = ops.get('migrate-careful'), fast = ops.get('migrate-fast');
  if (careful || fast || byId.has('migrate-careful') || byId.has('migrate-fast')) {
    const a = seconds1(careful?.started, careful?.succeeded), b = seconds1(fast?.started, fast?.succeeded);
    const timing = a !== undefined && b !== undefined ? ` (${a} s / ${b} s)` : a !== undefined ? ` (careful ${a} s)` : b !== undefined ? ` (fast ${b} s)` : '';
    add('migrations', SEQ.migrations, earliest(careful?.started, fast?.started, byId.get('migrate-careful')?.t), 'Migrations',
      `Two Event judges, two MicroVMs in parallel: careful copies every table, fast skips the carts${timing}.`,
      { ...(a !== undefined && { careful_s: a }), ...(b !== undefined && { fast_s: b }) });
  }
  const checkedC = ops.get('compare-careful'), checkedF = ops.get('compare-fast');
  if (checkedC || checkedF || byId.has('compare-careful') || byId.has('compare-fast')) {
    const verdict = option => (isRecord(evidence?.[option]) && typeof evidence[option].cart_kept === 'boolean'
      ? `${option} ${evidence[option].cart_kept ? 'KEPT' : 'LOST'} it` : undefined);
    const said = [verdict('careful'), verdict('fast')].filter(Boolean);
    add('checks', SEQ.checks, earliest(checkedC?.started, checkedF?.started, byId.get('compare-careful')?.t), 'Checks',
      `Two fresh VMs restore each database and look for Maya's cart${said.length ? `: ${said.join(', ')}` : ''}.`);
  }
  ROUND_LIST.forEach((r, index) => {
    const number = index + 1, current = round === r && ['open', 'closing'].includes(phase);
    const wait = ops.get(`await-close-${r}`), published = ops.get(`publish-results-${r}`);
    const result = results.get(r);
    if (current || wait || result || byId.has(`open-${r}`)) {
      const closed = byId.get(`web-close-${r}`)?.detail ?? byId.get(`close-${r}`)?.detail;
      const picked = current ? players.filter(player => player.picks[r]).length : closed?.picked;
      const total = current ? players.length : closed?.players;
      const counts = Number.isInteger(picked) && Number.isInteger(total) ? `${picked}/${total} picked` : undefined;
      const byPlayer = {};
      for (const player of players) {
        if (player.picks[r]) byPlayer[player.id] = { lines: [pickLine(player, r, player.picks[r], evidence)], pick: player.picks[r] };
      }
      add(`open-${r}`, 10 + 2 * index, wait?.started ?? byId.get(`open-${r}`)?.t, `Round ${number} open`,
        current
          ? `Round ${number} is open. Durable is suspended, waiting for the presenter's Close. No code is running and nothing is billed.${counts ? ` ${counts}.` : ''}`
          : `Round ${number} was open${counts ? `: ${counts}` : ''}. Durable was suspended, waiting for the presenter's Close, and nothing was billed.`,
        { round: number, ...(Number.isInteger(picked) && { picked }), ...(Number.isInteger(total) && { players: total }) }, byPlayer);
    }
    if (wait?.succeeded !== undefined || (current && phase === 'closing') || result) {
      const secs = seconds1(wait?.succeeded, published?.succeeded);
      const scored = result?.rows.filter(row => !row.bot).length;
      const unit = ROUND.get(r)?.market?.unit ?? '';
      const byPlayer = {};
      for (const row of result?.rows ?? []) byPlayer[row.id] = { lines: rowLines(row, r) };
      add(`close-${r}`, 11 + 2 * index, wait?.succeeded, `Round ${number} closed`,
        published?.succeeded !== undefined || result
          ? `Close woke the workflow: it drew the market${result?.market !== undefined ? ` (${result.market}${unit})` : ''}, rolled everyone's dice, scored${scored !== undefined ? ` ${scored}` : ''} players and published${secs !== undefined ? `, in ${secs} s` : ''}. Then it went back to sleep.`
          : "Close woke the workflow: it is drawing the market, rolling everyone's dice and scoring.",
        { round: number, ...(result?.market !== undefined && { market: result.market }), ...(scored !== undefined && { players: scored }), ...(secs !== undefined && { seconds: secs }) }, byPlayer);
    }
  });
  if (phase === 'final' || ended !== undefined) {
    const facts = ['One Durable execution', ...(runs ? [`${runs} short runs of our code`] : []), ...(microvms ? [`${microvms} MicroVMs`] : [])];
    add('final', SEQ.final, ended ?? byId.get('final')?.t, 'Game over', `Game over. ${facts.join(', ')}.`,
      { ...(runs && { runs }), ...(microvms && { microvms }) });
  }
  return cards.sort((a, b) => a.seq - b.seq);
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
  const resultsCache = new Map();     // run#round -> parsed results (immutable once published)
  const firstSeen = new Map();        // card id -> time first seen, for cards the data does not time
  let roster = { run: undefined, players: undefined }, latestPlayers = [], cards = [], storyChanged = [], cardJson = new Map();
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
    const round = item?.results_round?.S;
    const wanted = round ? ROUND_LIST.slice(0, ROUND_LIST.indexOf(round) + 1) : [];
    const [lines, steps] = await Promise.all([
      runId ? getItem(`timeline#${runId}`).then(row => (row?.lines?.L ?? []).map(entry => jsonOf(entry.S))) : [],
      arn ? history(arn) : [],
      ...wanted.map(async r => {
        const key = `${runId}#${r}`;
        if (!resultsCache.has(key)) {
          const parsed = parseResults(await getItem(`results#${runId}#${r}`));
          if (parsed) { if (resultsCache.size > 16) resultsCache.clear(); resultsCache.set(key, parsed); }
        }
      }),
    ]);
    const events = [...(runId ? fromTimeline(runId, lines) : []), ...(arn ? fromDurable(arn, steps) : [])];
    if (!runId) { cards = []; storyChanged = []; return events; }
    const run8 = runId.slice(0, 8), players = parseRoster(item), at = now();
    const results = new Map(wanted.filter(r => resultsCache.has(`${runId}#${r}`)).map(r => [r, resultsCache.get(`${runId}#${r}`)]));
    if (roster.run !== runId) roster = { run: runId, players: undefined };
    events.push(...diffRoster(roster.players, players, { run8, at, announceJoins: item.phase?.S === 'ready' }));
    roster = { run: runId, players: new Map(players.map(player => [player.id, player])) };
    latestPlayers = players;
    for (const [r, result] of results) {
      const done = stamp(steps.find(event => event.Name === `publish-results-${r}` && /Succeeded$/.test(event.EventType))?.EventTimestamp ?? NaN);
      for (const row of result.rows) {
        if (!row.bot && Number.isFinite(done)) events.push(playerEvent(run8, row.id, `res:${r}`, done, 'debug', rowLines(row, r).join(' '), { round: r }));
      }
    }
    cards = buildStory({ item, players, history: steps, lines, results, firstSeen, at });
    const next = new Map(cards.map(card => [card.id, JSON.stringify(card)]));
    storyChanged = cards.filter(card => cardJson.get(card.id) !== next.get(card.id));
    cardJson = next;
    return events;
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

  function notify(added, snapshotNow, story) {
    for (const listener of [...listeners]) {
      try { listener(added, snapshotNow, story); } catch { listeners.delete(listener); }
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
    const changedCards = storyChanged;
    storyChanged = [];
    notify(added.filter(event => buffer.has(event.id)), status, changedCards);
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
    /** Every story card of the current run, in order. */
    storyCards() { return cards; },
    /** The roster for the Follow picker: id and display name only. */
    get players() { return latestPlayers.map(({ id, name }) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name)); },
    get polls() { return polls; },
    get active() { return running; },
    poll,
    get tailActive() { return tail.active; },
    stop() { clearTimer(timer); stopTail(); },
  };
}
