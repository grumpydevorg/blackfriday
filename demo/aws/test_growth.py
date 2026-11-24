import functools
import gzip
import json
import re
import subprocess
from pathlib import Path

import pytest

import scoring

AWS_DIR = Path(__file__).parent


def javascript(source):
    return subprocess.check_output(['node', '--input-type=module', '-e', source], cwd=AWS_DIR, text=True)


def test_join_validation_uniqueness():
    javascript("""
      import assert from 'node:assert/strict';
      import { houses, join } from './game.mjs';
      const teams = houses();
      for (const name of ['', '   ', 'x'.repeat(21), 'bad\\nname', 'bad\\u0000']) {
        assert.throws(() => join(teams, 'ready', name), e => e.status === 400);
      }
      const first = join(teams, 'ready', '  Cart Keepers  ');
      assert.equal(first.team.name, 'Cart Keepers');
      assert.equal(first.token.length, 64);
      assert.equal(first.team.option, 'lift', 'a team that never picks plays Lift & shift');
      teams.push(first.team);
      assert.throws(() => join(teams, 'launch', 'cArT kEePeRs'), e => e.status === 409);
      assert.throws(() => join(teams, 'ready', 'HOUSE A'), e => e.status === 409);
      assert.throws(() => join(houses(), 'migrating', 'Late'), e => e.status === 409);
    """)


def test_there_is_no_room_limit_below_the_two_hundred_team_safety_cap():
    javascript("""
      import assert from 'node:assert/strict';
      import { houses, join, MAX_TEAMS } from './game.mjs';
      assert.equal(MAX_TEAMS, 200);
      const teams = houses();
      for (let n = 1; n <= 200; n++) teams.push(join(teams, 'launch', `Player ${n}`).team);
      assert.equal(teams.length, 202);
      assert.throws(() => join(teams, 'launch', 'Player 201'), e => e.status === 409 && e.message === 'The round is full');
    """)


def test_the_bots_names_are_reserved():
    javascript("""
      import assert from 'node:assert/strict';
      import { houses, join, BOTS } from './game.mjs';
      assert.ok(BOTS.length >= 4);
      for (const name of BOTS) {
        assert.throws(() => join(houses(), 'ready', name), e => e.status === 409, name);
        assert.throws(() => join(houses(), 'ready', name.toUpperCase()), e => e.status === 409, name);
      }
    """)


def test_houses_play_lift_and_refactor():
    javascript("""
      import assert from 'node:assert/strict';
      import { houses } from './game.mjs';
      assert.deepEqual(houses().map(t => [t.id, t.name, t.option, t.picked]),
        [['housea', 'House A', 'lift', true], ['houseb', 'House B', 'refactor', true]]);
    """)


def test_a_pick_is_one_of_three_options_needs_its_token_and_freezes_at_close():
    javascript("""
      import assert from 'node:assert/strict';
      import { houses, join, pick, frozen, publicTeams, ownTeam } from './game.mjs';
      const teams = houses(); const {team, token} = join(teams, 'ready', 'Shop'); teams.push(team);
      const body = {id: team.id, token, option: 'scale'};
      assert.throws(() => pick(teams, 'launch', {...body, token: 'wrong'}), e => e.status === 403);
      for (const option of ['', 'Lift', 'omit', 'huge', 7, null, undefined, ['lift']]) {
        assert.throws(() => pick(teams, 'launch', {...body, option}), e => e.status === 400, JSON.stringify(option));
      }
      // The old release / storage / replicas / cache fields are not a pick any more.
      assert.throws(() => pick(teams, 'launch', {id: team.id, token, release: 'A', storage: 'small', replicas: 1, cache: true}), e => e.status === 400);
      teams[2] = pick(teams, 'launch', body);
      assert.equal(teams[2].option, 'scale');
      assert.equal(teams[2].picked, true);
      // Before Close the roster carries only id, name, house and picked: no option, not even the viewer's own.
      assert.deepEqual(Object.keys(publicTeams(teams, 'launch')[2]).sort(), ['house', 'id', 'name', 'picked']);
      assert.deepEqual(ownTeam(teams, team.id, token), { id: team.id, name: 'Shop', picked: true, option: 'scale' });
      assert.equal(ownTeam(teams, team.id, 'wrong'), null);
      assert.equal(ownTeam(teams, 'housea', token), null);
      assert.equal(publicTeams(teams, 'migrating')[2].option, 'scale', 'revealed once the round closes');
      assert.deepEqual(Object.keys(frozen(teams)[2]).sort(), ['house', 'id', 'name', 'option']);
      assert.throws(() => pick(teams, 'migrating', body), e => e.status === 409);
    """)


def test_the_board_is_the_top_ten_the_viewers_own_row_and_a_count():
    javascript("""
      import assert from 'node:assert/strict';
      import { board } from './game.mjs';
      const rows = Array.from({ length: 40 }, (_, n) => ({ id: `t${n}`, rank: n + 1 }));
      assert.deepEqual(board(rows).rows.map(r => r.id), rows.slice(0, 10).map(r => r.id));
      assert.equal(board(rows).more, 30);
      assert.equal(board(rows).total, 40);
      const mine = board(rows, 't27');
      assert.equal(mine.rows.length, 11);
      assert.equal(mine.rows.at(-1).rank, 28);
      assert.equal(mine.more, 29);
      assert.equal(board(rows, 't3').rows.length, 10, 'a viewer already in the top ten gets no extra row');
      assert.equal(board(rows.slice(0, 4)).more, 0);
    """)


def test_the_phones_get_labels_risks_and_extra_cost_but_no_architecture():
    javascript("""
      import assert from 'node:assert/strict';
      import { publicOptions } from './game.mjs';
      const options = publicOptions();
      assert.deepEqual(options.map(o => [o.id, o.label, o.risk, o.extra_cost_cents]),
        [['lift', 'Lift & shift', 5, 0], ['scale', 'Scale up', 10, 4000], ['refactor', 'Refactor', 30, 0]]);
      assert.ok(!JSON.stringify(options).includes('storage'));
    """)


def test_public_team_list_never_carries_tokens():
    javascript("""
      import assert from 'node:assert/strict';
      import { publicTeams, houses } from './game.mjs';
      assert.ok(!JSON.stringify(publicTeams(houses(), 'ready')).includes('token'));
    """)


def test_javascript_and_python_read_the_same_option_table():
    assert list(scoring.OPTIONS) == ['lift', 'scale', 'refactor']
    shown = json.loads(javascript("""
      import { OPTIONS, BOTS } from './game.mjs';
      console.log(JSON.stringify({ options: OPTIONS, bots: BOTS }));
    """))
    assert shown == {'options': scoring.OPTIONS, 'bots': scoring.BOTS}
    assert all(scoring.TEAM_ID.fullmatch(option) for option in scoring.OPTIONS), 'option ids double as schema ids'
    assert all(scoring.TEAM_ID.fullmatch(scoring.bot_id(name)) for name in scoring.BOTS)


@functools.cache
def shared_result(option):
    """What the evaluator returns for one option: the real checks and the capacity model, before any roll."""
    from evaluator import Rehearsal

    stage = Rehearsal.parse({'id': 'a' * 32, 'action': 'compare', 'team': option, 'kind': 'preserve'})
    probability = scoring.return_probability(25)
    return {'cart_status': 200, 'maya_sales': scoring.maya_sales(1, probability), 'returns_next_week': True,
            'return_probability': probability, 'architecture': stage.capacity()}


@pytest.mark.parametrize(('option', 'broke', 'total'), [
    ('lift', False, 14500), ('lift', True, 2500),
    ('scale', False, 19200), ('scale', True, -1800),
    ('refactor', False, 23500), ('refactor', True, 2500),
])
def test_per_team_totals_for_each_option_and_outcome(option, broke, total):
    """Launch $9 + the wave on the team's shop + Maya's two sales ($20) - the $2 migration."""
    shared = shared_result(option)
    assert shared['maya_sales'] == 2
    risk = {'option': option, 'risk': scoring.OPTIONS[option]['risk'], 'roll': 0 if broke else 99, 'broke': broke}
    row = scoring.team_row({'id': 'housea', 'name': 'House A', 'house': True, 'option': option}, risk, shared)
    assert row['cumulative_cents'] == total
    wave = row['architecture']
    if broke:
        assert wave['verified_sales'] == 0 and wave['profit_cents'] == -wave['cost_cents']
    else:
        assert wave['verified_sales'] > 0
    assert wave['extra_cost_cents'] == scoring.OPTIONS[option]['extra_cost_cents']
    assert row['bot'] is False


def test_teams_that_picked_the_same_option_share_its_result_but_keep_their_own_roll():
    shared = shared_result('refactor')
    one, two = ({'id': name, 'name': name, 'house': False, 'option': 'refactor'} for name in ('one', 'two'))
    held = scoring.team_row(one, {'option': 'refactor', 'risk': 30, 'roll': 63, 'broke': False}, shared)
    broke = scoring.team_row(two, {'option': 'refactor', 'risk': 30, 'roll': 12, 'broke': True}, shared)
    assert (held['cumulative_cents'], broke['cumulative_cents']) == (23500, 2500)
    assert held['maya_sales'] == broke['maya_sales'] and held['cart_status'] == broke['cart_status'] == 200


def test_the_capacity_model_behind_each_option_is_virtual_rds():
    from evaluator import Rehearsal

    expected = {'lift': (12, 200, 11800), 'scale': (21, 500, 20500), 'refactor': (21, 200, 20800)}
    for option, (sales, cost, profit) in expected.items():
        stage = Rehearsal.parse({'id': 'a' * 32, 'action': 'compare', 'team': option, 'kind': 'preserve'})
        result = stage.capacity()
        assert (result['verified_sales'], result['cost_cents'], result['profit_cents']) == (sales, cost, profit)
        assert stage.step == f'compare-{option}'


def test_the_evaluator_judges_options_not_teams_and_refuses_the_old_shapes():
    from evaluator import Rehearsal

    for action in ('migrate', 'compare'):
        for option in scoring.OPTIONS:
            assert Rehearsal.parse({'id': 'a' * 32, 'action': action, 'team': option, 'kind': 'preserve'}).team == option
    good = {'id': 'a' * 32, 'action': 'compare', 'team': 'lift', 'kind': 'preserve'}
    for bad in [
        {**good, 'team': 'housea'}, {**good, 'team': 'omit'}, {**good, 'kind': 'omit'}, {**good, 'broke': False},
        {**good, 'option': 'lift'}, {**good, 'architecture': {}},
        {key: value for key, value in good.items() if key != 'kind'},
    ]:
        with pytest.raises(ValueError):
            Rehearsal.parse(bad)


def test_ranking_shares_equal_ranks():
    rows = [{'id': name, 'name': name, 'cumulative_cents': profit} for name, profit in [('c', 10), ('a', 20), ('b', 20)]]
    assert [(row['name'], row['rank']) for row in scoring.rank(rows)] == [('a', 1), ('b', 1), ('c', 3)]


def test_team_validation_accepts_only_the_frozen_option_shape():
    team = {'id': 'housea', 'name': 'House A', 'house': True, 'option': 'lift'}
    assert scoring.validate_teams([team]) == [team]
    for bad in [
        {**team, 'option': 'omit'}, {**team, 'release': 'A'}, {**team, 'architecture': {}}, {**team, 'bot': True},
        {key: value for key, value in team.items() if key != 'option'},
    ]:
        with pytest.raises(ValueError):
            scoring.validate_teams([bad])
    many = [{'id': f't{n:05d}', 'name': f'P{n}', 'house': False, 'option': 'lift'} for n in range(202)]
    assert len(scoring.validate_teams(many)) == 202
    with pytest.raises(ValueError):
        scoring.validate_teams(many + [{'id': 'toomany1', 'name': 'x', 'house': False, 'option': 'lift'}])


def audience(count):
    options = list(scoring.OPTIONS)
    houses = [{'id': 'housea', 'name': 'House A', 'house': True, 'option': 'lift'},
              {'id': 'houseb', 'name': 'House B', 'house': True, 'option': 'refactor'}]
    return houses + [{'id': f't{n:012x}', 'name': f'Player {n}', 'house': False, 'option': options[n % 3]} for n in range(count)]


@pytest.mark.parametrize(('count', 'bots'), [(0, 4), (1, 3), (2, 2), (3, 1), (4, 0), (40, 0)])
def test_a_thin_room_is_padded_with_bots_up_to_six_teams(count, bots):
    draws = iter([1, 2, 0, 1] + [50] * 300)
    plan = scoring.plan_round(audience(count), lambda bound: next(draws) % bound)
    added = plan['teams'][count + 2:]
    assert len(added) == bots
    assert [team['name'] for team in added] == scoring.BOTS[:bots]
    assert all(team['bot'] and team['house'] and team['option'] in scoring.OPTIONS for team in added)
    assert len({team['id'] for team in plan['teams']}) == len(plan['teams'])
    assert set(plan['risks']) == {team['id'] for team in plan['teams']}
    assert len(plan['teams']) == max(6, count + 2)


def test_bots_options_are_drawn_before_the_rolls_and_only_once():
    asked = []
    plan = scoring.plan_round(audience(0), lambda bound: asked.append(bound) or 7 % bound)
    assert asked == [3] * 4 + [100] * 6  # four bot options, then one roll for each of the six teams
    assert all(risk['roll'] == 7 for risk in plan['risks'].values())


def test_team_rows_label_bots_and_stay_small_enough_for_a_dynamodb_item():
    """200 audience teams: every item the room writes stays far under 400 KB, and the callback under 64 KB."""
    shared = {option: shared_result(option) for option in scoring.OPTIONS}
    teams = [{**team, 'name': '𝔸' * 20} for team in audience(200)]  # worst case: 20 four-byte characters
    plan = scoring.plan_round(teams)
    rows = scoring.rank([scoring.team_row(team, plan['risks'][team['id']], shared[team['option']]) for team in plan['teams']])
    roster = json.dumps([{**team, 'picked': True, 'token_hash': 'f' * 64} for team in teams])
    frozen = json.dumps([{key: team[key] for key in ('id', 'name', 'house', 'option')} for team in teams])
    outcomes = json.dumps(rows)
    callback = json.dumps({'action': 'migrate', 'teams': json.loads(frozen)})
    item = len(roster.encode()) + len(frozen.encode()) + len(outcomes.encode())
    print(f'roster {len(roster):,} B, frozen {len(frozen):,} B, outcomes {len(outcomes):,} B, callback {len(callback):,} B, item ~{item:,} B')
    assert item < 300_000
    assert len(callback.encode()) < 64_000
    assert len(json.dumps(plan).encode()) < 100_000  # the checkpoint of the migration-risk step
    assert {row['bot'] for row in rows} == {False}
    padded = scoring.plan_round(audience(0))
    assert sum(team.get('bot', False) for team in padded['teams']) == 4


def test_state_sizes_for_a_room_of_forty_and_two_hundred():
    from_node = json.loads(javascript("""
      import { houses, join, publicTeams, publicOptions, board } from './game.mjs';
      const sizes = {};
      for (const count of [40, 200]) {
        const teams = houses();
        for (let n = 1; n <= count; n++) teams.push({ ...join(teams, 'launch', `Player number ${n}`).team, picked: true });
        const open = JSON.stringify({ phase: 'launch', open: true, teams: publicTeams(teams, 'launch'), options: publicOptions() });
        sizes[count] = open;
      }
      console.log(JSON.stringify(sizes));
    """))
    rows = scoring.rank([scoring.team_row(team, {'option': team['option'], 'risk': 5, 'roll': 1, 'broke': True}, shared_result(team['option']))
                         for team in audience(200)])
    for count, open_state in from_node.items():
        raw, packed = len(open_state.encode()), len(gzip.compress(open_state.encode()))
        print(f'open /api/state at {count} teams: {raw:,} B raw, {packed:,} B gzip')
        assert raw < 40_000 and packed < 10_000
    board_json = json.dumps({'phase': 'compared', 'teams': [], 'board': {'rows': rows[:11], 'total': 202, 'more': 191}})
    assert len(board_json.encode()) < 12_000


def test_the_web_package_contains_every_file_the_web_function_loads():
    sources = (AWS_DIR / 'web.mjs').read_text() + (AWS_DIR / 'game.mjs').read_text()
    needed = set(re.findall(r"from '\./([\w.-]+)'", sources)) | set(re.findall(r"new URL\('\./([\w./-]+)'", sources))
    assert {'game.mjs', 'host_auth.mjs', 'roster.mjs', 'options.json', 'player.html', 'host.html', 'round.js', 'round.css',
            'art/cloud-surge.jpg'} <= needed
    packaging = (AWS_DIR / 'operate.py').read_text()
    missing = {name for name in needed if f"'{name}'" not in packaging and name.rsplit('/', 1)[-1] not in packaging}
    assert not missing, missing
    for name in needed:
        assert (AWS_DIR / name).is_file(), name
    assert (AWS_DIR / 'art' / 'cloud-surge.jpg').stat().st_size <= 300_000
    # The Python zips read the same table next to scoring.py.
    for zipped in ("'workflow.py'", "'evaluator.py'"):
        block = packaging[packaging.index(zipped):packaging.index(zipped) + 220]
        assert "'options.json'" in block, zipped


def test_the_web_role_may_invoke_every_workflow_version_not_just_the_newest():
    """After a deploy the previous Web Function revision keeps serving; it must still be able to start a round."""
    template = (AWS_DIR / 'rehearsal.yaml').read_text()
    block = template[template.index('Action: lambda:InvokeFunction', template.index('WebRole:')):][:700]
    assert "Resource: !Sub '${Workflow.Arn}:*'" in block
    assert 'Resource: !Ref WorkflowVersion' not in block
