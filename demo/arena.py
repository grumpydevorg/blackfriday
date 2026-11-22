"""Run the local BLACK FRIDAY rehearsal against real PHP shop processes."""

import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import file_digest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from typing import Literal, NotRequired, TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

LOGGER = logging.getLogger(__name__)
APP = Path(__file__).parent
STATE = Path('/state')
ART = Path('/art/cloud-surge.png')
CUSTOMER = 'maya'
FIRST_CART = 'cart-launch'
SAVED_CART = 'cart-saved'
SALES = {'launch-mug': FIRST_CART, 'growth-mug': SAVED_CART}
PRICE = 2500
MARGIN = 1000
LAUNCH_COST = 100
GROWTH_COST = 200
RETURN_DRAW = 42  # One controlled draw, shared by the same customer in both forks.
Kind = Literal['preserve', 'omit']
KINDS: tuple[Kind, ...] = ('preserve', 'omit')


class Launch(TypedDict):
    checkout_statuses: list[int]
    charges_before_retry: int
    charges: int
    verified_sales: int
    cart_status: int
    order_status: int
    profit_cents: int


class Branch(TypedDict):
    kind: Kind
    schema: str
    database: str
    health_status: int
    history_status: int
    cart_status: NotRequired[int]
    checkout_status: NotRequired[int | None]
    checkout_ms: NotRequired[int | None]
    charges: NotRequired[int]
    verified_sales: NotRequired[int]
    return_probability: NotRequired[int]
    returns_next_week: NotRequired[bool]
    cost_cents: NotRequired[int]
    profit_cents: NotRequired[int]
    cumulative_cents: NotRequired[int]


class Run(TypedDict):
    id: str
    phase: Literal['launch', 'migrated', 'compared']
    checkpoint: str
    launch: Launch
    branches: NotRequired[list[Branch]]
    return_draw: NotRequired[int]


@dataclass(frozen=True)
class Reply:
    status: int
    data: dict[str, object]
    elapsed_ms: int

    def expect(self, status: int) -> 'Reply':
        if self.status != status:
            raise RuntimeError(f'Shop returned {self.status}, expected {status}: {self.data}')
        return self


def request(
    base: str, path: str, body: dict[str, str] | None = None, intention: str | None = None,
) -> Reply:
    headers = {'X-Customer-ID': CUSTOMER}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if intention is not None:
        headers['Idempotency-Key'] = intention
    req = Request(base + path, None if body is None else json.dumps(body).encode(), headers)
    start = monotonic()
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        data = json.load(response)
        return Reply(response.status, data, round((monotonic() - start) * 1000))


@contextmanager
def shop(
    payment_url: str, scope: str, *, sqlite: Path | None = None, schema: str = '', database: str = 'shop',
) -> Iterator[str]:
    # Only trusted starter code runs here. This process is not a contestant sandbox.
    with socket.socket() as port_probe:
        port_probe.bind(('127.0.0.1', 0))
        port = port_probe.getsockname()[1]
    env = {**os.environ, 'PAYMENT_SCOPE': scope, 'PAYMENT_URL': payment_url}
    if sqlite is not None:
        env['SHOP_SQLITE'] = str(sqlite)
    else:
        env['SHOP_SCHEMA'] = schema
        env['PGDATABASE'] = database
    process = subprocess.Popen(['php', '-S', f'127.0.0.1:{port}', 'shop.php'], cwd=APP, env=env)
    base = f'http://127.0.0.1:{port}'
    try:
        deadline = monotonic() + 5
        while True:
            if process.poll() is not None:
                raise RuntimeError(f'PHP shop exited with status {process.returncode}')
            try:
                request(base, '/health').expect(200)
                break
            except URLError:
                if monotonic() >= deadline:
                    raise
                sleep(0.025)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS charges (
                scope TEXT NOT NULL, intention TEXT NOT NULL, receipt TEXT NOT NULL UNIQUE,
                PRIMARY KEY (scope, intention)
            )''')

    def charge(self, scope: str, intention: str) -> tuple[str, bool]:
        with closing(sqlite3.connect(self.path)) as db, db:
            receipt = uuid4().hex
            cursor = db.execute(
                'INSERT INTO charges VALUES (?, ?, ?) ON CONFLICT (scope, intention) DO NOTHING',
                (scope, intention, receipt),
            )
            if cursor.rowcount:
                return receipt, True
            existing = db.execute(
                'SELECT receipt FROM charges WHERE scope = ? AND intention = ?', (scope, intention),
            ).fetchone()
            return existing[0], False

    def receipt(self, scope: str, intention: str) -> str:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                'SELECT receipt FROM charges WHERE scope = ? AND intention = ?', (scope, intention),
            ).fetchone()
            if row is None:
                raise RuntimeError(f'No trusted charge for {intention}')
            return row[0]

    def count(self, scope: str) -> int:
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT COUNT(*) FROM charges WHERE scope = ?', (scope,)).fetchone()[0]

    def verify_order(self, scope: str, intention: str, order: Reply) -> int:
        expected = {
            'id': f'order-{intention}', 'customer_id': CUSTOMER, 'cart_id': SALES[intention],
            'intention': intention, 'amount_cents': PRICE, 'receipt': self.receipt(scope, intention),
        }
        if order.expect(200).data != expected:
            raise RuntimeError(f'Persisted order for {intention} does not match its trusted charge')
        return 1


class Handler(BaseHTTPRequestHandler):
    def send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, value: object) -> None:
        self.send_body(status, json.dumps(value).encode(), 'application/json')


class Payments(HTTPServer):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__(('127.0.0.1', 0), PaymentHandler)
        self.ledger = ledger


class PaymentHandler(Handler):
    server: Payments

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if self.path != '/payments' or not 0 < size <= 1024:
                raise ValueError('Invalid payment request')
            data = json.loads(self.rfile.read(size))
            scope = data['scope']
            phase = scope.rsplit(':', 1)[-1]
            intention = 'launch-mug' if phase == 'launch' else 'growth-mug'
            if not (
                re.fullmatch('[0-9a-f]{32}:(launch|preserve|omit)', scope)
                and data['intention'] == intention
                and data['cart_id'] == SALES[intention]
                and data['customer_id'] == CUSTOMER
                and data['amount_cents'] == PRICE
            ):
                raise ValueError('Payment does not match the scenario')
        except (AttributeError, KeyError, TypeError, ValueError):
            self.send_json(400, {'error': 'Invalid payment intention'})
            return
        receipt, first = self.server.ledger.charge(scope, intention)
        if first and phase == 'launch':
            # Commit first. The checkout really loses its upstream HTTP response.
            self.close_connection = True
            return
        self.send_json(200, {'receipt': receipt})


def digest(path: Path) -> str:
    with path.open('rb') as file:
        return file_digest(file, 'sha256').hexdigest()


class Rehearsal:
    def __init__(self, payments: Payments) -> None:
        self.ledger = payments.ledger
        self.payment_url = f'http://127.0.0.1:{payments.server_port}/payments'
        self.current = STATE / 'current.json'
        self.run: Run | None = json.loads(self.current.read_text()) if self.current.exists() else None

    def save(self, run: Run | None) -> Run | None:
        draft = self.current.with_suffix('.tmp')
        draft.write_text(json.dumps(run))
        draft.replace(self.current)
        self.run = run
        return run

    def launch(self) -> Run:
        run_id = uuid4().hex
        folder = STATE / run_id
        folder.mkdir()
        database = folder / 'shop.sqlite'
        scope = f'{run_id}:launch'
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript((APP / 'schema.sql').read_text())
            db.execute('INSERT INTO customers VALUES (?, ?)', (CUSTOMER, 'Maya'))
        with shop(self.payment_url, scope, sqlite=database) as base:
            request(base, '/product').expect(200)
            request(base, '/cart', {'cart_id': FIRST_CART, 'sku': 'cloud-mug'}).expect(200)
            checkout = {'cart_id': FIRST_CART}
            lost = request(base, '/checkout', checkout, 'launch-mug').expect(502)
            charged = self.ledger.count(scope)
            request(base, '/orders/order-launch-mug').expect(404)
            retry = request(base, '/checkout', checkout, 'launch-mug').expect(200)
            request(base, '/cart', {'cart_id': SAVED_CART, 'sku': 'cloud-mug'}).expect(200)
        checkpoint = folder / 'checkpoint.sqlite'
        database.rename(checkpoint)
        # Reopen with a new PHP process: no sale is verified from the checkout response.
        with shop(self.payment_url, scope, sqlite=checkpoint) as base:
            cart = request(base, '/cart/' + SAVED_CART).expect(200)
            order = request(base, '/orders/order-launch-mug')
            sold = self.ledger.verify_order(scope, 'launch-mug', order)
        return {
            'id': run_id, 'phase': 'launch', 'checkpoint': digest(checkpoint),
            'launch': {
                'checkout_statuses': [lost.status, retry.status], 'charges_before_retry': charged,
                'charges': self.ledger.count(scope), 'verified_sales': sold,
                'cart_status': cart.status, 'order_status': order.status,
                'profit_cents': MARGIN * sold - LAUNCH_COST,
            },
        }

    def migrate(self, run: Run) -> Run:
        checkpoint = STATE / run['id'] / 'checkpoint.sqlite'
        if digest(checkpoint) != run['checkpoint']:
            raise RuntimeError('Launch checkpoint has changed; start a new rehearsal')
        branches: list[Branch] = []
        attempt = uuid4().hex
        for kind in KINDS:
            schema = f'bf_{attempt}_{kind}'
            subprocess.run(
                ['php', 'migrate.php', str(checkpoint), schema, kind], cwd=APP, check=True, timeout=15,
            )
            with shop(self.payment_url, f'{run["id"]}:{kind}', schema=schema) as base:
                health = request(base, '/health').expect(200)
                order = request(base, '/orders/order-launch-mug')
                self.ledger.verify_order(f'{run["id"]}:launch', 'launch-mug', order)
            branches.append({
                'kind': kind, 'schema': schema, 'database': str(health.data['database']),
                'health_status': health.status, 'history_status': order.status,
            })
        if digest(checkpoint) != run['checkpoint']:
            raise RuntimeError('Migration changed the Launch checkpoint')
        return {**run, 'phase': 'migrated', 'branches': branches}

    def compare(self, run: Run) -> Run:
        branches: list[Branch] = []
        for branch in run['branches']:
            scope = f'{run["id"]}:{branch["kind"]}'
            with shop(self.payment_url, scope, schema=branch['schema']) as base:
                cart = request(base, '/cart/' + SAVED_CART)
                checkout = (
                    request(base, '/checkout', {'cart_id': SAVED_CART}, 'growth-mug').expect(200)
                    if cart.status == 200 else None
                )
            if checkout is not None:
                with shop(self.payment_url, scope, schema=branch['schema']) as base:
                    order = request(base, '/orders/order-growth-mug')
                    sold = self.ledger.verify_order(scope, 'growth-mug', order)
                return_probability = 75 if checkout.elapsed_ms <= 2000 else 50
            else:
                cart.expect(404)
                sold = 0
                return_probability = 5  # Losing a saved cart is a history/integrity failure.
            profit = MARGIN * sold - GROWTH_COST
            branches.append({
                **branch, 'cart_status': cart.status, 'verified_sales': sold,
                'checkout_status': checkout.status if checkout is not None else None,
                'checkout_ms': checkout.elapsed_ms if checkout is not None else None,
                'charges': self.ledger.count(scope), 'return_probability': return_probability,
                'returns_next_week': RETURN_DRAW < return_probability, 'cost_cents': GROWTH_COST,
                'profit_cents': profit, 'cumulative_cents': run['launch']['profit_cents'] + profit,
            })
        return {**run, 'phase': 'compared', 'branches': branches, 'return_draw': RETURN_DRAW}


class Arena(HTTPServer):
    def __init__(self, rehearsal: Rehearsal) -> None:
        super().__init__(('0.0.0.0', 8766), ArenaHandler)
        self.rehearsal = rehearsal


class ArenaHandler(Handler):
    server: Arena

    def do_GET(self) -> None:
        match self.path:
            case '/':
                self.send_body(200, (APP / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            case '/art/cloud-surge.png':
                self.send_body(200, ART.read_bytes(), 'image/png')
            case '/api/state':
                self.send_json(200, self.server.rehearsal.run)
            case _:
                self.send_json(404, {'error': 'Not found'})

    def do_POST(self) -> None:
        if self.headers.get('X-Rehearsal-Action') != '1':
            self.send_json(403, {'error': 'Use the rehearsal page to run an action'})
            return
        rehearsal = self.server.rehearsal
        run = rehearsal.run
        try:
            if self.path == '/api/launch' and run is None:
                updated = rehearsal.launch()
            elif self.path == '/api/migrate' and run is not None and run['phase'] == 'launch':
                updated = rehearsal.migrate(run)
            elif self.path == '/api/compare' and run is not None and run['phase'] == 'migrated':
                updated = rehearsal.compare(run)
            elif self.path == '/api/reset' and run is not None and run['phase'] == 'compared':
                updated = None
            else:
                self.send_json(409, {'error': 'This action does not match the saved rehearsal stage'})
                return
            result = rehearsal.save(updated)
        except (OSError, RuntimeError, subprocess.SubprocessError, sqlite3.Error):
            LOGGER.exception('Rehearsal step failed')
            self.send_json(500, {'error': 'Rehearsal step failed. See docker compose logs arena.'})
            return
        self.send_json(200, result)


def stop(_signal: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    with Payments(Ledger(STATE / 'referee.sqlite')) as payments, Arena(Rehearsal(payments)) as arena:
        thread = Thread(target=payments.serve_forever, daemon=True)
        thread.start()
        try:
            arena.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            payments.shutdown()
            thread.join()


if __name__ == '__main__':
    main()
