import { createHash, randomBytes, randomInt, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
// rounds.json is the one table of rounds, options, names and bots, shared with model.py and the pages.
const TABLE = JSON.parse(readFileSync(new URL('./rounds.json', import.meta.url)));
export const ROUNDS = TABLE.rounds;
export const ROUND_IDS = ROUNDS.map(round => round.id);
export const PLAYER_CAP = TABLE.player_cap;
export const BOTS = TABLE.bots;
export const HOUSE = TABLE.house;
export const NAMES = TABLE.names;
const roundOf = id => ROUNDS.find(round => round.id === id);
export class RequestError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export const digest = value => createHash('sha256').update(value ?? '').digest('hex');
export const equal = (left, right) => timingSafeEqual(Buffer.from(digest(left)), Buffer.from(digest(right)));

/** The first option of a round is what nobody picked. */
export const defaultOption = roundId => roundOf(roundId).options[0].id;
/** True when `option` exists in the round and its `requires` is met by the earlier picks. */
export function allowed(roundId, option, picks) {
  const found = roundOf(roundId)?.options.find(candidate => candidate.id === option);
  if (!found) return false;
  const need = found.requires;
  return !need || need.options.includes(picks?.[need.round]);
}
/** What the phones render about the rounds: everything in rounds.json except the model block and the names. */
export const publicRounds = () => ROUNDS.map(({ id, title, weeks, forecast, market, options }) => ({
  id, title, weeks, forecast, market,
  options: options.map(({ id, label, blurb, cost_text, risk, dice_text, requires, evidence }) => (
    { id, label, blurb, cost_text, risk, dice_text, requires, evidence })),
}));

/** Joining: before the start, while it starts, and while the first two rounds are open. */
export const joinable = (phase, round) => ['ready', 'starting'].includes(phase)
  || (phase === 'open' && ['launch', 'growth'].includes(round));
/** Renaming ends when the launch round closes. */
export const renamable = (phase, round) => ['ready', 'starting'].includes(phase) || (phase === 'open' && round === 'launch');
/** Picks and removals touch the roster only while no round is closing. */
export const editable = phase => ['ready', 'starting', 'open'].includes(phase);

const fold = value => value.normalize('NFKC').toLowerCase();
const reserved = () => [...BOTS, HOUSE.name].map(fold);
function checkName(name) {
  name = name.trim();
  // Printable only: no control or format characters, no invisible Hangul fillers that render
  // as blank, and no stacked combining marks (zalgo) that overflow the scoreboard.
  if (
    ![...name].length || [...name].length > 20 || /[\p{C}\p{Zl}\p{Zp}\u3164\u115F\u1160\uFFA0]/u.test(name)
    || /\p{M}{3,}/u.test(name)
  ) throw new RequestError(400, 'Use 1–20 printable characters');
  return name;
}
const taken = (players, name, skip) => reserved().includes(fold(name))
  || players.some(player => player.id !== skip && fold(player.name) === fold(name));

/** "<Adjective> <Noun>", unique against the players and the bots; a collision gets " 2", " 3"… while it still fits 20 characters. */
export function generateName(players, draw = randomInt) {
  const { adjectives, nouns } = NAMES;
  for (let attempt = 0; attempt < 1000; attempt++) {
    const base = `${adjectives[draw(adjectives.length)]} ${nouns[draw(nouns.length)]}`;
    for (let suffix = 1; [...base].length + (suffix > 1 ? String(suffix).length + 1 : 0) <= 20; suffix++) {
      const name = suffix > 1 ? `${base} ${suffix}` : base;
      if (!taken(players, name)) return name;
    }
  }
  throw new RequestError(409, 'No names left, enter your own');
}

function nameFor(players, value, skip) {
  if (value === undefined || value === null || (typeof value === 'string' && !value.trim())) {
    return generateName(skip ? players.filter(player => player.id !== skip) : players);
  }
  if (typeof value !== 'string') throw new RequestError(400, 'Name must be text');
  const name = checkName(value);
  if (taken(players, name, skip)) throw new RequestError(409, 'That name is taken');
  return name;
}

export function join(players, phase, round, name) {
  if (!joinable(phase, round)) throw new RequestError(409, 'Joining has closed');
  if (players.length >= PLAYER_CAP) throw new RequestError(409, 'The game is full');
  const token = randomBytes(32).toString('hex');
  const player = { id: `t${randomBytes(6).toString('hex')}`, name: nameFor(players, name), house: false, token_hash: digest(token), picks: {} };
  return { player, token };
}

/** The player this id and token belong to, or null. */
export function find(players, id, token) {
  const player = typeof id === 'string' ? players.find(value => value.id === id && !value.house) : undefined;
  return player && typeof token === 'string' && equal(digest(token), player.token_hash) ? player : null;
}
function authenticated(players, body) {
  const player = find(players, body.id, body.token);
  if (!player) throw new RequestError(403, 'Player token required');
  return player;
}

export function rename(players, phase, round, body) {
  const player = authenticated(players, body);
  if (!renamable(phase, round)) throw new RequestError(409, 'Names are locked once the launch round closes');
  // A blank rename asks for a fresh generated name rather than clearing it.
  return { ...player, name: nameFor(players, body.name, player.id) };
}

export function pick(players, phase, round, body) {
  const player = authenticated(players, body);
  if (typeof body.round !== 'string' || !ROUND_IDS.includes(body.round)) {
    throw new RequestError(400, `Choose a round: ${ROUND_IDS.join(', ')}`);
  }
  if (phase !== 'open' || body.round !== round) throw new RequestError(409, 'That round is closed');
  const options = roundOf(round).options;
  if (typeof body.option !== 'string' || !options.some(option => option.id === body.option)) {
    throw new RequestError(400, `Choose one of: ${options.map(option => option.id).join(', ')}`);
  }
  if (!allowed(round, body.option, player.picks)) {
    throw new RequestError(409, options.find(option => option.id === body.option).requires.reason);
  }
  return { ...player, picks: { ...player.picks, [round]: body.option } };
}

export function remove(players, phase, id) {
  if (!editable(phase)) throw new RequestError(409, 'The round is closing');
  if (typeof id !== 'string' || !players.some(player => player.id === id)) throw new RequestError(404, 'No such player');
  return players.filter(player => player.id !== id);
}

/** What the workflow gets from Close: the roster at that instant, without token hashes. */
export const closePayload = (round, frozen) => ({
  action: 'close', round,
  players: frozen.map(({ id, name, house, picks }) => ({
    id, name, house: Boolean(house), ...(picks?.[round] ? { option: picks[round] } : {}), picks: picks ?? {},
  })),
});

/** What a phone may know about itself. */
export function me(players, id, token) {
  const player = find(players, id, token);
  return player ? { id: player.id, name: player.name, picks: player.picks ?? {} } : null;
}
/** The host's roster: who is in, and who has picked for the open round. */
export const hostPlayers = (players, round) => players.map(player => ({
  id: player.id, name: player.name, house: Boolean(player.house), bot: false, picked: Boolean(round && player.picks?.[round]),
}));

export const BOARD_ROWS = 10;
/** The scoreboard a phone or the projector needs: the top rows, the viewer's own row, and how many are left. */
export function board(rows, ownId) {
  const shown = rows.slice(0, BOARD_ROWS);
  const own = ownId && !shown.some(row => row.id === ownId) ? rows.find(row => row.id === ownId) : undefined;
  return { rows: own ? [...shown, own] : shown, total: rows.length, more: rows.length - shown.length - (own ? 1 : 0) };
}
