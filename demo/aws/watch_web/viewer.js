'use strict';
// Live log viewer: every filter runs here, in the browser. The server only streams the JSONL.
const MAX_LINES = 10000;
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

const state = {
  offTags: new Set(),                       // tags switched off; new tags start on
  levels: new Set(['info', 'warn', 'error']), // debug hidden by default
  q: '', regex: false, paused: false,
};
const rows = [];            // {ev, el, hay, tag, level, hidden}
const tagCounts = {}, levelCounts = {};
const tagChips = {}, levelChips = {};
let incoming = [];          // received, not yet rendered
let held = [];              // received while paused
let matcher = null;         // null = match all
let flushTimer = null;
let connectedBefore = false;

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
    + `tag ${ev.tag} · level ${ev.level} · source ${ev.source}\n\n${ev.text}`;
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
    rows.push(row);
    frag.append(row.el);
  }
  logEl.append(frag);
  const over = rows.length - MAX_LINES;
  if (over > 0) {
    for (const row of rows.splice(0, over)) {
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
    held = held.concat(list).slice(-MAX_LINES);
    updateCounts();
    return;
  }
  addEvents(list);
}

// ---- stream
function connect() {
  const source = new EventSource('/events');
  source.onopen = () => {
    if (connectedBefore) clearAll();   // the server resends its backlog after a reconnect
    connectedBefore = true;
    setStatus();
  };
  source.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (!ev || typeof ev !== 'object') return;
    incoming.push(ev);
    if (flushTimer === null) flushTimer = setTimeout(flush, 60);
  };
  source.onerror = () => setStatus('down');
}
function clearAll() {
  rows.length = 0; incoming = []; held = [];
  for (const k of Object.keys(tagCounts)) tagCounts[k] = 0;
  for (const k of LEVELS) levelCounts[k] = 0;
  logEl.textContent = '';
  updateCounts();
}
function setStatus(kind) {
  statusEl.className = 'status' + (kind === 'down' ? ' down' : state.paused ? ' paused' : '');
  statusEl.textContent = kind === 'down' ? 'reconnecting…' : state.paused ? 'paused' : 'live';
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
  connect();
}
init();
