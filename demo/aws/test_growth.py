import json
import subprocess
from pathlib import Path

import pytest

import scoring

AWS_DIR = Path(__file__).parent


def javascript(source):
    return subprocess.check_output(['node', '--input-type=module', '-e', source], cwd=AWS_DIR, text=True)


def test_join_validation_uniqueness_and_cap():
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
      teams.push(first.team);
      assert.throws(() => join(teams, 'launch', 'cArT kEePeRs'), e => e.status === 409);
      assert.throws(() => join(teams, 'ready', 'HOUSE A'), e => e.status === 409);
      for (let n = 1; n < 8; n++) teams.push(join(teams, 'launching', `Team ${n}`).team);
      assert.equal(teams.length, 10);
      assert.throws(() => join(teams, 'ready', 'Ninth'), e => e.status === 409);
      assert.throws(() => join(houses(), 'migrating', 'Late'), e => e.status === 409);
    """)


def test_picks_need_token_are_private_and_freeze_at_close():
    javascript("""
      import assert from 'node:assert/strict';
      import { houses, join, pick, frozen, publicTeams } from './game.mjs';
      const teams = houses(); const {team, token} = join(teams, 'ready', 'Shop'); teams.push(team);
      const body = {id: team.id, token, release: 'B', storage: 'large', replicas: 2, cache: true};
      assert.throws(() => pick(teams, 'launch', {...body, token:'wrong'}), e => e.status === 403);
      for (const invalid of [{release:'C'}, {storage:'huge'}, {replicas:3}, {cache:'true'}]) {
        assert.throws(() => pick(teams, 'launch', {...body,...invalid}), e => e.status === 400);
      }
      teams[2] = pick(teams, 'launch', body);
      assert.equal(teams[2].kind, 'omit');
      assert.equal(publicTeams(teams, 'launch')[2].release, undefined);
      assert.equal(publicTeams(teams, 'launch',team.id,token)[2].release, 'B');
      assert.equal(publicTeams(teams, 'migrating')[2].release, 'B');
      assert.equal(frozen(teams)[2].token_hash, undefined);
      assert.throws(() => pick(teams,'migrating',body), e => e.status === 409);
    """)


def test_public_team_list_never_carries_tokens():
    javascript("""
      import assert from 'node:assert/strict';
      import { publicTeams, houses } from './game.mjs';
      assert.ok(!JSON.stringify(publicTeams(houses(), 'ready')).includes('token'));
    """)


@pytest.mark.parametrize(('kind', 'sold', 'expected'), [('preserve', 1, 900 + 11800 + 2000 - 200), ('omit', 0, 900 + 11800 - 100)])
def test_per_team_profit_includes_retention_and_modelled_prices(kind, sold, expected):
    probability = scoring.return_probability(kind, 25 if sold else None)
    maya = scoring.maya_sales(sold, probability)
    profit = scoring.total_profit(scoring.growth_profit(11800, maya, kind))
    assert profit == expected
    assert probability == (75 if sold else 5)


def test_architecture_is_the_virtual_rds_model():
    from evaluator import Rehearsal

    expected = [('small', 1, False, 12, 200, 11800), ('small', 1, True, 21, 200, 20800),
                ('large', 1, False, 21, 500, 20500), ('local', 1, False, 0, 100, -100)]
    for storage, replicas, cache, sales, cost, profit in expected:
        architecture = {'storage': storage, 'replicas': replicas, 'cache': cache}
        stage = Rehearsal.parse({'id': 'a' * 32, 'action': 'compare', 'team': 'housea', 'kind': 'preserve', 'architecture': architecture})
        result = stage.capacity()
        assert (result['verified_sales'], result['cost_cents'], result['profit_cents']) == (sales, cost, profit)
        assert stage.step == 'compare-housea'


def test_ranking_shares_equal_ranks():
    rows = [{'id': name, 'name': name, 'cumulative_cents': profit} for name, profit in [('c', 10), ('a', 20), ('b', 20)]]
    assert [(row['name'], row['rank']) for row in scoring.rank(rows)] == [('a', 1), ('b', 1), ('c', 3)]


def test_the_web_package_contains_every_file_the_web_function_loads():
    import re

    web = (AWS_DIR / 'web.mjs').read_text()
    needed = set(re.findall(r"from '\./([\w.-]+)'", web)) | set(re.findall(r"new URL\('\./([\w./-]+)'", web))
    assert {'game.mjs', 'host_auth.mjs', 'roster.mjs', 'player.html', 'host.html', 'round.js', 'round.css', 'art/cloud-surge.jpg'} <= needed
    packaging = (AWS_DIR / 'operate.py').read_text()
    missing = {name for name in needed if f"'{name}'" not in packaging and name.rsplit('/', 1)[-1] not in packaging}
    assert not missing, missing
    for name in needed:
        assert (AWS_DIR / name).is_file(), name
    assert (AWS_DIR / 'art' / 'cloud-surge.jpg').stat().st_size <= 300_000
