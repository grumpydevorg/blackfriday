import json
import time
from pathlib import Path

import pytest

import model

AWS_DIR = Path(__file__).parent
DEFAULTS = {rid: model.default_option(rid) for rid in model.ROUND_IDS}
KEPT = {'careful': {'cart_kept': True, 'source': 'real'}, 'fast': {'cart_kept': True, 'source': 'real'}}
LOST = {'careful': {'cart_kept': True, 'source': 'real'}, 'fast': {'cart_kept': False, 'source': 'real'}}


def row(pid, option=None, name=None, **extra):
    return {'id': pid, 'name': name or pid.title(), 'house': False, 'option': option, **extra}


def hand_plan(round_id, market, players, roll=99, noise=0, rolls=None):
    """A plan with chosen dice instead of random ones: rolls maps id -> roll."""
    rolls = rolls or {}
    rows = [{'bot': False, 'picks': {}, **p, 'option': p['option'] or model.default_option(round_id)} for p in players]
    return {'round': round_id, 'market': market, 'players': rows,
            'draws': {p['id']: {'roll': rolls.get(p['id'], roll), 'noise': noise} for p in rows}}


def play(picks, markets=(100, 100, 11, 100), rolls=None, evidence=LOST, noise=0, who='abc'):
    """One shop through all four rounds; picks is a list of four options. Returns the four score() outputs."""
    previous, outputs = None, []
    for index, round_id in enumerate(model.ROUND_IDS):
        rows = [row(who, picks[index])]
        rolled = {who: (rolls or {}).get(round_id, 99)}
        planned = hand_plan(round_id, markets[index], rows, rolls=rolled, noise=noise)
        previous = model.score(round_id, planned, previous, evidence)
        outputs.append(previous)
    return outputs


def mine(output, who='abc'):
    return next(r for r in output['rows'] if r['id'] == who)


def scripted(values):
    iterator = iter(values)
    return lambda n: next(iterator) % n


def test_the_model_reads_the_single_rounds_table():
    table = json.loads((AWS_DIR / 'rounds.json').read_text())
    assert model.ROUNDS == table['rounds']
    assert model.ROUND_IDS == ['launch', 'growth', 'blackfriday', 'christmas']
    assert (model.PLAYER_CAP, model.MIN_PLAYERS) == (100, 6)
    assert model.HOUSE == {'id': 'house', 'name': 'House (does nothing)'}
    assert [model.default_option(r) for r in model.ROUND_IDS] == ['small', 'stay', 'ride', 'keep']


def test_all_default_totals_with_fixed_draws():
    # Hand-checked from rounds.json (small server $315/week, 60 customers/week; markets 100%, 100%, 11x, 100%, no noise):
    #  launch    160 customers (40/wk x 4)            -> 160 fast; cost 4 x 31500 = 126000; profit 160k - 126k = 34000; 120 return
    #  growth    224 new + 120 back = 344; stay caps 60/wk -> 240 fast + 33 slow (14% band) + 71 failed;
    #            cost 126000; profit 273k - 126k = 147000; (240x75 + 33x50 + 71x20)/100 = 210 return
    #  blackfriday  2 wk x 2/wk x 11 = 44 new + 210 = 254; ride 120 fast + 16 slow + 118 failed;
    #            cost 2 x 31500 = 63000; profit 136k - 63k = 73000; 121 return
    #  christmas 53 x 3 = 159 new + 121 = 280; keep 180 fast + 25 slow + 75 failed; cost 3 x 31500 = 94500;
    #            profit 205k - 94.5k = 110500
    outputs = play(['small', 'stay', 'ride', 'keep'])
    assert [mine(o)['profit_cents'] for o in outputs] == [34000, 147000, 73000, 110500]
    assert [(mine(o)['sales'], mine(o)['slow'], mine(o)['failed']) for o in outputs] == \
        [(160, 0, 0), (240, 33, 71), (120, 16, 118), (180, 25, 75)]
    assert [mine(o)['returning'] for o in outputs] == [120, 210, 121, 162]
    assert mine(outputs[-1])['cumulative_cents'] == 364500
    assert outputs[-1]['baseline']['cum'] == 364500, 'the baseline is the all-default shop with zero noise'


def test_fast_migration_hit_loses_carts_and_a_miss_does_not():
    miss = play(['small', 'fast', 'ride', 'keep'], rolls={'growth': 99})
    hit = play(['small', 'fast', 'ride', 'keep'], rolls={'growth': 0})
    assert mine(miss[1])['hit'] is False and mine(hit[1])['hit'] is True
    assert 'lost' in mine(hit[1])['event'].lower()
    assert mine(hit[1])['evidence'] == 'real'
    assert mine(hit[1])['sales'] + mine(hit[1])['slow'] < mine(miss[1])['sales'] + mine(miss[1])['slow'], \
        'lost carts mean fewer returning customers'
    assert mine(hit[-1])['cumulative_cents'] < mine(miss[-1])['cumulative_cents']
    assert hit[1]['states']['abc']['hist_lost'] is True and miss[1]['states']['abc']['hist_lost'] is False


def test_a_fast_hit_follows_the_real_evidence():
    hit_kept = play(['small', 'fast', 'ride', 'keep'], rolls={'growth': 0}, evidence=KEPT)
    miss = play(['small', 'fast', 'ride', 'keep'], rolls={'growth': 99}, evidence=KEPT)
    assert mine(hit_kept[1])['hit'] is True
    assert mine(hit_kept[1])['profit_cents'] == mine(miss[1])['profit_cents']
    assert hit_kept[1]['states']['abc']['hist_lost'] is False


def test_recorded_evidence_marks_the_row_source():
    recorded = {'careful': {'cart_kept': True, 'source': 'recorded'}, 'fast': {'cart_kept': False, 'source': 'recorded'}}
    rows = [mine(o) for o in play(['medium', 'careful', 'ride', 'keep'], evidence=recorded)]
    assert [r['evidence'] for r in rows] == ['modelled', 'recorded', 'modelled', 'modelled']
    rows = [mine(o) for o in play(['medium', 'careful', 'ride', 'keep'], evidence=LOST)]
    assert rows[1]['evidence'] == 'real'


def test_the_real_evidence_options_are_derived_from_rounds_json():
    table = json.loads((AWS_DIR / 'rounds.json').read_text())
    real = {opt['id']: rnd['id'] for rnd in table['rounds'] for opt in rnd['options'] if opt['evidence'] == 'real'}
    assert model.REAL_OPTIONS == real == {'careful': 'growth', 'fast': 'growth'}
    assert set(model.RECORDED) == set(real)
    assert model.option_label('careful') == 'Careful migration' and model.round_title('growth') == 'Round 2 · Growth'


def test_outcome_is_lost_carts_exactly_when_a_cart_was_lost_and_otherwise_hit_or_held():
    for evidence in (KEPT, LOST, {'careful': {'cart_kept': False, 'source': 'real'}, 'fast': {'cart_kept': True, 'source': 'real'}}):
        for second in model.ROUNDS[1]['options']:
            for third in model.ROUNDS[2]['options']:
                if not model.allowed('blackfriday', third['id'], {'growth': second['id']}):
                    continue
                for roll in (0, 20, 34, 37, 38, 50, 99):
                    outputs = play(['small', second['id'], third['id'], 'keep'], rolls={'growth': roll, 'blackfriday': roll},
                                   evidence=evidence)
                    for round_id, output in zip(model.ROUND_IDS, outputs):
                        r = mine(output)
                        lost_carts = (round_id == 'growth' and (
                            second['id'] == 'careful' and not evidence['careful']['cart_kept']
                            or second['id'] == 'fast' and roll < 38 and not evidence['fast']['cart_kept']))
                        assert r['hit'] == (roll < r['risk'])
                        assert (r['outcome'] == 'lost_carts') == lost_carts, (round_id, r)
                        if not lost_carts:
                            assert r['outcome'] == ('hit' if r['hit'] else 'held'), (round_id, r)
                        if round_id == 'growth':
                            assert output['states']['abc']['hist_lost'] == lost_carts


def test_a_careful_migration_that_lost_the_carts_is_lost_carts_without_a_dice_hit():
    r = mine(play(['small', 'careful', 'ride', 'keep'], evidence=LOST | {'careful': {'cart_kept': False, 'source': 'real'}})[1])
    assert (r['hit'], r['outcome']) == (False, 'lost_carts')
    kept = mine(play(['small', 'fast', 'ride', 'keep'], rolls={'growth': 0}, evidence=KEPT)[1])
    assert (kept['hit'], kept['outcome']) == (True, 'hit'), 'the dice hit but the real check kept the carts'


@pytest.mark.parametrize('bad', [{'source': 'real'}, {'source': 'real', 'cart_kept': None},
                                 {'source': 'real', 'cart_kept': 'yes'}, {'source': 'real', 'cart_kept': 1}])
def test_real_evidence_without_a_boolean_cart_kept_is_rejected(bad):
    for option in ('careful', 'fast'):
        planned = hand_plan('growth', 100, [row('abc', 'careful')])
        with pytest.raises(ValueError, match='cart_kept'):
            model.score('growth', planned, None, {**KEPT, option: bad})


def test_recorded_or_missing_evidence_does_not_need_cart_kept_and_falls_back_to_the_recorded_result():
    planned = hand_plan('growth', 100, [row('abc', 'careful')])
    assert model.score('growth', planned, None, {'careful': {'source': 'recorded'}})['rows']
    assert model.score('growth', planned, None, {})['rows']


def test_autoscale_needs_a_shared_database():
    assert not model.allowed('blackfriday', 'autoscale', {'growth': 'stay'})
    assert not model.allowed('blackfriday', 'autoscale', {})
    assert model.allowed('blackfriday', 'autoscale', {'growth': 'careful'})
    assert model.allowed('blackfriday', 'autoscale', {'growth': 'fast'})
    assert model.allowed('blackfriday', 'ride', {'growth': 'stay'})
    assert not model.allowed('blackfriday', 'nope', {}) and not model.allowed('nope', 'ride', {})
    assert not model.allowed('growth', ['stay'], {})
    # A refused pick that reaches the model is played as the default, never as autoscale.
    previous = play(['small', 'stay', 'ride', 'keep'])[1]
    planned = model.plan('blackfriday', [row('abc', 'autoscale')], previous, scripted([0] * 99))
    assert next(p for p in planned['players'] if p['id'] == 'abc')['option'] == 'ride'
    # The same row with a shared database is allowed.
    previous = play(['small', 'careful', 'ride', 'keep'])[1]
    planned = model.plan('blackfriday', [row('abc', 'autoscale')], previous, scripted([0] * 99))
    assert next(p for p in planned['players'] if p['id'] == 'abc')['option'] == 'autoscale'


def test_bots_never_draw_autoscale_without_the_requirement():
    previous = {'states': {bid: {'picks': {'launch': 'small', 'growth': 'stay'}} for bid in
                           ('botlambo', 'botturbo', 'botkevin', 'botmonolith', 'botspike')},
                'bots': [{'id': bid, 'name': bid} for bid in
                         ('botlambo', 'botturbo', 'botkevin', 'botmonolith', 'botspike')]}
    for draw in range(3):
        planned = model.plan('blackfriday', [], previous, scripted([5] * 3 + [draw] * 20))
        bots = [p for p in planned['players'] if p['bot']]
        assert len(bots) == 5 and all(p['option'] in ('ride', 'queue') for p in bots)


def test_queue_nobody_fails_and_a_hit_refunds_double_charges():
    clean = play(['large', 'careful', 'queue', 'keep'], rolls={'blackfriday': 99})
    hit = play(['large', 'careful', 'queue', 'keep'], rolls={'blackfriday': 0})
    ride = play(['large', 'careful', 'ride', 'keep'])
    assert mine(clean[2])['failed'] == 0 and mine(clean[2])['slow'] > 0
    assert mine(ride[2])['failed'] > 0
    assert mine(clean[2])['refund_cents'] == 0 and not mine(clean[2])['hit']
    assert mine(hit[2])['hit'] is True and mine(hit[2])['refund_cents'] > 0
    assert mine(hit[2])['failed'] == 0
    assert mine(hit[2])['profit_cents'] == mine(clean[2])['profit_cents'] - mine(hit[2])['refund_cents']
    assert mine(hit[2])['returning'] < mine(clean[2])['returning'], 'double-charged customers return at 5%'


def test_autoscale_hit_serves_the_first_part_at_old_capacity():
    clean = play(['small', 'careful', 'autoscale', 'keep'], rolls={'blackfriday': 99})
    hit = play(['small', 'careful', 'autoscale', 'keep'], rolls={'blackfriday': 0})
    assert mine(clean[2])['failed'] == 0 and mine(clean[2])['slow'] == 0
    assert mine(hit[2])['failed'] > 0
    assert mine(hit[2])['cost_cents'] < mine(clean[2])['cost_cents'], 'the late servers were live for less time'
    assert mine(clean[2])['cost_cents'] > mine(play(['small', 'careful', 'ride', 'keep'])[2])['cost_cents']


def test_commit_is_paid_even_when_demand_is_low():
    low = play(['small', 'careful', 'ride', 'commit'], markets=(100, 100, 11, 70), noise=-10)
    high = play(['small', 'careful', 'ride', 'commit'], markets=(100, 100, 11, 120), noise=10)
    assert mine(low[3])['cost_cents'] == mine(high[3])['cost_cents'] > 0
    assert mine(low[3])['sales'] + mine(low[3])['slow'] < mine(high[3])['sales'] + mine(high[3])['slow']
    keep = play(['small', 'careful', 'ride', 'keep'], markets=(100, 100, 11, 70), noise=-10)
    assert mine(low[3])['cost_cents'] != mine(keep[3])['cost_cents']


def test_stay_keeps_the_disk_ceiling_and_a_shared_database_lifts_it():
    stay = play(['large', 'stay', 'ride', 'keep'])
    careful = play(['large', 'careful', 'ride', 'keep'])
    assert stay[1]['states']['abc']['cap'] < careful[1]['states']['abc']['cap']
    assert stay[1]['states']['abc']['data'] == 'disk' and careful[1]['states']['abc']['data'] == 'shared'


def test_validate_players_is_total():
    assert model.validate_players([]) == []
    for bad in (None, {}, 'x', 3, ('a',)):
        with pytest.raises(ValueError):
            model.validate_players(bad)
    rows = model.validate_players([
        None, 5, 'x', [], {},
        {'id': 3, 'name': 'No'}, {'id': 'AB', 'name': 'No'}, {'id': 'has space', 'name': 'No'}, {'id': 'x' * 17},
        {'id': 'good1', 'name': 'Good', 'house': False, 'option': 'medium', 'picks': {'launch': 'medium'}},
        {'id': 'good1', 'name': 'Dupe', 'option': 'large'},
        {'id': 'noname', 'name': '', 'option': 'medium'},
        {'id': 'zalgo1', 'name': 'a\u0301\u0302\u0303b', 'option': 7},
        {'id': 'ctrl12', 'name': 'bad\nname', 'option': 'nonsense'},
        {'id': 'long12', 'name': 'x' * 21, 'option': ['small'], 'picks': 'junk'},
        {'id': 'blank1', 'name': '\u3164\u3164', 'house': 'yes', 'bot': 1, 'picks': {'launch': 9, 'growth': 'zzz'}},
    ])
    assert [r['id'] for r in rows] == ['good1', 'noname', 'zalgo1', 'ctrl12', 'long12', 'blank1']
    by_id = {r['id']: r for r in rows}
    assert by_id['good1']['name'] == 'Good' and by_id['good1']['option'] == 'medium'
    assert by_id['noname']['name'] == 'Player name'
    assert by_id['zalgo1']['name'] == 'Player lgo1' and by_id['ctrl12']['name'] == 'Player rl12'
    assert by_id['long12']['name'] == 'Player ng12' and by_id['blank1']['name'] == 'Player ank1'
    assert by_id['zalgo1']['option'] is None and by_id['ctrl12']['option'] is None
    assert by_id['blank1']['house'] is False and by_id['blank1']['bot'] is False and by_id['blank1']['picks'] == {}
    json.dumps(rows)


def test_validate_players_checks_the_option_against_a_given_round():
    rows = model.validate_players([
        row('aaa', 'autoscale', picks={'growth': 'stay'}), row('bbb', 'autoscale', picks={'growth': 'fast'}),
        row('ccc', 'bogus'), row('ddd'),
    ], 'blackfriday')
    assert [r['option'] for r in rows] == ['ride', 'autoscale', 'ride', 'ride']


def test_bots_fill_to_six_and_keep_their_ids_across_rounds():
    random_source = scripted(range(1000))
    planned = model.plan('launch', [row('t1aaaaaa', 'medium', house=False)], None, random_source)
    ids = [p['id'] for p in planned['players']]
    assert ids == ['t1aaaaaa', 'house', 'botlambo', 'botturbo', 'botkevin', 'botmonolith']
    assert len(planned['players']) == model.MIN_PLAYERS
    house = planned['players'][1]
    assert house['house'] is True and house['bot'] is False and house['option'] == 'small'
    assert all(p['bot'] and p['house'] for p in planned['players'][2:])
    assert set(planned['draws']) == set(ids)
    assert all(0 <= d['roll'] < 100 and -10 <= d['noise'] <= 10 for d in planned['draws'].values())
    low, high = model.ROUNDS[0]['market']['min'], model.ROUNDS[0]['market']['max']
    assert low <= planned['market'] <= high
    scored = model.score('launch', planned, None, LOST)
    assert [b['id'] for b in scored['bots']] == ids[2:]
    previous = scored
    for round_id in model.ROUND_IDS[1:]:
        planned = model.plan(round_id, [row('t1aaaaaa')], previous, random_source)
        assert [p['id'] for p in planned['players']] == ['t1aaaaaa'] + ids[1:] or \
            [p['id'] for p in planned['players']] == ['t1aaaaaa', 'house'] + ids[2:]
        previous = model.score(round_id, planned, previous, LOST)
        assert [b['id'] for b in previous['bots']] == ids[2:]
        assert len(previous['rows']) == 6


def test_a_crowd_needs_no_bots_and_the_house_still_plays():
    crowd = [row(f't{n:07}') for n in range(10)]
    planned = model.plan('launch', crowd, None, scripted([7] * 500))
    assert [p['id'] for p in planned['players']][-1] == 'house'
    assert len(planned['players']) == 11 and not any(p['bot'] for p in planned['players'])
    assert model.score('launch', planned, None, LOST)['bots'] == []


def test_house_always_plays_the_default_and_bots_are_random():
    previous = play(['small', 'careful', 'ride', 'keep'], who='house')[1]
    planned = model.plan('blackfriday', [row('house', 'queue', house=True)], previous, scripted([1] * 99))
    assert planned['players'][0]['option'] == 'ride'
    seen = set()
    for draw in range(3):
        seen.add(model.plan('launch', [], None, scripted([0, draw, draw, draw, draw, draw] + [0] * 40))
                 ['players'][-1]['option'])
    assert seen == {'small', 'medium', 'large'}


def test_determinism_and_json_round_trip():
    def run():
        previous, out = None, []
        for round_id in model.ROUND_IDS:
            players = [row(f't{n:07}', ['small', 'medium', 'large'][n % 3] if round_id == 'launch' else None)
                       for n in range(12)]
            planned = model.plan(round_id, players, previous, scripted(range(7, 7 + 2000)))
            planned = json.loads(json.dumps(planned))
            previous = json.loads(json.dumps(model.score(round_id, planned, previous, LOST)))
            out.append((planned, previous))
        return out

    assert run() == run()
    json.dumps(run())


def test_a_late_joiner_starts_at_the_baseline():
    outputs = play(['small', 'stay', 'ride', 'keep'])
    previous = outputs[1]
    planned = hand_plan('blackfriday', 11, [row('abc', 'ride'), row('late1', 'ride')])
    both = model.score('blackfriday', planned, previous, LOST)
    old, late = mine(both, 'abc'), mine(both, 'late1')
    assert (old['sales'], old['slow'], old['failed'], old['cost_cents']) == \
        (late['sales'], late['slow'], late['failed'], late['cost_cents'])
    assert late['cumulative_cents'] == old['cumulative_cents'] == mine(outputs[1])['cumulative_cents'] + old['profit_cents']
    # A late joiner who joins in the growth round starts from the launch baseline.
    planned = hand_plan('growth', 100, [row('abc', 'stay'), row('late1', 'stay')])
    grown = model.score('growth', planned, outputs[0], LOST)
    assert mine(grown, 'late1')['cumulative_cents'] == mine(grown, 'abc')['cumulative_cents']
    assert 'late1' in grown['states']


def test_removed_players_stop_appearing():
    first = model.score('launch', hand_plan('launch', 100, [row('abc'), row('def')]), None, LOST)
    second = model.score('growth', hand_plan('growth', 100, [row('abc')]), first, LOST)
    assert [r['id'] for r in second['rows']] == ['abc'] and list(second['states']) == ['abc']


def test_ties_share_a_rank_then_names_sort():
    planned = hand_plan('launch', 100, [row('ccc', name='zed'), row('aaa', name='Amy'), row('bbb', name='amy'),
                                         row('ddd', name='Bob', option='large')])
    rows = model.score('launch', planned, None, LOST)['rows']
    cums = [r['cumulative_cents'] for r in rows]
    assert cums == sorted(cums, reverse=True)
    tied = [r for r in rows if r['option'] == 'small']
    assert len({r['cumulative_cents'] for r in tied}) == 1
    assert len({r['rank'] for r in tied}) == 1
    assert [r['id'] for r in tied] == ['aaa', 'bbb', 'ccc'], 'name casefold, then id'
    other = next(r for r in rows if r['id'] == 'ddd')
    assert other['rank'] != tied[0]['rank']
    ranks = [r['rank'] for r in rows]
    assert ranks == sorted(ranks)
    if other['rank'] > tied[0]['rank']:
        assert other['rank'] == 4


def test_summary_shape():
    planned = hand_plan('launch', 100, [row('aaa', 'small'), row('bbb', 'small'), row('ccc', 'large')])
    summary = model.score('launch', planned, None, LOST)['summary']
    assert summary['market'] == 100 and set(summary['options']) == {'small', 'medium', 'large'}
    assert summary['options']['small']['players'] == 2 and summary['options']['medium']['players'] == 0
    profits = {r['id']: r['profit_cents'] for r in model.score('launch', planned, None, LOST)['rows']}
    assert summary['options']['small']['best_profit_cents'] == max(profits['aaa'], profits['bbb'])
    assert summary['options']['large']['mean_profit_cents'] == profits['ccc']


def test_every_row_has_the_contract_shape():
    keys = ['id', 'name', 'house', 'bot', 'rank', 'option', 'roll', 'risk', 'hit', 'outcome', 'event', 'sales', 'slow',
            'failed', 'cost_cents', 'refund_cents', 'profit_cents', 'cumulative_cents', 'returning', 'evidence']
    for output in play(['medium', 'fast', 'queue', 'commit'], rolls={'growth': 0, 'blackfriday': 0}):
        for r in output['rows']:
            assert list(r) == keys
            assert r['event'] and isinstance(r['hit'], bool) and r['evidence'] in ('real', 'recorded', 'modelled')
            assert r['outcome'] in ('held', 'hit', 'lost_carts')


def test_a_hundred_players_over_four_rounds_is_fast_and_rows_are_small():
    picks = [list(opts) for opts in zip(*[[o['id'] for o in rnd['options']] for rnd in model.ROUNDS])]
    previous, worst = None, 0
    start = time.perf_counter()
    for round_id in model.ROUND_IDS:
        players = [row(f't{n:012x}', picks[n % 3][model.ROUND_IDS.index(round_id)], name=f'Player {n} ' + 'x' * 10)
                   for n in range(model.PLAYER_CAP)]
        planned = model.plan(round_id, players, previous, scripted(range(3, 5000)))
        previous = model.score(round_id, planned, previous, LOST)
        for r in previous['rows']:
            worst = max(worst, len(json.dumps(r, ensure_ascii=False, separators=(',', ':')).encode()))
    assert time.perf_counter() - start < 0.1
    assert len(previous['rows']) == model.PLAYER_CAP + 1 and not previous['bots']
    # The contract's '< 300 bytes' cannot hold with the contract's own 19 keys, a 13-char web id and a 20-char name:
    # the bare keys are ~230 bytes. The real bound is the 400 KB DynamoDB item: 101 rows x ~340 bytes is ~34 KB.
    assert worst < 360, worst


def test_balance_passes():
    ok, report = model.balance(1500)
    assert ok, report
