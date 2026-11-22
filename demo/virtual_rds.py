"""Score a trusted local shop over HTTP against a modeled, in-memory database."""

import argparse
import json
import logging
import random
import sys
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from http.server import HTTPServer
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter_ns

from arena import CUSTOMER, MARGIN, PRICE, Handler, Reply, request

LOGGER = logging.getLogger(__name__)
SECOND = 1_000_000
# Units/second and cents per modeled round, not observed throughput or AWS prices.
TIERS = {'local': (160, 0), 'small': (80, 100), 'large': (160, 400)}
SHOP_COST = 100
HOT = 'history-hot'
COLD = tuple(f'history-{i:02}' for i in range(24))
WAVE_ORDERS = tuple(f'wave-{i:02}' for i in range(20))
RETRY = 'retry-mug'
INTENTIONS = (*WAVE_ORDERS, RETRY)
Record = dict[str, str | int]


class CapacityExceeded(Exception):
    """This operation could not finish within the modeled queue deadline."""


class CommitReplyLost(Exception):
    """The transaction committed, but its caller did not receive the reply."""


class Store:
    def __init__(self, rate: int, records: dict[str, Record], *, lose_reply_for: str | None = None) -> None:
        self.rate = rate
        self.records = records
        self.lose_reply_for = lose_reply_for
        self.lock = Lock()
        self.next_free = 0
        self.units = self.rejected = self.peak_delay = 0

    def admit(self, arrival: int, units: int) -> None:
        self.units += units
        finish = max(arrival, self.next_free) + units * SECOND // self.rate
        delay = finish - arrival
        if delay > SECOND:
            self.rejected += 1
            raise CapacityExceeded
        self.next_free = finish
        self.peak_delay = max(self.peak_delay, delay)

    def read(self, key: str, arrival: int) -> Record | None:
        with self.lock:
            self.admit(arrival, 1)
            return self.records.get(key)

    def conditional_commit(
        self, arrival: int, checks: dict[str, Record | None], writes: dict[str, Record],
    ) -> bool:
        with self.lock:
            self.admit(arrival, 2)
            if any(self.records.get(key) != expected for key, expected in checks.items()):
                return False
            self.records.update(writes)
            if self.lose_reply_for is not None and self.lose_reply_for in writes:
                self.lose_reply_for = None
                raise CommitReplyLost
            return True


def cart_key(intention: str) -> str:
    return f'cart-{intention}'


def order_key(intention: str) -> str:
    return f'order-{intention}'


def seed_records(intentions: Iterable[str] = INTENTIONS) -> dict[str, Record]:
    return {
        **{key: {'id': key, 'customer_id': CUSTOMER, 'amount_cents': PRICE} for key in (HOT, *COLD)},
        **{cart_key(tag): {'customer_id': CUSTOMER, 'sku': 'cloud-mug'} for tag in intentions},
    }


class Referee:
    def __init__(self, intentions: Iterable[str] = INTENTIONS) -> None:
        self.intentions = frozenset(intentions)
        self.charged: set[str] = set()
        self.payment_attempts = 0
        self.lock = Lock()

    def charge(self, intention: str, cart: str, customer: str, amount: int) -> bool:
        if intention not in self.intentions or (cart, customer, amount) != (cart_key(intention), CUSTOMER, PRICE):
            return False
        with self.lock:
            self.payment_attempts += 1
            self.charged.add(intention)
        return True

    def verifies(self, intention: str, reply: Reply, records: dict[str, Record]) -> bool:
        key = order_key(intention)
        expected = {'id': key, 'cart_id': cart_key(intention), 'customer_id': CUSTOMER, 'amount_cents': PRICE}
        return intention in self.charged and reply.status == 200 and reply.data == records.get(key) == expected


class Shop(HTTPServer):
    def __init__(self, store: Store, referee: Referee, *, cached: bool) -> None:
        super().__init__(('127.0.0.1', 0), ShopHandler)
        self.base = f'http://127.0.0.1:{self.server_port}'
        self.store = store
        self.referee = referee
        self.cache: dict[str, Record] | None = {} if cached else None
        self.arrival = 0  # Advanced by the trusted workload driver, never by an HTTP caller.


class ShopHandler(Handler):
    server: Shop

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.debug(format, *args)

    def do_GET(self) -> None:
        if not self.path.startswith('/orders/'):
            self.send_json(404, {'error': 'No such route'})
            return
        key = self.path.removeprefix('/orders/')
        shop = self.server
        cache = shop.cache
        record = cache.get(key) if cache is not None else None
        if record is None:
            try:
                record = shop.store.read(key, shop.arrival)
            except CapacityExceeded:
                self.send_json(429, {'error': 'Modeled storage deadline exceeded'})
                return
            if record is not None and cache is not None and len(cache) < 32:
                cache[key] = record  # Orders in this scenario are immutable.
        if record is None or record['customer_id'] != self.headers.get('X-Customer-ID'):
            self.send_json(404, {'error': 'Order not found'})
        else:
            self.send_json(200, record)

    def do_POST(self) -> None:
        if self.path != '/checkout':
            self.send_json(404, {'error': 'No such route'})
            return
        shop = self.server
        intention = self.headers.get('Idempotency-Key', '')
        customer = self.headers.get('X-Customer-ID', '')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1024:
                raise ValueError('Invalid checkout size')
            cart = json.loads(self.rfile.read(size))['cart_id']
            if intention not in shop.referee.intentions or cart != cart_key(intention) or customer != CUSTOMER:
                raise ValueError('Invalid checkout')
        except (ValueError, TypeError, KeyError):
            self.send_json(400, {'error': 'Invalid checkout'})
            return
        key = order_key(intention)
        order = {'id': key, 'cart_id': cart, 'customer_id': customer, 'amount_cents': PRICE}
        try:
            created = shop.store.conditional_commit(
                shop.arrival, {key: None, cart: {'customer_id': customer, 'sku': 'cloud-mug'}}, {key: order},
            )
            if not created and shop.store.read(key, shop.arrival) != order:
                self.send_json(409, {'error': 'Cart missing or order differs'})
                return
        except CapacityExceeded:
            self.send_json(429, {'error': 'Modeled storage deadline exceeded'})
            return
        except CommitReplyLost:
            self.send_json(504, {'error': 'Commit reply lost; retry with the same key'})
            return
        if shop.referee.charge(intention, cart, customer, PRICE):
            self.send_json(200, order)
        else:
            self.send_json(409, {'error': 'Payment intention not authorized'})


@contextmanager
def serving(store: Store, referee: Referee, *, cached: bool) -> Iterator[Shop]:
    with Shop(store, referee, cached=cached) as shop:
        thread = Thread(target=shop.serve_forever, kwargs={'poll_interval': 0.02})
        thread.start()
        try:
            yield shop
        finally:
            shop.shutdown()
            thread.join()


@dataclass(frozen=True)
class Plan:
    storage: str
    replicas: int = 1
    cached: bool = False


PLANS = {
    'local': Plan('local'), 'small': Plan('small'), 'replicas': Plan('small', 2),
    'large': Plan('large'), 'cached': Plan('small', cached=True),
}


def run_round(plan: Plan, seed: int) -> tuple[dict, dict[str, Record]]:
    rate, tier_cost = TIERS[plan.storage]
    referee = Referee()
    fault_key = order_key(RETRY)
    shared = None if plan.storage == 'local' else Store(rate, seed_records(), lose_reply_for=fault_key)
    reads = [(False, key) for key in (*COLD, *([HOT] * 56))]
    wave = reads + [(True, intention) for intention in WAVE_ORDERS]
    random.Random(seed).shuffle(wave)
    http_us = []

    def checkout(shop: Shop, intention: str) -> Reply:
        return request(shop.base, '/checkout', {'cart_id': cart_key(intention)}, intention)

    with ExitStack() as fleet:
        shops = []
        for _ in range(plan.replicas):
            store = shared if shared is not None else Store(rate, seed_records(), lose_reply_for=fault_key)
            shop = fleet.enter_context(serving(store, referee, cached=plan.cached))
            request(shop.base, f'/orders/{HOT}').expect(200)
            shops.append(shop)
        stores = {shop.store for shop in shops}
        before = sum(store.units for store in stores)
        for i, (buy, key) in enumerate(wave):
            shop = shops[i % len(shops)]
            shop.arrival = SECOND
            start = perf_counter_ns()
            reply = checkout(shop, key) if buy else request(shop.base, f'/orders/{key}')
            http_us.append((perf_counter_ns() - start) // 1000)
            if reply.status not in {200, 429}:
                raise RuntimeError(f'Unexpected wave response: {reply.status} {reply.data}')
        units = sum(store.units for store in stores) - before
        rejections = sum(store.rejected for store in stores)
        peak_delay = max(store.peak_delay for store in stores)
        shops[0].arrival = 2 * SECOND
        first = checkout(shops[0], RETRY)
        first.expect(504)

    replacement_store = shared if shared is not None else Store(rate, {})
    with serving(replacement_store, referee, cached=plan.cached) as replacement:
        replacement.arrival = 3 * SECOND
        before_retry = request(replacement.base, f'/orders/{fault_key}')
        second = checkout(replacement, RETRY)
        duplicate = checkout(replacement, RETRY)
        verified = {
            tag for tag in INTENTIONS if referee.verifies(
                tag, request(replacement.base, f'/orders/{order_key(tag)}'), replacement_store.records,
            )
        }

    cost = SHOP_COST * plan.replicas + tier_cost
    return {
        'storage': plan.storage, 'replicas': plan.replicas, 'cache': plan.cached,
        'wave_storage_units': units, 'wave_storage_rejections': rejections,
        'wave_max_modeled_storage_delay_ms': peak_delay / 1000,
        'wave_p95_measured_http_ms': sorted(http_us)[(len(http_us) * 95 + 99) // 100 - 1] / 1000,
        'wave_verified_sales': len(verified - {RETRY}), 'verified_sales': len(verified),
        'checkout_statuses': [first.status, second.status, duplicate.status],
        'replacement_order_status': before_retry.status, 'charges': len(referee.charged),
        'payment_attempts': referee.payment_attempts, 'unverified_charges': len(referee.charged - verified),
        'modeled_cost_cents': cost, 'profit_cents': MARGIN * len(verified) - cost,
    }, replacement_store.records


def resume(path: Path) -> dict:
    checkpoint = json.loads(path.read_text())
    if checkpoint['version'] != 1 or not isinstance(checkpoint['records'], dict):
        raise ValueError('Expected a version 1 Virtual RDS checkpoint')
    store = Store(TIERS['small'][0], checkpoint['records'])
    with serving(store, Referee(), cached=False) as shop:
        reply = request(shop.base, f'/orders/{order_key(RETRY)}')
    return {'status': reply.status, 'order': reply.data if reply.status == 200 else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    compare = commands.add_parser('compare', help='Score five architectures on the same trace')
    run = commands.add_parser('run', help='Score one architecture')
    for command in (compare, run):
        command.add_argument('--seed', type=int, default=42)
    run.add_argument('--storage', choices=TIERS, default='small')
    run.add_argument('--replicas', type=int, choices=(1, 2), default=1)
    run.add_argument('--cache', action='store_true')
    run.add_argument('--checkpoint', type=Path, help='Save app records to a new JSON file')
    restore = commands.add_parser('resume', help='Read the retry order from a fresh shop restored from JSON')
    restore.add_argument('checkpoint', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'compare':
            output = [{'plan': name, **run_round(plan, args.seed)[0]} for name, plan in PLANS.items()]
        elif args.command == 'run':
            output, records = run_round(Plan(args.storage, args.replicas, args.cache), args.seed)
            if args.checkpoint is not None:
                with args.checkpoint.open('x') as file:
                    json.dump({'version': 1, 'records': records}, file)
        else:
            output = resume(args.checkpoint)
    except (KeyError, OSError, TypeError, ValueError, RuntimeError) as error:
        print(f'Virtual RDS: {error}', file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
