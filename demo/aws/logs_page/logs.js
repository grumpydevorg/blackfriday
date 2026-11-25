'use strict';
// Live log viewer: every filter runs here, in the browser. The server builds the events and shares one
// poll of AWS between every viewer. Events have stable ids, so a reconnect that resends an overlap is harmless.
const MAX_LINES = 10000;
const HELLO_WAIT_MS = 5000;
const POLL_MS = 2000;
const KNOWN_TAGS = ['GAME', 'DURABLE', 'EVENT', 'VM', 'PHP', 'MICROVM', 'WEB', 'WORKFLOW', 'ERROR', 'WATCH'];
const TAG_COLOURS = {
  GAME: '#3fb950', DURABLE: '#d2a8ff', EVENT: '#e3b341', VM: '#39c5cf', PHP: '#79e0ee', MICROVM: '#39c5cf',
  WEB: '#58a6ff', WORKFLOW: '#ff9bce', ERROR: '#ff7b72', WATCH: '#ffffff',
};
const LEVELS = ['debug', 'info', 'warn', 'error'];
const LEVEL_COLOURS = { debug: '#8b949e', info: '#e6edf3', warn: '#f2cc60', error: '#ff7b72' };
const TIME = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Australia/Adelaide', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
});
const DAY_TIME = new Intl.DateTimeFormat('en-AU', {
  timeZone: 'Australia/Adelaide', hour12: false, dateStyle: 'medium', timeStyle: 'medium',
});

const $ = (id) => document.getElementById(id);
const logEl = $('log'), tagsEl = $('tags'), levelsEl = $('levels'), qEl = $('q'), reEl = $('re');
const pauseEl = $('pause'), statusEl = $('status'), shownEl = $('shown'), latestEl = $('latest');
const modeEl = $('mode'), signoutEl = $('signout');

const state = {
  offTags: new Set(),                       // tags switched off; new tags start on
  levels: new Set(['info', 'warn', 'error']), // debug hidden by default
  q: '', regex: false, paused: false,
};
const rows = [];            // {ev, el, hay, tag, level, hidden}
const seen = new Set();     // ids of every event shown or held; bounded with the lines
const tagCounts = {}, levelCounts = {};
const tagChips = {}, levelChips = {};
let incoming = [];          // received, not yet rendered
let held = [];              // received while paused
let matcher = null;         // null = match all
let flushTimer = null;
let lastTs = null;          // newest event timestamp received: the polling cursor
let health = 'connecting';  // connecting | up | down

// ---- URL hash <-> state
function saveHash() {
  const p = new URLSearchParams();
  if (state.offTags.size) p.set('off', [...state.offTags].join(','));
  p.set('lv', LEVELS.filter((l) => state.levels.has(l)).join(','));
  if (state.q) p.set('q', state.q);
  if (state.regex) p.set('re', '1');
  history.replaceState(null, '', '#' + p.toString());
}
function loadHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.has('off')) state.offTags = new Set(p.get('off').split(',').filter(Boolean));
  if (p.has('lv')) state.levels = new Set(p.get('lv').split(',').filter((l) => LEVELS.includes(l)));
  state.q = p.get('q') || '';
  state.regex = p.get('re') === '1';
  qEl.value = state.q;
  reEl.checked = state.regex;
}

// ---- filtering
function buildMatcher() {
  matcher = null;
  qEl.classList.remove('bad');
  if (!state.q) return;
  if (state.regex) {
    try {
      const re = new RegExp(state.q, 'i');
      matcher = (hay, text) => re.test(text) || re.test(hay);
    } catch (e) {
      qEl.classList.add('bad');   // invalid pattern: keep showing everything rather than nothing
    }
    return;
  }
  const needle = state.q.toLowerCase();
  matcher = (hay) => hay.toLowerCase().includes(needle);
}
function visible(row) {
  return !state.offTags.has(row.tag) && state.levels.has(row.level) && (!matcher || matcher(row.hay, row.ev.text));
}
function applyFilters() {
  const stick = atBottom();
  buildMatcher();
  let shown = 0;
  for (const row of rows) {
    const show = visible(row);
    if (show === row.hidden) { row.hidden = !show; row.el.hidden = !show; }
    if (show) shown++;
  }
  updateCounts(shown);
  saveHash();
  syncDebug();
  if (stick) scrollToEnd();
}
function countShown() { let n = 0; for (const r of rows) if (!r.hidden) n++; return n; }

// ---- chips
function makeChip(parent, label, colour, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chip';
  b.style.setProperty('--c', colour);
  const name = document.createElement('span'), n = document.createElement('span');
  name.textContent = label;
  n.className = 'n';
  n.textContent = '0';
  b.append(name, n);
  b.addEventListener('click', onClick);
  parent.append(b);
  return { b, n };
}
function ensureTag(tag) {
  if (tagChips[tag]) return;
  tagCounts[tag] = 0;
  tagChips[tag] = makeChip(tagsEl, tag, TAG_COLOURS[tag] || '#c9d1d9', () => {
    if (state.offTags.has(tag)) state.offTags.delete(tag); else state.offTags.add(tag);
    syncChips();
    applyFilters();
  });
}
function syncChips() {
  for (const [tag, c] of Object.entries(tagChips)) c.b.classList.toggle('on', !state.offTags.has(tag));
  for (const [lv, c] of Object.entries(levelChips)) c.b.classList.toggle('on', state.levels.has(lv));
}
function updateCounts(shown) {
  for (const [tag, c] of Object.entries(tagChips)) c.n.textContent = tagCounts[tag];
  for (const [lv, c] of Object.entries(levelChips)) c.n.textContent = levelCounts[lv];
  shownEl.textContent = `${shown === undefined ? countShown() : shown} shown / ${rows.length} in memory`
    + (held.length ? ` / ${held.length} held` : '');
}

// ---- rows
function formatTime(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts || '?');
  return TIME.format(d) + '.' + String(d.getMilliseconds()).padStart(3, '0');
}
function span(cls, text) {
  const s = document.createElement('span');
  s.className = cls;
  s.textContent = text;
  return s;
}
function toggleDetail(row) {
  const next = row.el.nextElementSibling;
  if (next && next.classList.contains('detail')) { next.remove(); row.el.classList.remove('open'); return; }
  const ev = row.ev;
  const box = document.createElement('div');
  box.className = 'detail';
  const when = new Date(ev.ts);
  const head = `${Number.isNaN(when.getTime()) ? ev.ts : DAY_TIME.format(when) + ' Adelaide'} · ${ev.ts}\n`
    + `tag ${ev.tag} · level ${ev.level} · source ${ev.source}\nid ${ev.id}\n\n${ev.text}`;
  const detail = ev.detail && Object.keys(ev.detail).length ? '\n\n' + JSON.stringify(ev.detail, null, 2) : '';
  box.textContent = head + detail;
  row.el.after(box);
  row.el.classList.add('open');
  box.scrollIntoView({ block: 'nearest' });
}
function makeRow(ev) {
  const tag = String(ev.tag || '?');
  const level = LEVELS.includes(ev.level) ? ev.level : 'info';
  const text = String(ev.text == null ? '' : ev.text);
  const source = String(ev.source || '');
  const el = document.createElement('div');
  el.className = 'row ' + level;
  el.style.setProperty('--c', TAG_COLOURS[tag] || '#c9d1d9');
  el.append(span('t', formatTime(ev.ts)), span('tag', tag), span('src', source), span('txt', text));
  el.firstChild.title = String(ev.ts);
  const row = { ev: { ...ev, tag, level, text, source }, el, hay: `${tag} ${source} ${text}`, tag, level, hidden: false };
  el.addEventListener('click', () => toggleDetail(row));
  return row;
}
function atBottom() { return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60; }
function scrollToEnd() { logEl.scrollTop = logEl.scrollHeight; latestEl.hidden = true; }

/** Put a row where its timestamp belongs: a late CloudWatch line lands a little above the newest ones. */
function place(row, frag) {
  const last = rows[rows.length - 1];
  if (!last || last.ev.ts <= row.ev.ts) { rows.push(row); frag.append(row.el); return; }
  let at = rows.length - 1;
  for (let steps = 0; at >= 0 && rows[at].ev.ts > row.ev.ts && steps < 500; steps++) at--;
  if (at >= 0 && rows[at].ev.ts > row.ev.ts) { rows.push(row); frag.append(row.el); return; }
  rows.splice(at + 1, 0, row);
  if (at < 0) { logEl.prepend(row.el); return; }
  const after = rows[at].el.nextElementSibling;
  (after && after.classList.contains('detail') ? after : rows[at].el).after(row.el);
}

function addEvents(list) {
  if (!list.length) return;
  const stick = atBottom();
  const frag = document.createDocumentFragment();
  for (const ev of list) {
    const row = makeRow(ev);
    ensureTag(row.tag);
    tagCounts[row.tag]++;
    levelCounts[row.level]++;
    row.hidden = !visible(row);
    row.el.hidden = row.hidden;
    place(row, frag);
  }
  logEl.append(frag);
  const over = rows.length - MAX_LINES;
  if (over > 0) {
    for (const row of rows.splice(0, over)) {
      seen.delete(row.ev.id);
      tagCounts[row.tag]--;
      levelCounts[row.level]--;
      const next = row.el.nextElementSibling;
      if (next && next.classList.contains('detail')) next.remove();
      row.el.remove();
    }
  }
  updateCounts();
  if (stick) scrollToEnd(); else latestEl.hidden = false;
}
function flush() {
  flushTimer = null;
  const list = incoming;
  incoming = [];
  if (state.paused) {
    held = held.concat(list);
    for (const ev of held.splice(0, Math.max(0, held.length - MAX_LINES))) seen.delete(ev.id);
    updateCounts();
    return;
  }
  addEvents(list);
}

/** Take events from either transport: dedupe by id, remember the newest timestamp, render in a batch. */
function ingest(list) {
  if (!Array.isArray(list)) return;
  for (const ev of list) {
    if (!ev || typeof ev !== 'object' || typeof ev.id !== 'string' || !Number.isFinite(ev.ts)) continue;
    if (lastTs === null || ev.ts > lastTs) lastTs = ev.ts;
    if (seen.has(ev.id)) continue;
    seen.add(ev.id);
    incoming.push(ev);
  }
  if (incoming.length && flushTimer === null) flushTimer = setTimeout(flush, 60);
}

// ---- game status bar
function setGame(status) {
  if (!status || typeof status !== 'object') return;
  const part = (id, label, value) => {
    const el = $(id);
    el.textContent = `${label} ${value === null || value === undefined ? '–' : value}`;
    el.classList.toggle('on', value !== null && value !== undefined);
  };
  part('g-run', 'run', status.run8);
  part('g-phase', 'phase', status.phase);
  part('g-round', 'round', status.round);
  part('g-players', 'players', status.players);
  part('g-picked', 'picked', status.picked);
}

// ---- transport: EventSource, or polling when the platform buffers the stream
const transport = { kind: null, source: null, helloTimer: null, pollTimer: null, polling: false, backoff: 0, debug: false, gen: 0 };
const wantDebug = () => state.levels.has('debug');

function setHealth(next) { health = next; setStatus(); }
function setStatus() {
  const down = health === 'down';
  statusEl.className = 'status' + (down ? ' down' : state.paused ? ' paused' : '');
  statusEl.textContent = down ? 'reconnecting…' : health === 'connecting' ? 'connecting…' : state.paused ? 'paused' : 'live';
  modeEl.textContent = transport.kind === 'stream' ? 'live stream' : transport.kind === 'polling' ? 'polling' : '';
}
function signedOut() { location.assign('/'); }
/** True when the presenter session still works; a 403 sends the page back to the sign-in form. */
async function checkAuth() {
  try {
    const response = await fetch('/api/status', { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 403) { signedOut(); return false; }
    if (response.ok) setGame((await response.json()).status);
    return response.ok;
  } catch (e) { return false; }
}

function stopTransport() {
  transport.gen++;
  clearTimeout(transport.helloTimer);
  clearTimeout(transport.pollTimer);
  if (transport.source) transport.source.close();
  transport.source = null;
  transport.polling = false;
}
function startStream() {
  stopTransport();
  const gen = transport.gen;
  transport.debug = wantDebug();
  transport.kind = 'stream';
  const source = new EventSource('/stream?debug=' + (transport.debug ? 1 : 0));
  transport.source = source;
  let hello = false;
  transport.helloTimer = setTimeout(() => {
    // Nothing in 5 s: something between here and the server holds the response back.
    if (!hello && gen === transport.gen) startPolling();
  }, HELLO_WAIT_MS);
  source.addEventListener('hello', () => {
    hello = true;
    clearTimeout(transport.helloTimer);
    setHealth('up');
  });
  source.addEventListener('status', (m) => { try { setGame(JSON.parse(m.data)); } catch (e) { /* ignore */ } });
  source.onmessage = (m) => {
    try { ingest([JSON.parse(m.data)]); } catch (e) { /* ignore */ }
  };
  source.onerror = () => {
    if (gen !== transport.gen) return;
    setHealth('down');
    // A refused stream (signed out, throttled) is final: EventSource does not retry it.
    if (source.readyState === EventSource.CLOSED) {
      checkAuth().then((ok) => { if (ok && gen === transport.gen) startPolling(); });
    }
  };
  setStatus();
}
function startPolling() {
  stopTransport();
  transport.kind = 'polling';
  transport.backoff = 0;
  transport.debug = wantDebug();
  setStatus();
  pollOnce(transport.gen);
}
async function pollOnce(gen) {
  if (gen !== transport.gen || transport.polling) return;
  transport.polling = true;
  let wait = POLL_MS;
  try {
    const query = `debug=${wantDebug() ? 1 : 0}` + (lastTs === null ? '' : `&after=${lastTs}`);
    const response = await fetch(`/events?${query}`, { credentials: 'same-origin', cache: 'no-store' });
    if (gen !== transport.gen) return;
    if (response.status === 403) { signedOut(); return; }
    if (response.status === 429) {
      // Never straight away: honour Retry-After, otherwise back off 10 s, doubling up to a minute.
      const retry = Number(response.headers.get('Retry-After'));
      transport.backoff = Math.min(60000, Math.max(Number.isFinite(retry) ? retry * 1000 : 0, transport.backoff * 2, 10000));
      wait = transport.backoff;
      setHealth('down');
    } else if (response.ok) {
      const reply = await response.json();
      transport.backoff = 0;
      ingest(reply.events);
      setGame(reply.status);
      setHealth('up');
    } else {
      transport.backoff = Math.min(30000, Math.max(transport.backoff * 2, 4000));
      wait = transport.backoff;
      setHealth('down');
    }
  } catch (e) {
    if (gen !== transport.gen) return;
    transport.backoff = Math.min(30000, Math.max(transport.backoff * 2, 4000));
    wait = transport.backoff;
    setHealth('down');
  } finally {
    if (gen === transport.gen) {
      transport.polling = false;
      transport.pollTimer = setTimeout(() => pollOnce(gen), wait);
    }
  }
}
/** Debug events are only sent when asked for: a new connection (or a cursor-less poll) fetches the backlog again. */
function syncDebug() {
  if (transport.kind === null || transport.debug === wantDebug()) return;
  transport.debug = wantDebug();
  if (transport.kind === 'stream') { startStream(); return; }
  lastTs = null;
  clearTimeout(transport.pollTimer);
  transport.polling = false;
  transport.gen++;
  pollOnce(transport.gen);
}

// ---- controls
function togglePause() {
  state.paused = !state.paused;
  pauseEl.textContent = state.paused ? 'Resume' : 'Pause';
  if (!state.paused) {
    const list = held;
    held = [];
    addEvents(list);
  }
  setStatus();
  updateCounts();
}
async function signOut() {
  signoutEl.disabled = true;
  try {
    await fetch('/api/logout', { method: 'POST', credentials: 'same-origin', headers: { 'X-Rehearsal-Action': '1' } });
  } finally {
    signedOut();
  }
}

function init() {
  for (const lv of LEVELS) {
    levelCounts[lv] = 0;
    levelChips[lv] = makeChip(levelsEl, lv, LEVEL_COLOURS[lv], () => {
      if (state.levels.has(lv)) state.levels.delete(lv); else state.levels.add(lv);
      syncChips();
      applyFilters();
    });
  }
  KNOWN_TAGS.forEach(ensureTag);
  loadHash();
  buildMatcher();
  syncChips();
  qEl.addEventListener('input', () => { state.q = qEl.value; applyFilters(); });
  reEl.addEventListener('change', () => { state.regex = reEl.checked; applyFilters(); });
  pauseEl.addEventListener('click', togglePause);
  signoutEl.addEventListener('click', signOut);
  latestEl.addEventListener('click', scrollToEnd);
  logEl.addEventListener('scroll', () => { if (atBottom()) latestEl.hidden = true; });
  window.addEventListener('hashchange', () => { loadHash(); syncChips(); applyFilters(); });
  document.addEventListener('keydown', (e) => {
    if (e.target === qEl || e.target.tagName === 'BUTTON' || e.target.tagName === 'INPUT') return;
    if (e.key === ' ') { e.preventDefault(); togglePause(); }
    if (e.key === '/') { e.preventDefault(); qEl.focus(); }
  });
  setStatus();
  updateCounts();
  startStream();
  checkAuth();
}
init();
