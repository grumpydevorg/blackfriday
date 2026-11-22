"""Run the same synthetic preload, unlock and recovery traffic on each plan."""

import json
from contextlib import ExitStack, closing
from http.client import HTTPConnection
from itertools import chain, cycle
from random import Random
from time import monotonic

from arena import CUSTOMER, MARGIN, Reply
from virtual_rds import (
    COLD,
    HOT,
    PLANS,
    RETRY,
    SECOND,
    SHOP_COST,
    TIERS,
    Referee,
    Shop,
    Store,
    cart_key,
    order_key,
    seed_records,
    serving,
)

SCENARIO = 'release-v1'
_RELEASE_PLANS = ('small', 'replicas', 'large', 'cached')
_UNLOCK = tuple(tuple(f'unlock-{wave:02}-{n:02}' for n in range(20)) for wave in range(12))
_RECOVERY = tuple(tuple(f'recovery-{wave:02}-{n:02}' for n in range(12)) for wave in range(3))


def _request(shop: Shop, arrival: int, key: str, *, checkout: bool = False) -> Reply:
    shop.arrival = arrival
    headers = {'X-Customer-ID': CUSTOMER}
    start = monotonic()
    # Connect directly to the ephemeral loopback server; never consult HTTP proxy settings.
    with closing(HTTPConnection('127.0.0.1', shop.server_port, timeout=5)) as connection:
        if checkout:
            headers.update({'Content-Type': 'application/json', 'Idempotency-Key': key})
            connection.request('POST', '/checkout', json.dumps({'cart_id': cart_key(key)}), headers)
        else:
            connection.request('GET', f'/orders/{key}', headers=headers)
        with connection.getresponse() as response:
            data = json.load(response)
            return Reply(response.status, data, round((monotonic() - start) * 1000))


def evaluate(plan: str, seed: int) -> dict:
    """Score one release window; all workload and verification HTTP calls are counted."""
    if plan not in _RELEASE_PLANS:
        raise ValueError(f'Unknown release plan: {plan}')
    config = PLANS[plan]
    rate, tier_cost = TIERS[config.storage]
    intentions = (*chain.from_iterable((*_UNLOCK, *_RECOVERY)), RETRY)
    fault_key = order_key(RETRY)
    store = Store(rate, seed_records(intentions), lose_reply_for=fault_key)
    referee = Referee(intentions)
    rng = Random(seed)
    phases = []
    verified: set[str] = set()
    arrival = http_requests = 0
    previous = (0, 0, 0, 0)

    def call(shop: Shop, at: int, key: str, *, checkout: bool = False) -> Reply:
        nonlocal http_requests
        reply = _request(shop, at, key, checkout=checkout)
        http_requests += 1
        return reply

    def finish_phase(name: str) -> None:
        nonlocal previous
        totals = (http_requests, store.units, store.rejected, len(verified))
        calls, units, rejected, sales = (now - then for now, then in zip(totals, previous))
        phases.append({
            'name': name, 'http_requests': calls, 'storage_units': units,
            'storage_rejections': rejected, 'verified_sales': sales,
        })
        previous = totals

    def burst(shops: list[Shop], reads: int, checkouts: tuple[str, ...] = ()) -> None:
        nonlocal arrival
        history = [*COLD, *([HOT] * (reads - len(COLD)))]
        purchases = list(checkouts)
        rng.shuffle(history)
        rng.shuffle(purchases)
        fleet = cycle(shops)
        # Keep four reads per checkout, but jitter its slot within each group.
        # The same seed gives each plan the same slots and customer identities.
        buy_at = {5 * i + rng.randrange(5): intention for i, intention in enumerate(purchases)}
        history_keys = iter(history)
        for i in range(reads + len(purchases)):
            intention = buy_at.get(i)
            if intention is None:
                reply = call(next(fleet), arrival, next(history_keys))
            else:
                reply = call(next(fleet), arrival, intention, checkout=True)
            if reply.status not in (200, 429):
                raise RuntimeError(f'Unexpected burst response: {reply.status} {reply.data}')

        # Ask for every offered checkout, including rejections. The probes have
        # their own model second so they cannot compete with that burst's buyers.
        for intention in purchases:
            reply = call(next(fleet), arrival + SECOND, order_key(intention))
            if intention not in referee.charged:
                reply.expect(404)
            elif referee.verifies(intention, reply, store.records):
                verified.add(intention)
            else:
                raise RuntimeError(f'Charged order did not verify: {intention}')
        arrival += 2 * SECOND

    with ExitStack() as fleet:
        survivors = [fleet.enter_context(serving(store, referee, cached=config.cached))
                     for _ in range(config.replicas - 1)]
        with serving(store, referee, cached=config.cached) as original:
            shops = [original, *survivors]
            for _ in range(4):
                burst(shops, 72)
            finish_phase('preload')

            for window in _UNLOCK:
                burst(shops, 80, window)
            first = call(original, arrival, RETRY, checkout=True).expect(504)
            finish_phase('unlock')

        arrival += SECOND
        with serving(store, referee, cached=config.cached) as replacement:
            shops = [replacement, *survivors]
            persisted = call(replacement, arrival, fault_key).expect(200)
            retry_charged_before_replay = RETRY in referee.charged
            before_attempts = referee.payment_attempts
            replay = call(replacement, arrival, RETRY, checkout=True).expect(200)
            duplicate = call(replacement, arrival, RETRY, checkout=True).expect(200)
            retry_attempts = referee.payment_attempts - before_attempts
            if (retry_charged_before_replay or retry_attempts != 2
                    or persisted.data != replay.data or persisted.data != duplicate.data
                    or not referee.verifies(RETRY, persisted, store.records)):
                raise RuntimeError('Checkout replay did not recover the same order with one charge')
            verified.add(RETRY)
            arrival += SECOND
            for window in _RECOVERY:
                burst(shops, 48, window)
            finish_phase('recovery')

    cost = SHOP_COST * config.replicas + tier_cost  # One priced release window, not an AWS price.
    return {
        'http_requests': http_requests, 'verified_sales': len(verified), 'profit_cents': MARGIN * len(verified) - cost,
        'phases': phases, 'modeled_cost_cents': cost,
        'checkout_statuses': [first.status, replay.status, duplicate.status],
        'replacement_order_status': persisted.status, 'retry_charged_before_replay': retry_charged_before_replay,
        'retry_payment_attempts': retry_attempts, 'charges': len(referee.charged),
        'payment_attempts': referee.payment_attempts, 'unverified_charges': len(referee.charged - verified),
    }
