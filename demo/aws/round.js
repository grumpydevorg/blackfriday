'use strict';
// One script for both pages: the phone (player.html) and the presenter (host.html, <body data-host>).
// No inline script or style anywhere (CSP), so every element is built here with textContent.
const $ = id => document.getElementById(id);
const host = document.body.dataset.host === 'true';
const STORAGE_KEY = 'black-friday-player';
const NAME_LIMIT = 20;
const money = cents => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 2 }).format(cents / 100);
const make = (tag, text = '', cls = '') => { const node = document.createElement(tag); node.textContent = text; node.className = cls; return node; };
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

let identity = null;
if (!host) { try { identity = JSON.parse(localStorage.getItem(STORAGE_KEY)); } catch { identity = null; } }
let current, pending = false;
let timer, inflight = false, again = false, epoch = 0, pollFailed = false, renameDirty = false;

const roundOf = (state, id) => (state?.rounds ?? []).find(round => round.id === id);
const optionOf = (state, roundId, optionId) => roundOf(state, roundId)?.options.find(option => option.id === optionId);
const optionLabel = (state, roundId, optionId) => optionOf(state, roundId, optionId)?.label ?? optionId;
const unit = (market, n) => `${n}${market.unit}`;
const marketRange = round => `${round.market.text}: somewhere between ${unit(round.market, round.market.min)} and ${unit(round.market, round.market.max)}`;
const marketDraw = (round, n) => `${round.market.text}: ${unit(round.market, n)}`;
const phaseTitles = {
  ready: 'Waiting for the game to start', starting: 'Setting up the shop', closing: 'Round closed',
  final: 'Final results', expired: 'The game expired', failed: 'The game could not finish',
};
const phoneMessages = {
  ready: 'Join now. Rick starts the game when the room is ready.',
  starting: 'Real MicroVMs are launching the shop and trying both migrations. Round 1 opens in a moment.',
  open: 'Pick one before Rick closes the round. Tap another to change it.',
  closing: 'Rolling the dice and scoring every shop…',
  final: 'That is all four rounds.',
  expired: 'Rick did not close the round in time. Ask him to reset.',
  failed: 'Something broke on the server side. Ask Rick to reset.',
};
const hostMessages = {
  ready: 'Players can join. Press Start when the room is ready.',
  starting: 'Real launch and migrations are running. Round 1 opens when they finish.',
  open: 'Players are picking. Close the round when the room has chosen.',
  closing: 'Scoring the round…',
  final: 'Four rounds done. Reset for a fresh game.',
  expired: 'The close callback expired. Reset to start again.',
  failed: 'The workflow failed. Reset to start again.',
};
const optionStates = (state, round, picks) => (round?.options ?? []).map(option => {
  const need = option.requires;
  const unmet = need && !need.options.includes(picks[need.round]);
  return { option, reason: unmet ? need.reason : '' };
});

async function api(path, { body, action = false, headers: extra = {}, retry = false } = {}) {
  const headers = { ...extra };
  if (identity) { headers['X-Team-Id'] = identity.id; headers['X-Team-Token'] = identity.token; }
  const options = { headers, cache: 'no-store' };
  if (body !== undefined || action) {
    options.method = 'POST';
    if (action) { headers['X-Rehearsal-Action'] = '1'; headers['X-Rehearsal-Run'] = current?.action_token ?? ''; }
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  }
  // The endpoint answers 429 (an HTML page) while it adds environments. Retry a join, rename or pick,
  // never a poll: the next tick is the retry, and a room of phones must not amplify a throttle.
  const attempts = retry ? 4 : 1;
  let response;
  for (let attempt = 1; ; attempt++) {
    response = await fetch(`/api/${path}`, options);
    if (response.status !== 429 || attempt === attempts) break;
    await new Promise(resolve => setTimeout(resolve, 300 * attempt + Math.random() * 300));
  }
  const data = (response.headers.get('content-type') ?? '').includes('json') ? await response.json() : {};
  if (!response.ok) {
    const failure = new Error(data.error || (response.status === 429 ? 'Busy, try again' : `HTTP ${response.status}`));
    failure.status = response.status;
    throw failure;
  }
  return data;
}

function badge(source) {
  const text = source === 'real' ? 'real' : source === 'recorded' ? 'recorded' : 'modelled';
  const pill = make('span', text, `pill ${text}`);
  pill.title = { real: 'Measured in a real MicroVM tonight', recorded: 'A real MicroVM step failed, so a recorded result was used', modelled: 'Simulated by the model, not measured' }[text];
  return pill;
}

// One ranked line. The working is in the tooltip; the outcome line says what happened and what the dice said.
function boardRow(state, results, row, own) {
  const line = make('article', '', `row ${row.hit ? 'hit' : 'ok'}${own ? ' own' : ''}`);
  const name = make('span', row.name, 'name');
  if (row.bot) name.append(' ', make('span', 'bot', 'pill'));
  else if (row.house) name.append(' ', make('span', 'house', 'pill'));
  const pick = make('span', optionLabel(state, results.round, row.option), 'pick');
  pick.append(' ', badge(row.evidence));
  const dice = row.risk > 0 ? ` (rolled ${row.roll}, risk ${row.risk})` : '';
  line.append(
    make('span', `#${row.rank}`, 'rank'), name, pick,
    make('span', `${row.event}${dice}`, row.hit ? 'outcome red' : 'outcome green'),
    make('span', money(row.cumulative_cents), 'money'),
  );
  line.title = `Sales ${row.sales}, slow ${row.slow}, failed ${row.failed} · cost ${money(row.cost_cents)}${row.refund_cents ? `, refunds ${money(row.refund_cents)}` : ''} · profit ${money(row.profit_cents)} · total ${money(row.cumulative_cents)}`;
  return line;
}
function renderBoard(state, ownId) {
  const results = state.results;
  $('scoreboard').hidden = !results?.board;
  if (!results?.board) return;
  const { rows, total, more } = results.board;
  const nodes = [];
  rows.forEach((row, index) => {
    if (index === 10) nodes.push(make('p', '⋯', 'gap'));
    nodes.push(boardRow(state, results, row, row.id === ownId));
  });
  $('scores').replaceChildren(...nodes);
  $('board-title').textContent = `After ${roundOf(state, results.round)?.title ?? 'the last round'}`;
  if (!host) $('board-sub').textContent = `Top 10, and you. ${plural(total, 'player')}. Ties share a rank.`;
  $('more').textContent = more > 0 ? `+${more} more ${more === 1 ? 'player' : 'players'}` : '';
}

let roundKey = '';
// The option list. Buttons on the phone (three big targets), a static summary on the presenter.
function renderPhoneOptions(state, round, me, interactive) {
  const picks = me?.picks ?? {};
  const rows = optionStates(state, round, picks);
  const key = JSON.stringify([round.id, interactive, picks[round.id], rows.map(item => item.reason)]);
  if (key === roundKey) return;
  roundKey = key;
  $('options').replaceChildren(...rows.map(({ option, reason }) => {
    const chosen = picks[round.id] === option.id;
    const button = make('button', '', `option${chosen ? ' selected' : ''}${reason ? ' unmet' : ''}${interactive ? '' : ' locked'}`);
    button.type = 'button';
    button.dataset.option = option.id;
    button.disabled = Boolean(reason) || !interactive;
    button.setAttribute('aria-pressed', String(chosen));
    const head = make('span', '', 'option-head');
    head.append(make('span', option.label, 'option-label'), make('span', option.cost_text, 'pill cost'));
    if (option.evidence === 'real') head.append(badge('real'));
    button.append(head, make('span', option.blurb, 'option-blurb'));
    if (option.dice_text) button.append(make('span', `Dice: ${option.dice_text}`, 'option-dice'));
    if (reason) button.append(make('span', reason, 'option-reason'));
    return button;
  }));
}
function renderHostOptions(round) {
  $('round-options').replaceChildren(...round.options.map(option => {
    const card = make('article', '', 'host-option');
    const head = make('p', '', 'option-head');
    head.append(make('span', option.label, 'option-label'), make('span', option.cost_text, 'pill cost'));
    if (option.evidence === 'real') head.append(badge('real'));
    card.append(head, make('p', option.blurb, 'option-blurb'));
    if (option.dice_text) card.append(make('p', `Dice: ${option.dice_text}`, 'option-dice'));
    if (option.requires) card.append(make('p', option.requires.reason, 'option-reason'));
    return card;
  }));
}

function renderPhone(state) {
  // A new run or a removed player ends the old identity; a Reset starts every phone from zero.
  if (identity && identity.run_id !== state.id) { identity = null; localStorage.removeItem(STORAGE_KEY); }
  else if (identity && !state.me) {
    identity = null; localStorage.removeItem(STORAGE_KEY);
    $('error').textContent = 'You were removed from the game. You can join again if joining is open.';
  }
  const me = state.me;
  const open = state.phase === 'open' && Boolean(roundOf(state, state.round));
  const round = open || state.phase === 'closing' ? roundOf(state, state.round) : ['ready', 'starting'].includes(state.phase) ? state.rounds[0] : null;
  const canPick = open && (Boolean(me) || state.joinable);
  $('headline').textContent = open ? `Round ${state.round_number} is open` : state.phase === 'closing' && round ? `Round ${state.round_number} closed` : phaseTitles[state.phase] ?? state.phase;
  const spectator = !me && !state.joinable && ['open', 'closing'].includes(state.phase);
  $('progress').textContent = state.message || (spectator ? 'Joining has closed. Watch the board with everyone else.' : phoneMessages[state.phase] ?? '');

  $('join-section').hidden = Boolean(me) || !state.joinable;
  $('me-section').hidden = !me;
  if (me) {
    $('your-name').textContent = me.name;
    $('rename').hidden = !state.renamable;
    $('rename-note').hidden = !state.renamable;
    if (!renameDirty) $('rename-name').value = me.name;
  }

  $('round-section').hidden = !round;
  if (round) {
    $('round-weeks').textContent = `${round.weeks} · ${state.round_number > 0 ? `Round ${state.round_number} of ${state.rounds.length}` : 'Coming up'}`;
    $('round-title').textContent = round.title;
    $('round-forecast').textContent = round.forecast;
    $('round-market').textContent = marketRange(round);
    renderPhoneOptions(state, round, me, canPick);
    const pick = me?.picks?.[round.id];
    $('saved').textContent = !canPick ? '' : pick
      ? `Saved: ${optionLabel(state, round.id, pick)}. Tap another to change it until Rick closes the round.`
      : `${me ? 'No pick yet.' : 'Tap an option to join and pick in one go.'} If you skip, you get “${round.options[0].label}”.`;
    $('round-hint').textContent = open ? 'Prices and sales are modelled, not real AWS charges.' : state.phase === 'closing' ? 'Your pick is frozen.' : 'Round 1 opens when Rick starts the game.';
  } else roundKey = '';

  const mine = state.results?.mine;
  const results = state.results;
  $('result-section').hidden = !mine;
  if (mine) {
    const resultRound = roundOf(state, results.round);
    const stat = (label, value, cls = '') => { const tile = make('div', '', `stat ${cls}`); tile.append(make('span', label, 'k'), make('span', value, 'v')); return tile; };
    const stats = make('div', '', 'stats');
    stats.append(
      stat('Sales', String(mine.sales)), stat('Slow', String(mine.slow)), stat('Failed', String(mine.failed)),
      stat('Cost', money(mine.cost_cents)), stat('Profit', money(mine.profit_cents), mine.profit_cents < 0 ? 'neg' : 'pos'), stat('Total', money(mine.cumulative_cents), 'total'),
    );
    const head = make('p', '', 'option-head');
    head.append(make('span', optionLabel(state, results.round, mine.option), 'option-label'), badge(mine.evidence));
    const nodes = [make('p', `${resultRound?.weeks ?? ''} · result`, 'kicker'), make('h2', `${resultRound?.title ?? 'Last round'}: #${mine.rank} of ${results.board.total}`), head, make('p', mine.event, `event ${mine.hit ? 'red' : 'green'}`)];
    if (mine.risk > 0) nodes.push(make('p', `Dice: rolled ${mine.roll} against a risk of ${mine.risk}. ${mine.hit ? 'It hit.' : 'You were safe.'}`, 'dice'));
    if (resultRound) nodes.push(make('p', marketDraw(resultRound, results.market), 'market'));
    nodes.push(stats);
    if (mine.refund_cents) nodes.push(make('p', `Includes ${money(mine.refund_cents)} in refunds.`, 'hint'));
    $('result-section').replaceChildren(...nodes);
  }

  const final = state.phase === 'final';
  $('final-section').hidden = !final;
  if (final) {
    $('final-section').replaceChildren(
      make('p', 'Final standing', 'kicker'),
      make('h2', mine ? `You finished #${mine.rank} of ${results.board.total}` : me ? 'You joined too late to score' : 'The game is over'),
      make('p', mine ? `${me.name} made ${money(mine.cumulative_cents)} in total. Most profit wins.` : 'Thanks for playing.', 'big'),
    );
  }
  renderBoard(state, me?.id);
}

function renderStages(state) {
  const nodes = state.rounds.map((round, index) => {
    const number = index + 1;
    const done = state.phase === 'final' || number < state.round_number;
    const active = number === state.round_number && state.phase !== 'final';
    return make('li', `${number} · ${round.title.replace(/^Round \d+ · /, '')}`, `${active ? 'active' : ''}${done ? ' done' : ''}`.trim());
  });
  $('stages').replaceChildren(...nodes);
}
function renderEvidence(state) {
  const evidence = state.evidence;
  $('evidence-section').hidden = !evidence;
  if (!evidence) return;
  const cards = [['launch', 'Launch'], ['careful', 'Careful (preserve)'], ['fast', 'Fast (omit)']].map(([key, title]) => {
    const data = evidence[key] ?? {};
    const card = make('article', '', 'evidence-card');
    const head = make('p', '', 'option-head');
    head.append(make('span', title, 'option-label'), badge(data.source ?? 'real'));
    const list = make('dl');
    for (const [name, value] of Object.entries(data)) {
      if (name === 'source' || value === null || typeof value === 'object') continue;
      list.append(make('dt', name.replaceAll('_', ' ')), make('dd', typeof value === 'boolean' ? (value ? 'yes' : 'no') : String(value)));
    }
    card.append(head, list);
    return card;
  });
  $('evidence').replaceChildren(...cards);
}
function renderHost(state) {
  const phase = state.phase;
  const live = ['open', 'closing'].includes(phase);
  const round = live ? roundOf(state, state.round) : ['ready', 'starting'].includes(phase) ? state.rounds[0] : null;
  renderStages(state);
  $('headline').textContent = live && round ? `${round.title}${phase === 'closing' ? ' · closing' : ''}` : phaseTitles[phase] ?? phase;
  $('progress').textContent = state.message || hostMessages[phase] || '';

  $('round-section').hidden = !round;
  if (round) {
    $('round-weeks').textContent = `${round.weeks}${live ? '' : ' · coming up'}`;
    $('round-title').textContent = round.title;
    $('round-forecast').textContent = round.forecast;
    $('round-market').textContent = marketRange(round);
    renderHostOptions(round);
  }

  const results = state.results;
  const resultRound = roundOf(state, results?.round);
  $('results-section').hidden = !results;
  if (results) {
    $('results-title').textContent = `Results · ${resultRound?.title ?? results.round}`;
    $('market-draw').textContent = resultRound ? marketDraw(resultRound, results.market) : `Market: ${results.market}`;
    const options = results.summary?.options ?? {};
    const head = make('div', '', 'summary-row head');
    head.append(make('span', 'Option'), make('span', 'Players', 'n'), make('span', 'Mean profit', 'n'), make('span', 'Best profit', 'n'));
    const lines = (resultRound?.options ?? []).filter(option => options[option.id]).map(option => {
      const data = options[option.id];
      const line = make('div', '', 'summary-row');
      line.append(make('span', option.label, 'name'), make('span', String(data.players), 'n'), make('span', money(data.mean_profit_cents), 'n money'), make('span', money(data.best_profit_cents), 'n money'));
      return line;
    });
    $('option-summary').replaceChildren(head, ...lines);
  }
  renderBoard(state);
  renderEvidence(state);

  const players = state.players ?? [];
  const audience = players.filter(player => !player.house && !player.bot);
  const picked = audience.filter(player => player.picked).length;
  $('capacity').textContent = `${state.player_count} of ${state.player_cap} players${phase === 'open' ? ` · ${picked} of ${audience.length} picked` : ''}${state.joinable ? ' · joining open' : ' · joining closed'}`;
  $('teams').replaceChildren(...players.map(player => {
    const row = make('li', '', 'team');
    const name = make('span', player.name, 'name');
    if (player.bot) name.append(' ', make('span', 'bot', 'pill'));
    else if (player.house) name.append(' ', make('span', 'house', 'pill'));
    row.append(name);
    if (phase === 'open' && player.picked) row.append(make('span', '✓', 'tick'));
    if (state.joinable && !player.house && !player.bot) {
      const button = make('button', 'Remove', 'secondary small');
      button.type = 'button';
      button.addEventListener('click', () => act(() => api('host/remove', { body: { id: player.id }, action: true })));
      row.append(button);
    }
    return row;
  }));

  const number = state.round_number;
  $('start').disabled = pending || phase !== 'ready';
  $('close').textContent = state.retry_action === 'close' && number > 0 ? `Retry close round ${number}` : live && number > 0 ? `Close round ${number}` : 'Close round';
  $('close').disabled = pending || (phase !== 'open' && state.retry_action !== 'close');
  $('reset').disabled = pending;
  $('execution').textContent = state.execution_arn || 'Not started';
  $('public-url').textContent = state.public_url ?? 'PUBLIC_HOST is not configured';
  $('public-url').href = state.public_url ?? '#';
  $('logout').hidden = state.auth_mode === 'key'; // the helper adds its own key; there is no cookie to clear
}

function render(state) {
  current = state;
  if (host) renderHost(state); else renderPhone(state);
}
function signedOut() {
  $('panel').hidden = true;
  $('signin').hidden = false;
}
const stopped = () => ['final', 'expired', 'failed'].includes(current?.phase);
// One poll at a time. A result that began before an action finished is discarded and fetched again.
// A 429 is not retried: the next tick is the retry.
async function refresh() {
  clearTimeout(timer);
  if (inflight || pending) { again = true; return; }
  inflight = true;
  const started = epoch;
  let keepPolling = true;
  try {
    const state = await api(host ? 'host/state' : 'state');
    if (started !== epoch) again = true;
    else {
      render(state);
      if (pollFailed) { $('error').textContent = ''; pollFailed = false; }
      if (host) {
        $('signin').hidden = true;
        $('panel').hidden = false;
        if (!$('qr').getAttribute('src') && state.public_url) $('qr').src = '/api/host/qr.svg';
      }
    }
  } catch (error) {
    if (host && error.status === 403) { signedOut(); keepPolling = false; }
    else if (error.status !== 429) { $('error').textContent = error.message; pollFailed = true; }
  } finally { inflight = false; }
  if (again) { again = false; return refresh(); }
  // 3 s plus 0–1 s of jitter; after the game ends a slow check (presenter 15 s, phones 30 s) still
  // notices a Reset from another device. A tab coming back refreshes at once.
  if (keepPolling) timer = setTimeout(refresh, (!stopped() ? 3000 : host ? 15000 : 30000) + Math.random() * 1000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
async function act(work, errorBox = 'error') {
  if (pending) return;
  pending = true; $(errorBox).textContent = '';
  if (host && current) renderHost(current);
  try { await work(); } catch (error) { $(errorBox).textContent = error.message; }
  finally { pending = false; epoch += 1; again = false; await refresh(); }
}
// The server counts code points, not UTF-16 units: 20 emoji are a legal name, so maxlength would cut it at 10.
const limitName = event => {
  const letters = [...event.target.value];
  if (letters.length > NAME_LIMIT) event.target.value = letters.slice(0, NAME_LIMIT).join('');
};

// Behind the scenes: a presenter-only drawer that polls the timeline only while it is open.
// It shares api() with the game but keeps its own timer and never touches the game's polling.
const BEHIND_POLL_MS = 2000;
const LOGS_URL = 'https://ap-southeast-2.console.aws.amazon.com/cloudwatch/home?region=ap-southeast-2#logsV2:log-groups/log-group/';
const clock = new Intl.DateTimeFormat('en-AU', { timeZone: 'Australia/Adelaide', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const TAGS = ['GAME', 'DURABLE', 'EVENT', 'MICROVM', 'WEB'];
// The logsV2 fragment encodes the group name twice over: percent-encode, then escape each % as $25.
const logGroupUrl = group => `${LOGS_URL}${encodeURIComponent(group).replaceAll('%', '$25')}`;
const detailValue = value => (value !== null && typeof value === 'object' ? JSON.stringify(value) : String(value));
let behindTimer, behindInflight = false, behindGeneration = 0, behindSignature = '';
const behindOpen = new Set();
let behindLines = [];
// Client-side filter: it only hides rows. Hidden tags and the search text never reach the poll or the open-line set.
const behindHidden = new Set();
let behindQuery = '';
const behindCounts = {};
const lineTag = line => (TAGS.includes(line.tag) ? line.tag : 'EVENT');
const lineHaystack = line => [clock.format(new Date(line.t)), lineTag(line), line.text, ...Object.values(line.detail ?? {}).map(detailValue)].join('\n').toLowerCase();

function behindLine(line) {
  const open = behindOpen.has(line.id);
  const tag = lineTag(line);
  const item = make('div', '', `bline${open ? ' open' : ''}`);
  item.dataset.id = line.id;
  const head = make('div', '', 'bhead');
  head.tabIndex = 0;
  head.setAttribute('role', 'button');
  head.setAttribute('aria-expanded', String(open));
  head.append(make('time', clock.format(new Date(line.t)), 'btime'), make('span', tag, `btag btag-${tag.toLowerCase()}`), make('span', line.text, 'btext'));
  item.append(head);
  if (open) {
    const body = make('div', '', 'bbody');
    const entries = Object.entries(line.detail ?? {});
    if (entries.length) {
      const list = make('dl');
      for (const [name, value] of entries) list.append(make('dt', name.replaceAll('_', ' ')), make('dd', detailValue(value)));
      body.append(list);
    } else body.append(make('p', 'No further detail.', 'muted'));
    if (line.log_group) {
      const link = make('a', 'Open logs in CloudWatch');
      link.href = logGroupUrl(line.log_group);
      link.target = '_blank';
      link.rel = 'noopener';
      body.append(link, make('code', line.log_group, 'bgroup'));
    }
    item.append(body);
  }
  return item;
}
function renderBehind(lines) {
  const box = $('behind-log');
  const signature = JSON.stringify([lines, [...behindOpen], [...behindHidden], behindQuery]);
  if (signature === behindSignature) return;
  behindSignature = signature;
  behindLines = lines;
  const needle = behindQuery.trim().toLowerCase();
  const matching = needle ? lines.filter(line => lineHaystack(line).includes(needle)) : lines;
  const counts = Object.fromEntries(TAGS.map(tag => [tag, 0]));
  for (const line of matching) counts[lineTag(line)] += 1;
  for (const tag of TAGS) if (behindCounts[tag]) behindCounts[tag].textContent = String(counts[tag]);
  const shown = matching.filter(line => !behindHidden.has(lineTag(line)));
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  const top = box.scrollTop;
  box.replaceChildren(...shown.map(behindLine));
  box.scrollTop = atBottom ? box.scrollHeight : top;
  $('behind-status').textContent = !lines.length ? 'Nothing yet. Lines appear as the game runs.'
    : !shown.length ? `No lines match the filter (${plural(lines.length, 'line')} hidden).`
      : shown.length < lines.length ? `Showing ${shown.length} of ${lines.length} lines.` : '';
}
function buildBehindFilter() {
  const group = $('behind-tags');
  for (const tag of TAGS) {
    const chip = make('button', '', `bchip btag-${tag.toLowerCase()}`);
    chip.type = 'button';
    chip.dataset.tag = tag;
    chip.setAttribute('aria-pressed', 'true');
    const count = make('span', '0', 'bcount');
    behindCounts[tag] = count;
    chip.append(make('span', tag), count);
    group.append(chip);
  }
  group.addEventListener('click', event => {
    const chip = event.target.closest('.bchip');
    if (!chip) return;
    const tag = chip.dataset.tag;
    if (!behindHidden.delete(tag)) behindHidden.add(tag);
    chip.setAttribute('aria-pressed', String(!behindHidden.has(tag)));
    renderBehind(behindLines);
  });
  $('behind-search').addEventListener('input', event => { behindQuery = event.target.value; renderBehind(behindLines); });
}
async function behindPoll() {
  clearTimeout(behindTimer);
  if (!$('behind').open || behindInflight) return;
  behindInflight = true;
  const generation = behindGeneration;
  let again = true;
  try {
    const data = await api('host/timeline');
    if (generation === behindGeneration) {
      renderBehind(Array.isArray(data.lines) ? data.lines : []);
    }
  } catch (error) {
    // A 429 just waits for the next tick; a 403 means signed out, so stop until the drawer is reopened.
    if (error.status === 403) again = false;
    else if (error.status !== 429 && generation === behindGeneration) $('behind-status').textContent = error.message;
  } finally { behindInflight = false; }
  if (again && generation === behindGeneration && $('behind').open) behindTimer = setTimeout(behindPoll, BEHIND_POLL_MS);
}
function toggleBehindLine(head) {
  const id = head.parentElement.dataset.id;
  if (!behindOpen.delete(id)) behindOpen.add(id);
  renderBehind(behindLines);
}

if (host) {
  buildBehindFilter();
  // Full-screen QR for the room. Client-side only: the same /api/host/qr.svg, shown as large as the screen allows.
  const overlay = $('qr-overlay');
  const openQr = () => {
    if (!$('qr').getAttribute('src')) return;   // not signed in or no public URL yet
    $('qr-big').src = $('qr').getAttribute('src');
    $('qr-big-url').textContent = ($('public-url').textContent || '').replace(/^https:\/\//, '');
    overlay.hidden = false;
    overlay.requestFullscreen?.().catch(() => {});   // the overlay still covers the page if fullscreen is refused
  };
  const closeQr = () => {
    if (overlay.hidden) return;
    overlay.hidden = true;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  };
  $('qr-full').addEventListener('click', openQr);
  $('qr-open').addEventListener('click', openQr);
  overlay.addEventListener('click', closeQr);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeQr(); });
  // Esc in fullscreen exits fullscreen without a keydown reaching the page: close the overlay with it.
  document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement) closeQr(); });
  $('login').addEventListener('submit', async event => {
    event.preventDefault();
    await act(async () => { await api('host/login', { body: { passphrase: $('passphrase').value } }); $('passphrase').value = ''; }, 'login-error');
  });
  $('logout').addEventListener('click', async () => {
    try { await api('host/logout', { body: {}, action: true }); } catch (error) { $('error').textContent = error.message; }
    $('qr').removeAttribute('src');
    clearTimeout(timer);
    signedOut();
  });
  $('behind').addEventListener('toggle', () => {
    behindGeneration += 1;
    clearTimeout(behindTimer);
    behindInflight = false;
    if ($('behind').open) behindPoll();
  });
  $('behind-log').addEventListener('click', event => {
    const head = event.target.closest('.bhead');
    if (head) toggleBehindLine(head);
  });
  $('behind-log').addEventListener('keydown', event => {
    const head = event.target.closest?.('.bhead');
    if (head && event.target === head && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); toggleBehindLine(head); }
  });
  $('start').addEventListener('click', () => act(() => api('host/start', { action: true })));
  $('close').addEventListener('click', () => act(() => api('host/close', { action: true, headers: { 'X-Rehearsal-Round': current.round } })));
  $('reset').addEventListener('click', () => {
    if (!['ready', 'final'].includes(current?.phase) && !confirm('Reset the game? Every player and score is thrown away.')) return;
    return act(() => api('host/reset', { action: true }));
  });
} else {
  const join = async () => {
    const name = $('team-name').value.trim();
    const joined = await api('join', { body: name ? { name } : {}, retry: true });
    identity = { id: joined.id, token: joined.token, run_id: joined.run_id };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(identity));
  };
  $('team-name').addEventListener('input', limitName);
  $('rename-name').addEventListener('input', event => { limitName(event); renameDirty = true; });
  $('join').addEventListener('submit', async event => {
    event.preventDefault();
    await act(join);
  });
  $('rename').addEventListener('submit', async event => {
    event.preventDefault();
    await act(async () => {
      await api('rename', { body: { id: identity.id, token: identity.token, name: $('rename-name').value }, retry: true });
      renameDirty = false;
    });
  });
  $('options').addEventListener('click', async event => {
    const button = event.target.closest('button[data-option]');
    if (!button || button.disabled || !current?.round) return;
    const round = current.round;
    await act(async () => {
      if (!identity) await join();
      await api('pick', { body: { id: identity.id, token: identity.token, round, option: button.dataset.option }, retry: true });
    });
  });
}
refresh();
