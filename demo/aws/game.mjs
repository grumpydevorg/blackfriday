import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
// A safety cap, not a game limit: the DynamoDB item and the Durable callback stay far under their limits at this size.
export const MAX_TEAMS = 200;
// The round's single table of options (labels, risk, extra cost, architecture) and bot names, shared with scoring.py.
const TABLE = JSON.parse(readFileSync(new URL('./options.json', import.meta.url)));
export const OPTIONS = TABLE.options;
export const BOTS = TABLE.bots;
export const optionIds = Object.keys(OPTIONS);
export const open = phase => ['ready', 'launching', 'launch'].includes(phase);
export class RequestError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export const digest = value => createHash('sha256').update(value ?? '').digest('hex');
export const equal = (left, right) => timingSafeEqual(Buffer.from(digest(left)), Buffer.from(digest(right)));
export const defaultPick = () => ({ option: 'lift' });
export const houses = () => [['A', 'lift'], ['B', 'refactor']].map(([letter, option]) => ({
  id: `house${letter.toLowerCase()}`, name: `House ${letter}`, house: true, picked: true, option,
}));
// What the phones render: the label, one line, the risk and any extra cost. No architecture.
export const publicOptions = () => optionIds.map(id => {
  const { label, blurb, risk, extra_cost_cents: extra } = OPTIONS[id];
  return { id, label, blurb, risk, extra_cost_cents: extra };
});
const fold = value => value.normalize('NFKC').toLowerCase();
export function join(teams, phase, name) {
  if (!open(phase)) throw new RequestError(409, 'The round is closed');
  if (typeof name !== 'string') throw new RequestError(400, 'Enter a team name');
  name = name.trim();
  // Printable only: no control or format characters, no invisible Hangul fillers that render
  // as blank, and no stacked combining marks (zalgo) that overflow the scoreboard.
  if (
    ![...name].length || [...name].length > 20 || /[\p{C}\p{Zl}\p{Zp}\u3164\u115F\u1160\uFFA0]/u.test(name)
    || /\p{M}{3,}/u.test(name)
  ) throw new RequestError(400, 'Use 1–20 printable characters');
  // The bots' names are reserved, so a bot row can never be mistaken for a player.
  if (teams.some(team => fold(team.name) === fold(name)) || BOTS.some(bot => fold(bot) === fold(name))) {
    throw new RequestError(409, 'That team name is taken');
  }
  if (teams.filter(team => !team.house).length >= MAX_TEAMS) throw new RequestError(409, 'The round is full');
  const token = randomBytes(32).toString('hex');
  const team = { id: `t${randomBytes(6).toString('hex')}`, name, house: false, picked: false, token_hash: digest(token), ...defaultPick() };
  return { team, token };
}
export function pick(teams, phase, body) {
  if (!open(phase)) throw new RequestError(409, 'The round is closed');
  const team = teams.find(team => team.id === body.id && !team.house);
  if (!team || typeof body.token !== 'string' || !equal(digest(body.token), team.token_hash)) throw new RequestError(403, 'Team token required');
  if (typeof body.option !== 'string' || !optionIds.includes(body.option)) {
    throw new RequestError(400, `Choose one of: ${optionIds.join(', ')}`);
  }
  return { ...team, picked: true, option: body.option };
}
export function remove(teams, phase, id) {
  if (!open(phase)) throw new RequestError(409, 'The round is closed');
  const team = typeof id === 'string' ? teams.find(value => value.id === id) : undefined;
  if (!team) throw new RequestError(404, 'No such team');
  if (team.house) throw new RequestError(400, 'House teams cannot be removed');
  return teams.filter(value => value.id !== id);
}
export const frozen = teams => teams.map(({ id, name, house, option }) => ({ id, name, house, option }));
// The roster everyone may see: no option until the round closes. Nothing about a team's own pick
// travels here; its own phone reads that from `ownTeam`.
export const publicTeams = (teams, phase) => teams.map(team => ({
  id: team.id, name: team.name, house: team.house, picked: team.picked, ...(open(phase) ? {} : { option: team.option }),
}));
export function ownTeam(teams, id, token) {
  const team = typeof id === 'string' ? teams.find(value => value.id === id && !value.house) : undefined;
  if (!team || typeof token !== 'string' || !equal(digest(token), team.token_hash)) return null;
  return { id: team.id, name: team.name, picked: team.picked, option: team.option };
}
export const BOARD_ROWS = 10;
/** The scoreboard a phone or the projector needs: the top rows, the viewer's own row, and how many are left. */
export function board(rows, ownId) {
  const shown = rows.slice(0, BOARD_ROWS);
  const own = ownId && !shown.some(row => row.id === ownId) ? rows.find(row => row.id === ownId) : undefined;
  return { rows: own ? [...shown, own] : shown, total: rows.length, more: rows.length - shown.length - (own ? 1 : 0) };
}
