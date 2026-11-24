'use strict';
const $ = id => document.getElementById(id);
const host = document.body.dataset.host === 'true';
const money = cents => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(cents / 100);
const titles = { ready: 'The Growth round is open', launching: '1 · Launch is running', launch: 'Launch saved · choose your release', migrating: '2 · Every team is deploying', migrated: 'Every deployment is healthy', comparing: '3 · The shoppers are returning', compared: 'The scores are in', expired: 'The round expired', failed: 'The round could not finish' };
const messages = { ready: 'Join now and save your pick. Rick starts the shop.', launching: 'A real MicroVM sells a mug, saves Maya’s cart, then terminates.', launch: 'The checkpoint is saved. Joining and picking stay open until Close.', migrating: 'Picks are frozen. Each team migrates the same checkpoint in its own MicroVM.', migrated: 'The databases are healthy and the old order exists. Did Maya’s cart survive?', comparing: 'Fresh MicroVMs check the carts. Event judges score your architecture on the same traffic wave.', compared: 'Profit wins, not most servers. Real migration checks; modelled capacity, costs and next-week sales.' };
let identity;
try { identity = JSON.parse(localStorage.getItem('black-friday-team')); } catch { identity = null; }
let current, initializedPick = false, pending = false;
const make = (tag, text, cls = '') => { const node = document.createElement(tag); node.textContent = text; node.className = cls; return node; };
async function api(path, input, action = false) {
  const headers = {};
  if (identity) { headers['X-Team-Id'] = identity.id; headers['X-Team-Token'] = identity.token; }
  const options = { headers, cache: 'no-store' };
  if (input !== undefined || action) {
    options.method = 'POST';
    if (action) { headers['X-Rehearsal-Action'] = '1'; headers['X-Rehearsal-Run'] = current.action_token; }
    else { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(input); }
  }
  let response;
  // The endpoint answers 429 (an HTML page) while it adds environments; wait and retry.
  for (let attempt = 1; ; attempt++) {
    response = await fetch(`/api/${path}`, options);
    if (response.status !== 429 || attempt === 4) break;
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
function score(row) {
  const card = make('article', '', 'score');
  card.append(make('span', `#${row.rank}`, 'rank'), make('h3', row.name), make('div', money(row.cumulative_cents), 'money'));
  const arch = row.architecture;
  card.append(make('p', `Release ${row.release} · ${arch.storage} · ${arch.replicas} replica${arch.replicas === 1 ? '' : 's'} · cache ${arch.cache ? 'on' : 'off'}`));
  card.append(make('p', row.cart_status === 200 ? 'Maya’s cart kept · checkout verified' : 'Maya’s cart lost · she leaves', row.cart_status === 200 ? 'green' : 'red'));
  card.append(make('small', `Architecture: ${arch.verified_sales} verified sales · cost ${money(arch.cost_cents)}`));
  card.append(make('small', `Maya: ${row.verified_sales} verified sale now + ${row.returns_next_week ? '1' : '0'} modelled next week (${row.return_probability}% chance, draw 42)`));
  card.append(make('small', `Launch $9 + architecture ${money(arch.profit_cents)} + Maya ${money(row.maya_sales * 1000)} − migration ${money(row.migration_cost_cents)}`));
  return card;
}
function render(state) {
  current = state;
  if (!host && identity && identity.run_id !== state.id) {
    identity = null; initializedPick = false; localStorage.removeItem('black-friday-team');
  }
  $('headline').textContent = titles[state.phase] || state.phase;
  $('progress').textContent = state.message || messages[state.phase] || '';
  const step = ['ready', 'launching'].includes(state.phase) ? 'launch' : ['launch', 'migrating'].includes(state.phase) ? 'migrate' : 'compare';
  for (const name of ['launch', 'migrate', 'compare']) $(`stage-${name}`).classList.toggle('active', name === step);
  $('capacity').textContent = `${state.audience_count} / 8 audience teams · ${state.open ? 'joining and picks open' : 'round closed'}`;
  $('teams').replaceChildren(...state.teams.map(team => make('li', `${team.name}${team.house ? ' · house' : ''} · ${team.picked ? 'pick saved' : 'default pick (not saved)'}${!state.open ? ` · Release ${team.release} · ${team.architecture.storage} / ${team.architecture.replicas} / cache ${team.architecture.cache ? 'on' : 'off'}` : ''}`)));
  const finished = state.phase === 'compared';
  $('scoreboard').hidden = !finished;
  if (finished) $('scores').replaceChildren(...state.branches.map(score));
  if (host) {
    for (const [name, phase] of [['launch', 'ready'], ['migrate', 'launch'], ['compare', 'migrated']]) {
      $(name).disabled = pending || (state.phase !== phase && state.retry_action !== name);
    }
    $('reset').hidden = !['ready', 'compared', 'expired', 'failed'].includes(state.phase);
    $('execution').textContent = state.execution_arn || 'Not started';
    $('checkpoint').textContent = state.checkpoint ? `Launch checkpoint ${state.checkpoint} · VM ${state.launch_vm_id} · ${state.launch_vm_state}` : '';
    $('public-url').textContent = state.public_url; $('public-url').href = state.public_url;
    return;
  }
  const team = state.teams.find(team => team.id === identity?.id);
  $('join-section').hidden = Boolean(team) || !state.open;
  $('pick-section').hidden = !team || !state.open;
  $('waiting').hidden = finished || (!team && state.open);
  $('waiting-title').textContent = state.open ? 'Waiting for the round to close' : 'Watching the judges';
  $('waiting-detail').textContent = team ? `${team.name}: ${team.picked ? 'your pick is saved' : 'using the default pick until you save'}. ${state.open ? 'You can change it above until Close.' : 'Your release is frozen.'}` : 'The round is closed. Watch the results with everyone else.';
  if (team) {
    $('your-team').textContent = team.name;
    if (!initializedPick && team.architecture) {
      $('release').value = team.release; $('storage').value = team.architecture.storage;
      $('replicas').value = team.architecture.replicas; $('cache').checked = team.architecture.cache;
      initializedPick = true;
    }
  }
  const own = finished && state.branches.find(row => row.id === identity?.id);
  $('own-result').hidden = !own;
  if (own) $('own-result').replaceChildren(make('h2', `Your result · #${own.rank}`), make('p', `${own.name} · ${money(own.cumulative_cents)} profit`));
}
function signedOut() {
  $('panel').hidden = true;
  $('signin').hidden = false;
}
async function refresh() {
  if (pending) return;
  try {
    render(await api(host ? 'host/state' : 'state'));
    if (host) {
      $('signin').hidden = true;
      $('panel').hidden = false;
      if (!$('qr').getAttribute('src')) $('qr').src = '/api/host/qr.svg';
    }
  } catch (error) {
    if (host && error.status === 403) signedOut();
    else $('error').textContent = error.message;
  }
}
if (host) {
  $('login').addEventListener('submit', async event => {
    event.preventDefault(); if (pending) return; pending = true; $('login-error').textContent = '';
    try {
      await api('host/login', { passphrase: $('passphrase').value });
      $('passphrase').value = '';
    } catch (error) { $('login-error').textContent = error.message; }
    finally { pending = false; await refresh(); }
  });
  $('logout').addEventListener('click', async () => {
    try { await api('host/logout', {}); } catch (error) { $('error').textContent = error.message; }
    $('qr').removeAttribute('src');
    signedOut();
  });
  for (const action of ['launch', 'migrate', 'compare', 'reset']) $(action).addEventListener('click', async () => {
    if (pending) return;
    pending = true; $('error').textContent = ''; $(action).disabled = true;
    try { render(await api(action, undefined, true)); } catch (error) { $('error').textContent = error.message; }
    finally { pending = false; await refresh(); }
  });
} else {
  $('join').addEventListener('submit', async event => {
    event.preventDefault(); if (pending) return; pending = true; $('error').textContent = '';
    try { identity = await api('join', { name: $('team-name').value }); localStorage.setItem('black-friday-team', JSON.stringify(identity)); initializedPick = false; }
    catch (error) { $('error').textContent = error.message; }
    finally { pending = false; await refresh(); }
  });
  $('pick').addEventListener('submit', async event => {
    event.preventDefault(); if (pending) return; pending = true; $('error').textContent = '';
    try {
      await api('pick', { id: identity.id, token: identity.token, release: $('release').value, storage: $('storage').value, replicas: Number($('replicas').value), cache: $('cache').checked });
      $('saved').textContent = 'Pick saved. Waiting for the round to close.';
    } catch (error) { $('error').textContent = error.message; }
    finally { pending = false; await refresh(); }
  });
}
refresh();
setInterval(refresh, 2000);
