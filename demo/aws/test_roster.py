import subprocess
from pathlib import Path

AWS_DIR = Path(__file__).parent

# A one-item table that applies exactly the conditions roster.mjs sends, so the ordering of a
# pick against Close is deterministic. The live DynamoDB behaviour is proven separately.
PRELUDE = """
import assert from 'node:assert/strict';
import { join, pick, remove, rename } from './game.mjs';
import { ROSTER_WRITE, CLOSE, RETRY_CLOSE, RELEASE_CLOSE, writeRoster, closeRound, retryClose, releaseClose, s, n } from './roster.mjs';
const item = { run_id: s('run1'), phase: s('open'), round: s('launch'), callback_id: s('cb1'), teams_data: s('[]') };
const players = () => JSON.parse(item.teams_data.S);
const phase = () => item.phase.S, round = () => item.round.S;
const store = {
  async update(expression, condition, values) {
    if (expression === ROSTER_WRITE.expression) {
      assert.equal(condition, ROSTER_WRITE.condition);
      const ok = item.run_id.S === values[':id'].S && item.teams_data.S === values[':before'].S
        && (['ready', 'starting'].includes(item.phase.S) || (item.phase.S === 'open' && item.round?.S === values[':round'].S));
      if (ok) item.teams_data = values[':next'];
      return ok;
    }
    assert.equal(expression, RELEASE_CLOSE.expression);
    assert.equal(condition, RELEASE_CLOSE.condition);
    const ok = item.run_id.S === values[':id'].S && item.callback_id.S === values[':callback'].S
      && item.phase.S === 'closing' && item.lease_until?.N === values[':lease'].N;
    if (ok) delete item.lease_until;
    return ok;
  },
  async claim(expression, condition, values) {
    if (expression === CLOSE.expression) {
      assert.equal(condition, CLOSE.condition);
      const ok = item.run_id.S === values[':id'].S && item.callback_id.S === values[':callback'].S
        && item.phase.S === values[':open'].S && item.round.S === values[':round'].S;
      if (!ok) return null;
      Object.assign(item, { phase: values[':closing'], lease_until: values[':lease'], frozen_data: item.teams_data });
      return { ...item };
    }
    assert.equal(expression, RETRY_CLOSE.expression);
    assert.equal(condition, RETRY_CLOSE.condition);
    const ok = item.run_id.S === values[':id'].S && item.callback_id.S === values[':callback'].S
      && item.phase.S === 'closing' && item.round.S === values[':round'].S && Number(item.lease_until?.N) <= Number(values[':now'].N);
    if (!ok) return null;
    item.lease_until = values[':lease'];
    return { ...item };
  },
};
const joined = async name => {
  const { player, token } = join(players(), phase(), round(), name);
  assert.ok(await writeRoster(store, item, [...players(), player]));
  return { player, token };
};
const flip = (who, option, at = round()) => pick(players(), phase(), round(), { id: who.player.id, token: who.token, round: at, option });
const frozenPick = (closed, who, at = 'launch') => JSON.parse(closed.frozen_data.S).find(t => t.id === who.player.id).picks[at];
"""


def javascript(source):
    return subprocess.check_output(['node', '--input-type=module', '-e', PRELUDE + source], cwd=AWS_DIR, text=True)


def test_a_pick_that_wins_the_race_with_close_is_frozen_and_one_that_loses_is_refused():
    javascript("""
      const player = await joined('Racer');
      // Pick A reads the roster, then Close lands, then pick A tries to write: it must lose.
      const stale = structuredClone(item);
      const latePlayers = players().map(t => t.id === player.player.id ? flip(player, 'large') : t);
      const closed = await closeRound(store, item, 'launch', n(1));
      assert.ok(closed, 'Close always succeeds');
      assert.equal(await writeRoster(store, stale, latePlayers), false, 'the late pick is refused');
      assert.throws(() => flip(player, 'large'), e => e.status === 409);
      assert.equal(frozenPick(closed, player), undefined, 'no pick means the default');
      // A pick that committed first is in the frozen roster.
      Object.assign(item, { phase: s('open'), callback_id: s('cb1') });
      delete item.frozen_data;
      assert.ok(await writeRoster(store, item, players().map(t => t.id === player.player.id ? flip(player, 'large') : t)));
      const second = await closeRound(store, item, 'launch', n(2));
      assert.equal(frozenPick(second, player), 'large');
    """)


def test_close_is_bound_to_the_run_the_callback_the_round_and_the_open_phase():
    javascript("""
      assert.equal(await closeRound(store, { ...item, run_id: s('other') }, 'launch', n(1)), null);
      assert.equal(await closeRound(store, { ...item, callback_id: s('stale') }, 'launch', n(1)), null);
      assert.equal(await closeRound(store, item, 'growth', n(1)), null, 'a Close for another round is refused');
      assert.ok(await closeRound(store, item, 'launch', n(1)));
      assert.equal(await closeRound(store, item, 'launch', n(1)), null, 'a second Close cannot freeze again');
    """)


def test_a_lost_callback_is_retried_only_after_its_lease_expires_and_with_the_first_frozen_roster():
    javascript("""
      const player = await joined('Retry');
      const closed = await closeRound(store, item, 'launch', n(1000));
      assert.equal(await retryClose(store, item, 'launch', n(2000), 500), null, 'the lease is still held');
      const again = await retryClose(store, item, 'launch', n(3000), 1500);
      assert.ok(again);
      assert.equal(again.frozen_data.S, closed.frozen_data.S);
      assert.equal(await retryClose(store, item, 'growth', n(4000), 9000), null, 'wrong round');
      assert.equal(await releaseClose(store, item, n(1)), false, 'a stale lease is not released');
      assert.ok(await releaseClose(store, item, n(3000)));
      assert.equal(item.lease_until, undefined);
    """)


def test_a_flood_of_picks_around_close_never_blocks_close_and_never_changes_the_frozen_roster():
    javascript("""
      const player = await joined('Flipper');
      let committed = 0, refused = 0, closed;
      for (let count = 0; count < 40; count++) {
        const snapshot = structuredClone(item);                    // the pick reads the roster...
        const next = (() => {
          try {
            return JSON.parse(snapshot.teams_data.S).map(t => t.id === player.player.id
              ? pick(JSON.parse(snapshot.teams_data.S), snapshot.phase.S, snapshot.round.S,
                { id: player.player.id, token: player.token, round: 'launch', option: count % 2 === 0 ? 'large' : 'medium' }) : t);
          } catch (error) { assert.equal(error.status, 409); return null; }
        })();
        if (count === 20) closed = await closeRound(store, item, 'launch', n(1));   // ...Close lands mid-flood...
        if (next === null) { refused++; continue; }
        if (await writeRoster(store, snapshot, next)) committed++; else refused++;   // ...then it writes
      }
      assert.ok(closed, 'Close succeeded the first time');
      assert.ok(committed >= 1 && refused >= 1, `${committed} picks before Close, ${refused} refused after`);
      assert.deepEqual(JSON.parse(closed.frozen_data.S), JSON.parse(item.frozen_data.S));
      assert.equal(item.frozen_data.S, closed.frozen_data.S);
    """)


def test_a_roster_write_is_bound_to_the_round_it_was_read_in():
    javascript("""
      const player = await joined('Slow');
      const stale = structuredClone(item);                          // read while launch was open
      const next = players().map(t => t.id === player.player.id ? flip(player, 'large') : t);
      Object.assign(item, { phase: s('open'), round: s('growth') });  // the next round opened meanwhile
      assert.equal(await writeRoster(store, stale, next), false);
      assert.deepEqual(players().map(t => t.picks), [{}]);
    """)


def test_pick_is_bound_to_the_open_round_and_to_requires():
    javascript("""
      const player = await joined('Picky');
      assert.throws(() => flip(player, 'careful', 'growth'), e => e.status === 409, 'growth is not open');
      assert.throws(() => flip(player, 'small', 'nonsense'), e => e.status === 400);
      assert.throws(() => flip(player, 'huge'), e => e.status === 400);
      Object.assign(player.player, players()[0]);
      Object.assign(item, { phase: s('open'), round: s('blackfriday') });
      assert.throws(() => flip(player, 'autoscale'), e => e.status === 409 && /shared database/.test(e.message));
      item.teams_data = s(JSON.stringify([{ ...players()[0], picks: { growth: 'stay' } }]));
      assert.throws(() => flip(player, 'autoscale'), e => e.status === 409);
      item.teams_data = s(JSON.stringify([{ ...players()[0], picks: { growth: 'careful' } }]));
      assert.equal(flip(player, 'autoscale').picks.blackfriday, 'autoscale');
      assert.equal(flip(player, 'queue').picks.growth, 'careful', 'earlier picks are kept');
    """)


def test_remove_only_audience_players_and_never_while_a_round_is_closing():
    javascript("""
      const squatter = await joined('Squatter');
      assert.throws(() => remove(players(), 'open', 'nobody'), e => e.status === 404);
      assert.throws(() => remove(players(), 'closing', squatter.player.id), e => e.status === 409);
      assert.throws(() => remove(players(), 'final', squatter.player.id), e => e.status === 409);
      const left = remove(players(), 'open', squatter.player.id);
      assert.deepEqual(left, []);
      assert.ok(await writeRoster(store, item, left));
      // The removed player's token no longer opens anything, and the name is free again.
      assert.throws(() => flip(squatter, 'large'), e => e.status === 403);
      await joined('Squatter');
    """)


def test_names_refuse_invisible_stacked_and_lookalike_duplicates_and_bots():
    javascript("""
      for (const bad of ['\\u3164', 'ab\\u3164', '\\u115F', '\\u1160x', '\\uFFA0', 'a\\u200Bb', 'e\\u0301\\u0302\\u0303', 'x\\u0300\\u0301\\u0302\\u0303',
          'x'.repeat(21)]) {
        assert.throws(() => join([], 'ready', 'ready', bad), e => e.status === 400, JSON.stringify(bad));
      }
      assert.throws(() => join([], 'ready', undefined, 5), e => e.status === 400);
      join([], 'ready', undefined, 'Café');       // one or two combining marks are fine
      join([], 'ready', undefined, 'e\\u0301\\u0302');
      const { player } = join([], 'ready', undefined, 'ＡＢＣ');  // full-width letters fold with NFKC
      assert.throws(() => join([player], 'ready', undefined, 'abc'), e => e.status === 409);
      assert.throws(() => join([], 'ready', undefined, 'bot lambo'), e => e.status === 409, 'bot names are reserved');
      assert.throws(() => join([], 'ready', undefined, 'HOUSE (does nothing)'), e => e.status === 409);
    """)


def test_rename_keeps_your_own_name_free_and_stops_when_launch_closes():
    javascript("""
      const me = await joined('Alpha');
      const other = await joined('Beta');
      const ask = (who, name, at = round(), now = phase()) => rename(players(), now, at, { id: who.player.id, token: who.token, name });
      assert.equal(ask(me, 'ALPHA').name, 'ALPHA', 'a case change of your own name is fine');
      assert.throws(() => ask(me, 'beta'), e => e.status === 409);
      assert.throws(() => ask({ ...me, token: 'nope' }, 'Gamma'), e => e.status === 403);
      assert.throws(() => ask(me, 'Gamma', 'growth'), e => e.status === 409, 'growth is open: names are locked');
      assert.throws(() => ask(me, 'Gamma', 'launch', 'closing'), e => e.status === 409);
      assert.ok(ask(me, 'Gamma', undefined, 'starting'));
      assert.ok(ask(me, '').name.includes(' '), 'a blank rename generates a name');
    """)
