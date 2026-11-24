import subprocess
from pathlib import Path

AWS_DIR = Path(__file__).parent

# A one-item table that applies exactly the conditions roster.mjs sends, so the ordering of a
# pick against Close is deterministic. The live DynamoDB behaviour is proven separately.
PRELUDE = """
import assert from 'node:assert/strict';
import { houses, join, pick, remove, frozen } from './game.mjs';
import { ROSTER_WRITE, CLOSE, writeRoster, closeRound, s, n } from './roster.mjs';
const item = { run_id: s('run1'), phase: s('launch'), callback_id: s('cb1'), teams_data: s(JSON.stringify(houses())) };
const teams = () => JSON.parse(item.teams_data.S);
const store = {
  async update(expression, condition, values) {
    assert.equal(expression, ROSTER_WRITE.expression);
    assert.equal(condition, ROSTER_WRITE.condition);
    const ok = item.run_id.S === values[':id'].S && ['ready', 'launching', 'launch'].includes(item.phase.S)
      && item.teams_data.S === values[':before'].S;
    if (ok) item.teams_data = values[':next'];
    return ok;
  },
  async claim(expression, condition, values) {
    assert.equal(expression, CLOSE.expression);
    assert.equal(condition, CLOSE.condition);
    const ok = item.run_id.S === values[':id'].S && item.callback_id.S === values[':callback'].S && item.phase.S === values[':waiting'].S;
    if (!ok) return null;
    Object.assign(item, { phase: values[':busy'], lease_until: values[':lease'], frozen_data: item.teams_data });
    return { ...item };
  },
};
const joined = async name => {
  const { team, token } = join(teams(), item.phase.S, name);
  assert.ok(await writeRoster(store, item, [...teams(), team]));
  return { team, token };
};
const flip = (player, option) => pick(teams(), item.phase.S, { id: player.team.id, token: player.token, option });
"""


def javascript(source):
    return subprocess.check_output(['node', '--input-type=module', '-e', PRELUDE + source], cwd=AWS_DIR, text=True)


def test_a_pick_that_wins_the_race_with_close_is_frozen_and_one_that_loses_is_refused():
    javascript("""
      const player = await joined('Racer');
      // Pick A reads the roster, then Close lands, then pick A tries to write: it must lose.
      const stale = structuredClone(item);
      const lateTeams = teams().map(t => t.id === player.team.id ? flip(player, 'refactor') : t);
      const closed = await closeRound(store, item, n(1));
      assert.ok(closed, 'Close always succeeds');
      assert.equal(await writeRoster(store, stale, lateTeams), false, 'the late pick is refused');
      assert.throws(() => pick(teams(), item.phase.S, { id: player.team.id, token: player.token, option: 'refactor' }),
        e => e.status === 409);
      assert.equal(frozen(JSON.parse(closed.frozen_data.S)).find(t => t.id === player.team.id).option, 'lift');
      // A pick that committed first is in the frozen roster.
      Object.assign(item, { phase: s('launch'), callback_id: s('cb1') });
      delete item.frozen_data;
      assert.ok(await writeRoster(store, item, teams().map(t => t.id === player.team.id ? flip(player, 'refactor') : t)));
      const second = await closeRound(store, item, n(2));
      assert.equal(frozen(JSON.parse(second.frozen_data.S)).find(t => t.id === player.team.id).option, 'refactor');
    """)


def test_close_is_bound_to_the_run_the_callback_and_the_launch_phase():
    javascript("""
      assert.equal(await closeRound(store, { ...item, run_id: s('other') }, n(1)), null);
      assert.equal(await closeRound(store, { ...item, callback_id: s('stale') }, n(1)), null);
      assert.ok(await closeRound(store, item, n(1)));
      assert.equal(await closeRound(store, item, n(1)), null, 'a second Close cannot freeze again');
    """)


def test_a_flood_of_picks_around_close_never_blocks_close_and_never_changes_the_frozen_roster():
    javascript("""
      const player = await joined('Flipper');
      let committed = 0, refused = 0, closed;
      for (let flipCount = 0; flipCount < 40; flipCount++) {
        const snapshot = structuredClone(item);                    // the pick reads the roster...
        const next = (() => {
          try {
            return JSON.parse(snapshot.teams_data.S).map(t => t.id === player.team.id
              ? pick(JSON.parse(snapshot.teams_data.S), snapshot.phase.S, { id: player.team.id, token: player.token, option: flipCount % 2 === 0 ? 'refactor' : 'scale' }) : t);
          } catch (error) { assert.equal(error.status, 409); return null; }
        })();
        if (flipCount === 20) closed = await closeRound(store, item, n(1));   // ...Close lands mid-flood...
        if (next === null) { refused++; continue; }
        if (await writeRoster(store, snapshot, next)) committed++; else refused++;   // ...then it writes
      }
      assert.ok(closed, 'Close succeeded the first time');
      assert.ok(committed >= 1 && refused >= 1, `${committed} picks before Close, ${refused} refused after`);
      assert.deepEqual(JSON.parse(closed.frozen_data.S), JSON.parse(item.frozen_data.S));
      assert.equal(item.frozen_data.S, closed.frozen_data.S);
    """)


def test_remove_only_audience_teams_only_while_open():
    javascript("""
      const squatter = await joined('Squatter');
      assert.throws(() => remove(teams(), 'launch', 'housea'), e => e.status === 400);
      assert.throws(() => remove(teams(), 'launch', 'nobody'), e => e.status === 404);
      assert.throws(() => remove(teams(), 'migrating', squatter.team.id), e => e.status === 409);
      const left = remove(teams(), 'launch', squatter.team.id);
      assert.deepEqual(left.map(t => t.name), ['House A', 'House B']);
      assert.ok(await writeRoster(store, item, left));
      // The removed team's token no longer opens anything, and the name is free again.
      assert.throws(() => pick(teams(), 'launch', { id: squatter.team.id, token: squatter.token, option: 'lift' }),
        e => e.status === 403);
      await joined('Squatter');
    """)


def test_team_names_refuse_invisible_stacked_and_lookalike_duplicates():
    javascript("""
      const base = houses();
      for (const bad of ['\\u3164', 'ab\\u3164', '\\u115F', '\\u1160x', '\\uFFA0', 'a\\u200Bb', 'e\\u0301\\u0302\\u0303', 'x\\u0300\\u0301\\u0302\\u0303']) {
        assert.throws(() => join(base, 'ready', bad), e => e.status === 400, JSON.stringify(bad));
      }
      join(base, 'ready', 'Café');               // one or two combining marks are fine
      join(base, 'ready', 'e\\u0301\\u0302');
      const { team } = join(base, 'ready', 'ＡＢＣ');  // full-width letters fold with NFKC
      assert.throws(() => join([...base, team], 'ready', 'abc'), e => e.status === 409);
    """)
