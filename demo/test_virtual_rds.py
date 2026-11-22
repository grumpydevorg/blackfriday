import json
import subprocess
import sys
from pathlib import Path

import pytest

from virtual_rds import SECOND, CapacityExceeded, CommitReplyLost, Store

SCRIPT = Path(__file__).with_name('virtual_rds.py')


def play(*args: str) -> dict | list:
    return json.loads(subprocess.check_output([sys.executable, str(SCRIPT), *args], timeout=15))


def test_real_shop_requests_reward_relief_at_the_storage_bottleneck(tmp_path):
    results = {row['plan']: row for row in play('compare')}
    baseline, replicas, larger, cached = (results[name] for name in ('small', 'replicas', 'large', 'cached'))
    assert baseline['wave_storage_units'] == replicas['wave_storage_units'] == larger['wave_storage_units'] == 120
    assert cached['wave_storage_units'] == 64
    assert 0 < baseline['wave_verified_sales'] == replicas['wave_verified_sales'] < 20
    assert baseline['wave_storage_rejections'] == replicas['wave_storage_rejections'] > 0
    assert larger['wave_verified_sales'] == cached['wave_verified_sales'] == 20
    assert cached['profit_cents'] > larger['profit_cents'] > baseline['profit_cents'] > replicas['profit_cents']
    for shared in (baseline, replicas, larger, cached):
        assert shared['checkout_statuses'] == [504, 200, 200]
        assert shared['replacement_order_status'] == 200
        assert shared['payment_attempts'] == shared['charges'] + 1
        assert shared['charges'] == shared['verified_sales'] == shared['wave_verified_sales'] + 1
    assert results['local']['replacement_order_status'] == 404
    assert results['local']['verified_sales'] == 0

    checkpoint = tmp_path / 'checkpoint.json'
    play('run', '--storage', 'small', '--cache', '--checkpoint', str(checkpoint))
    restored = play('resume', str(checkpoint))
    assert restored['status'] == 200
    assert restored['order']['id'] == 'order-retry-mug'
    assert restored['order']['amount_cents'] == 2500


def test_store_checks_all_conditions_and_never_applies_a_rejected_write():
    store = Store(80, {'cart': {'sku': 'mug'}}, lose_reply_for='order')
    order = {'amount_cents': 2500}
    assert not store.conditional_commit(0, {'cart': None}, {'order': order, 'cart': {}})
    assert store.read('order', 0) is None
    assert store.read('cart', 0) == {'sku': 'mug'}
    with pytest.raises(CommitReplyLost):
        store.conditional_commit(0, {'order': None}, {'order': order})
    assert store.read('order', 0) == order

    for _ in range(73):
        store.read('cart', 0)
    with pytest.raises(CapacityExceeded):
        store.conditional_commit(0, {'order': order}, {'order': {}})
    assert store.read('order', SECOND) == order
