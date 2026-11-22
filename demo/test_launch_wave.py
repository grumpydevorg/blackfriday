import pytest

from launch_wave import SCENARIO, evaluate


@pytest.fixture(scope='module')
def results():
    return {name: evaluate(name, 42) for name in ('small', 'replicas', 'large', 'cached')}


def test_release_scores_capacity_and_cache_on_the_same_workload(results):
    assert SCENARIO == 'release-v1'
    for result in results.values():
        assert result['http_requests'] == 1948
        assert [(p['name'], p['http_requests']) for p in result['phases']] == [
            ('preload', 288), ('unlock', 1441), ('recovery', 219),
        ]
        assert sum(p['verified_sales'] for p in result['phases']) == result['verified_sales']
        preload, _, recovery = result['phases']
        assert preload['verified_sales'] == 0
        assert recovery['verified_sales'] == 37
        assert preload['storage_rejections'] == recovery['storage_rejections'] == 0

    small, replicas, large, cached = (results[name] for name in ('small', 'replicas', 'large', 'cached'))
    small_unlock, replica_unlock, large_unlock, cache_unlock = (
        row['phases'][1] for row in (small, replicas, large, cached)
    )
    assert small_unlock == replica_unlock
    assert small_unlock['storage_rejections'] > 0
    assert 0 < small_unlock['verified_sales'] < 240
    assert large_unlock['verified_sales'] == cache_unlock['verified_sales'] == 240
    assert large_unlock['storage_rejections'] == cache_unlock['storage_rejections'] == 0
    assert cache_unlock['storage_units'] < large_unlock['storage_units']
    assert cached['modeled_cost_cents'] < large['modeled_cost_cents']
    assert cached['profit_cents'] > large['profit_cents'] > small['profit_cents'] > replicas['profit_cents']


def test_release_recovers_a_lost_reply_after_replacement_without_a_duplicate_charge(results):
    for result in results.values():
        assert result['checkout_statuses'] == [504, 200, 200]
        assert result['replacement_order_status'] == 200
        assert result['retry_charged_before_replay'] is False
        assert result['retry_payment_attempts'] == 2
        assert result['charges'] == result['verified_sales']
        assert result['payment_attempts'] == result['charges'] + 1
        assert result['unverified_charges'] == 0


def test_release_scores_repeat_for_the_same_seed(results):
    assert evaluate('small', 42) == results['small']
    with pytest.raises(ValueError, match='Unknown release plan'):
        evaluate('local', 42)
