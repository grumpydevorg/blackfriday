'use strict';
const $ = id => document.getElementById(id);
const host = document.body.dataset.host === 'true';
const money = cents => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(cents / 100);
const titles = { ready: 'The Growth round is open', launching: '1 · Launch is running', launch: 'Launch saved · pick your path', migrating: '2 · Every team is deploying', migrated: 'Every deployment is healthy', comparing: '3 · The shoppers are returning', compared: 'The scores are in', expired: 'The round expired', failed: 'The round could not finish' };
const messages = { ready: 'Join now and pick one option. Rick starts the shop.', launching: 'A real MicroVM sells a mug, saves Maya’s cart, then terminates.', launch: 'The checkpoint is saved. Joining and picking stay open until Close.', migrating: 'Picks are frozen and each team’s risk is rolled once. Every team migrates the same checkpoint in its own MicroVM.', migrated: 'The databases are healthy and the old order exists. Which migrations held on Black Friday?', comparing: 'Fresh MicroVMs check the carts. Event judges replay the Black Friday wave on each team’s shop.', compared: 'Profit wins, not most servers. Real migration checks; modelled capacity, costs and next-week sales.' };
const optionOf = id => (current?.options ?? []).find(option => option.id === id);
const optionLabel = id => optionOf(id)?.label ?? id;
const whole = cents => `$${cents / 100}`;
const optionLine = option => `${option.blurb}${option.extra_cost_cents ? `, +${whole(option.extra_cost_cents)}` : ''} · breaks ${option.risk}%`;
let identity;
try { identity = JSON.parse(localStorage.getItem('black-friday-team')); } catch { identity = null; }
let current, initializedPick = false, pending = false;
let timer, inflight = false, again = false, epoch = 0;
const make = (tag, text, cls = '') => { const node = document.createElement(tag); node.textContent = text; node.className = cls; return node; };
async function api(path, input, action = false) {
  const headers = {};
  if (identity) { headers['X-Team-Id'] = identity.id; headers['X-Team-Token'] = identity.token; }
  const options = { headers, cache: 'no-store' };
  const post = input !== undefined || action;
  if (post) {
    options.method = 'POST';
    if (action) { headers['X-Rehearsal-Action'] = '1'; headers['X-Rehearsal-Run'] = current.action_token; }
    if (input !== undefined) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(input); }
  }
  let response;
  // The endpoint answers 429 (an HTML page) while it adds environments. Retry a join or a pick,
  // never a poll: the next tick is the retry, and a room of phones must not amplify a throttle.
  const retries = post && !action ? 4 : 1;
  for (let attempt = 1; ; attempt++) {
    response = await fetch(`/api/${path}`, options);
    if (response.status !== 429 || attempt === retries) break;
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
// One line per team so ten of them fit a projector screen; the working is in the tooltip and on the viewer's own result.
function score(row) {
  const line = make('article', '', `row ${row.broke ? 'broke' : 'held'}${row.id === identity?.id ? ' own' : ''}`);
  const name = make('span', row.name, 'name');
  if (row.bot) name.append(' ', make('span', 'bot', 'pill'));
  else if (row.house) name.append(' ', make('span', 'house', 'pill'));
  const extra = row.architecture.extra_cost_cents ? ` · +${whole(row.architecture.extra_cost_cents)}` : '';
  line.append(
    make('span', `#${row.rank}`, 'rank'), name,
    make('span', `${optionLabel(row.option)} · ${row.risk}%${extra}`, 'pick'),
    make('span', row.broke ? `Broke on Black Friday (rolled ${row.roll}, under ${row.risk})` : `Held (rolled ${row.roll})`,
      row.broke ? 'outcome red' : 'outcome green'),
    make('span', money(row.cumulative_cents), 'money'),
  );
  line.title = `${row.broke ? 'The wave was lost: 0 verified sales' : `The wave: ${row.architecture.verified_sales} verified sales`}, shop cost ${money(row.architecture.cost_cents)} · Launch $9 + shop ${money(row.architecture.profit_cents)} + Maya ${money(row.maya_sales * 1000)} − migration ${money(row.migration_cost_cents)}`;
  return line;
}
let optionsKey = '';
// Three big buttons; the saved one is highlighted. Rebuilt only when something changed, so a tap is never lost to a poll.
function renderOptions(state, team) {
  const chosen = team.picked ? team.option : '';
  const key = JSON.stringify([state.options, chosen]);
  if (key === optionsKey) return;
  optionsKey = key;
  $('options').replaceChildren(...state.options.map(option => {
    const button = make('button', '', chosen === option.id ? 'option selected' : 'option');
    button.type = 'button';
    button.dataset.option = option.id;
    button.setAttribute('aria-pressed', String(chosen === option.id));
    button.append(make('span', option.label, 'option-label'), make('span', optionLine(option), 'option-line'));
    return button;
  }));
}
function render(state) {
  current = state;
  if (!host && identity && identity.run_id !== state.id) {
    identity = null; initializedPick = false; localStorage.removeItem('black-friday-team');
  } else if (!host && identity && !state.me) {
    identity = null; initializedPick = false; localStorage.removeItem('black-friday-team');
    $('error').textContent = 'Your team was removed. You can join again.';
  }
  $('headline').textContent = titles[state.phase] || state.phase;
  $('progress').textContent = state.message || messages[state.phase] || '';
  const step = ['ready', 'launching'].includes(state.phase) ? 'launch' : ['launch', 'migrating'].includes(state.phase) ? 'migrate' : 'compare';
  for (const name of ['launch', 'migrate', 'compare']) $(`stage-${name}`).classList.toggle('active', name === step);
  $('capacity').textContent = `${state.audience_count} team${state.audience_count === 1 ? '' : 's'} joined · ${state.open ? 'joining and picks open' : 'round closed'}`;
  $('teams').replaceChildren(...state.teams.map(team => {
    const row = make('li', `${team.name}${team.house ? ' · house' : ''}${team.picked ? ' ✓' : ''}${!state.open && team.option ? ` · ${optionLabel(team.option)}` : ''}`, 'team');
    if (host && state.open && !team.house) {
      const button = make('button', 'Remove', 'secondary small');
      button.type = 'button';
      button.addEventListener('click', () => removeTeam(team));
      row.append(button);
    }
    return row;
  }));
  const finished = state.phase === 'compared';
  $('scoreboard').hidden = !finished;
  if (finished) {
    $('scores').replaceChildren(...state.board.rows.map(score));
    $('more').textContent = state.board.more > 0 ? `+${state.board.more} more team${state.board.more === 1 ? '' : 's'}` : '';
  }
  if (host) {
    for (const [name, phase] of [['launch', 'ready'], ['migrate', 'launch'], ['compare', 'migrated']]) {
      $(name).disabled = pending || (state.phase !== phase && state.retry_action !== name);
    }
    $('reset').hidden = !['ready', 'compared', 'expired', 'failed'].includes(state.phase);
    $('execution').textContent = state.execution_arn || 'Not started';
    $('checkpoint').hidden = !state.checkpoint; $('checkpoint').textContent = state.checkpoint ? `Launch checkpoint ${state.checkpoint} · VM ${state.launch_vm_id} · ${state.launch_vm_state}` : '';
    $('public-url').textContent = state.public_url ?? 'PUBLIC_HOST is not configured'; $('public-url').href = state.public_url ?? '#';
    $('logout').hidden = state.auth_mode === 'key'; // the helper adds its own key; there is no cookie to clear
    return;
  }
  const team = state.me;
  $('join-section').hidden = Boolean(team) || !state.open;
  $('pick-section').hidden = !team || !state.open;
  $('waiting').hidden = finished || (!team && state.open);
  $('waiting-title').textContent = state.open ? 'Waiting for the round to close' : 'Watching the judges';
  $('waiting-detail').textContent = team ? `${team.name}: ${team.picked ? `you picked ${optionLabel(team.option)}` : 'using Lift & shift until you pick'}. ${state.open ? 'You can change it above until Close.' : 'Your pick is frozen.'}` : 'The round is closed. Watch the results with everyone else.';
  if (team) {
    $('your-team').textContent = team.name;
    renderOptions(state, team);
  }
  const own = finished && state.board.rows.find(row => row.id === identity?.id);
  $('own-result').hidden = !own;
  if (own) $('own-result').replaceChildren(make('h2', `Your result · #${own.rank}`), make('p', `${own.name} · ${own.broke ? `broke on Black Friday (rolled ${own.roll}, under ${own.risk})` : `held (rolled ${own.roll})`} · ${money(own.cumulative_cents)} profit`), make('p', `${own.broke ? 'The wave was lost: 0 verified sales' : `The wave: ${own.architecture.verified_sales} verified sales`}, shop cost ${money(own.architecture.cost_cents)}. Launch $9 + shop ${money(own.architecture.profit_cents)} + Maya ${money(own.maya_sales * 1000)} − migration ${money(own.migration_cost_cents)}.`, 'hint'));
}
function signedOut() {
  $('panel').hidden = true;
  $('signin').hidden = false;
}
const stopped = () => ['compared', 'expired', 'failed'].includes(current?.phase);
// One poll at a time. A result that began before an action finished is discarded and fetched again.
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
      if (host) {
        $('signin').hidden = true;
        $('panel').hidden = false;
        if (!$('qr').getAttribute('src') && state.public_url) $('qr').src = '/api/host/qr.svg';
      }
    }
  } catch (error) {
    if (host && error.status === 403) { signedOut(); keepPolling = false; }
    else if (error.status !== 429) $('error').textContent = error.message;
  } finally { inflight = false; }
  if (again) { again = false; return refresh(); }
  // After a round ends, keep a slow check (presenter 15 s, players 30 s) so a Reset
  // from another device shows up, and check at once when the tab comes back.
  if (keepPolling) timer = setTimeout(refresh, !stopped() ? 2000 : host ? 15000 : 30000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
async function act(work, errorBox = 'error') {
  if (pending) return;
  pending = true; $(errorBox).textContent = '';
  try { await work(); } catch (error) { $(errorBox).textContent = error.message; }
  finally { pending = false; epoch += 1; again = false; await refresh(); }
}
async function removeTeam(team) {
  await act(() => api('host/remove', { id: team.id }, true));
}
if (host) {
  $('login').addEventListener('submit', async event => {
    event.preventDefault();
    await act(async () => { await api('host/login', { passphrase: $('passphrase').value }); $('passphrase').value = ''; }, 'login-error');
  });
  $('logout').addEventListener('click', async () => {
    try { await api('host/logout', {}, true); } catch (error) { $('error').textContent = error.message; }
    $('qr').removeAttribute('src');
    clearTimeout(timer);
    signedOut();
  });
  for (const action of ['launch', 'migrate', 'compare', 'reset']) $(action).addEventListener('click', async () => {
    $(action).disabled = true;
    await act(async () => { render(await api(action, undefined, true)); });
  });
} else {
  $('join').addEventListener('submit', async event => {
    event.preventDefault();
    await act(async () => {
      identity = await api('join', { name: $('team-name').value });
      localStorage.setItem('black-friday-team', JSON.stringify(identity));
      initializedPick = false;
    });
  });
  $('options').addEventListener('click', async event => {
    const button = event.target.closest('button[data-option]');
    if (!button || !identity) return;
    await act(async () => {
      await api('pick', { id: identity.id, token: identity.token, option: button.dataset.option });
      $('saved').textContent = `Saved: ${optionLabel(button.dataset.option)}. Waiting for the round to close.`;
    });
  });
}
refresh();
