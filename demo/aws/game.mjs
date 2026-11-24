import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
export const MAX_TEAMS = 8;
export const open = phase => ['ready', 'launching', 'launch'].includes(phase);
export class RequestError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export const digest = value => createHash('sha256').update(value ?? '').digest('hex');
export const equal = (left, right) => timingSafeEqual(Buffer.from(digest(left)), Buffer.from(digest(right)));
export const defaultPick = () => ({ release: 'A', kind: 'preserve', architecture: { storage: 'small', replicas: 1, cache: false } });
export const houses = () => ['A', 'B'].map(release => ({
  id: `house${release.toLowerCase()}`, name: `House ${release}`, house: true, picked: true,
  ...defaultPick(), release, kind: release === 'A' ? 'preserve' : 'omit',
}));
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
  const same = value => value.normalize('NFKC').toLowerCase();
  if (teams.some(team => same(team.name) === same(name))) throw new RequestError(409, 'That team name is taken');
  if (teams.filter(team => !team.house).length >= MAX_TEAMS) throw new RequestError(409, 'All eight audience places are taken');
  const token = randomBytes(32).toString('hex');
  const team = { id: `t${randomBytes(6).toString('hex')}`, name, house: false, picked: false, token_hash: digest(token), ...defaultPick() };
  return { team, token };
}
export function pick(teams, phase, body) {
  if (!open(phase)) throw new RequestError(409, 'The round is closed');
  const team = teams.find(team => team.id === body.id && !team.house);
  if (!team || typeof body.token !== 'string' || !equal(digest(body.token), team.token_hash)) throw new RequestError(403, 'Team token required');
  const { release, storage, replicas, cache } = body;
  if (!['A', 'B'].includes(release) || !['local', 'small', 'large'].includes(storage) || ![1, 2].includes(replicas) || typeof cache !== 'boolean') {
    throw new RequestError(400, 'Choose a release, storage tier, one or two replicas, and cache');
  }
  return { ...team, picked: true, release, kind: release === 'A' ? 'preserve' : 'omit', architecture: { storage, replicas, cache } };
}
export function remove(teams, phase, id) {
  if (!open(phase)) throw new RequestError(409, 'The round is closed');
  const team = typeof id === 'string' ? teams.find(value => value.id === id) : undefined;
  if (!team) throw new RequestError(404, 'No such team');
  if (team.house) throw new RequestError(400, 'House teams cannot be removed');
  return teams.filter(value => value.id !== id);
}
export const frozen = teams => teams.map(({ id, name, house, release, kind, architecture }) => ({ id, name, house, release, kind, architecture }));
export function publicTeams(teams, phase, id, token) {
  return teams.map(team => ({ id: team.id, name: team.name, house: team.house, picked: team.picked,
    ...(!open(phase) || (team.id === id && equal(digest(token), team.token_hash)) ? {
      release: team.release, architecture: team.architecture,
    } : {}),
  }));
}
