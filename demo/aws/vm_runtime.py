"""Fixed shop and artifact routes for the roleless BLACK FRIDAY MicroVM."""

import base64
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
from collections import deque
from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep, time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOGGER = logging.getLogger(__name__)
APP = Path('/app')
STATE = Path('/state')
SQLITE = STATE / 'shop.sqlite'
DUMP = STATE / 'branch.dump'
PG = Path('/usr/lib/postgresql/17/bin')
READY_HOOK = '/aws/lambda-microvms/runtime/v1/ready'
MAX_ARTIFACT = 1024 * 1024
PAYMENT_URL = 'http://127.0.0.1:8082/payments'
Hex32 = re.compile(r'[0-9a-f]{32}')
Team = re.compile(r'[a-z0-9]{3,16}')
# PHP's built-in server logs every request to stderr; those lines carry nothing the room needs.
PHP_ACCESS = re.compile(r'(\d{1,3}\.){3}\d{1,3}:\d+ (Accepted|Closing|\[\d{3}\]:)|Development Server .* started')
# PHP's own severity, as written by its error log: `PHP Warning:  ...`, optionally after a `[date]` stamp.
PHP_SEVERITY = re.compile(r'(?:\[[^\]]*\] )?PHP (Fatal error|Parse error|Warning|Notice|Deprecated):')
PHP_LEVEL = {'Fatal error': 'error', 'Parse error': 'error', 'Warning': 'warn', 'Notice': 'info', 'Deprecated': 'info'}

LEVELS = ('debug', 'info', 'warn', 'error')
KINDS = ('stage', 'php')
ACTIONS = ('launch', 'migrate', 'compare')
MAX_MESSAGE = 200
BUFFER = 500
# Anything a message could leak: a URL (payment capability), a signed-URL parameter, a receipt or any long hex run.
SECRET = re.compile(r'https?://\S+|\S*X-Amz-\S*|[0-9a-fA-F]{32,}')


def redact(text: str, run_id: str = '') -> str:
    """Shorten the run id, drop anything URL- or receipt-shaped, then limit the length. In this order: a secret
    cut in half by the limit would no longer match the patterns."""
    if run_id:
        text = text.replace(run_id, run_id[:8])
    return SECRET.sub('<redacted>', ' '.join(text.split()))[:MAX_MESSAGE]


class Journal:
    """The VM's own log. MicroVM logging to CloudWatch is not possible, so nothing here reaches CloudWatch by
    itself: every entry goes to stdout and to this buffer, which the evaluator reads over GET /control/log and
    relays before it terminates the VM. An entry is one JSON object: seq, t, kind, level, run, action, option, msg,
    and for a PHP request also method, path, status and ms. A stage entry may carry the ms its step took. Messages
    hold only timed stage facts and request facts: never the payment URL, the receipt, a token, a request body or
    the full run id. Entries made before setup (no run, no action) are not recorded."""

    def __init__(self) -> None:
        self.run_id = ''
        self.action = ''
        self.option: str | None = None
        self.seq = 0
        self.entries: deque[dict] = deque(maxlen=BUFFER)
        self.lock = Lock()

    def configure(self, run_id: str, action: str, option: str | None) -> None:
        assert action in ACTIONS, action
        self.run_id, self.action, self.option = run_id, action, option

    def record(self, kind: str, level: str, message: str, **fields: object) -> None:
        assert kind in KINDS and level in LEVELS, (kind, level)
        if not self.action:
            return
        with self.lock:
            entry = {
                'seq': self.seq, 't': round(time() * 1000), 'kind': kind, 'level': level,
                'run': self.run_id[:8], 'action': self.action, 'option': self.option,
                'msg': redact(message, self.run_id), **fields,
            }
            self.seq += 1
            self.entries.append(entry)
        print(json.dumps(entry, separators=(',', ':')), flush=True)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.entries)


JOURNAL = Journal()


def log_stage(message: str, level: str = 'info', ms: int | None = None) -> None:
    JOURNAL.record('stage', level, message, **({} if ms is None else {'ms': ms}))


def log_took(message: str, started: float, level: str = 'info') -> None:
    """A stage line whose text ends with how long it took, and which carries the same time as `ms`."""
    log_stage(f'{message} in {timed(started)}', level, round((monotonic() - started) * 1000))


def log_php(method: str, path: str, status: int, ms: int, dropped: bool = False) -> None:
    """One PHP request, from its parsed fields. The query string never enters the entry. A 5xx is an error unless
    the shop's payment reply was dropped on purpose."""
    path = urlsplit(path).path
    level = 'info' if status < 500 else 'warn' if dropped else 'error'
    JOURNAL.record('php', level, f'{method} {path} {status} {ms} ms',
                   method=method, path=redact(path), status=status, ms=ms)


def buffered_entries() -> list[dict]:
    return JOURNAL.snapshot()


def timed(started: float) -> str:
    return f'{monotonic() - started:.2f} s'


class BadRequest(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

S3_HTTP = build_opener(NoRedirect())


def ledger_url(value: object) -> str:
    if not isinstance(value, str):
        raise BadRequest('Invalid payment capability')
    url = urlsplit(value)
    if url.username is not None or url.password is not None or url.fragment:
        raise BadRequest('Invalid payment capability')
    if (
        os.environ.get('ALLOW_LOCAL_LEDGER') == '1'
        and url.scheme == 'http'
        and url.hostname in {'127.0.0.1', 'host.docker.internal'}
    ):
        return value
    signed = parse_qs(url.query).get('X-Amz-SignedHeaders', [''])[0].split(';')
    if not (
        url.scheme == 'https' and url.port in {None, 443}
        and re.fullmatch(r'[a-z0-9-]+\.s3\.[a-z0-9-]+\.amazonaws\.com', url.hostname or '')
        and {'if-none-match', 'x-amz-checksum-sha256'} <= set(signed)
    ):
        raise BadRequest('Invalid payment capability')
    return value


def shop_request(
    method: str, path: str, body: bytes | None = None, intention: str = '',
) -> tuple[int, bytes, int]:
    headers = {'X-Customer-ID': 'maya'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if intention:
        headers['Idempotency-Key'] = intention
    with closing(HTTPConnection('127.0.0.1', 8081, timeout=5)) as connection:
        started = monotonic()
        connection.request(method, path, body, headers)
        with connection.getresponse() as response:
            payload = response.read(16385)
            if len(payload) > 16384:
                raise RuntimeError('Unexpected shop response size')
            return response.status, payload, round((monotonic() - started) * 1000)


class Payment:
    def __init__(self, scope: str, launching: bool, url: str, receipt: str) -> None:
        self.url = url
        self.receipt = receipt
        self.last_status = ''  # 'PUT' outcome of the latest charge, for the log
        self.drop_next = launching
        self.expected = {
            'scope': scope, 'customer_id': 'maya', 'amount_cents': 2500,
            'intention': 'launch-mug' if launching else 'growth-mug',
            'cart_id': 'cart-launch' if launching else 'cart-saved',
        }

    def charge(self) -> bool:
        body = self.receipt.encode('ascii')
        checksum = base64.b64encode(hashlib.sha256(body).digest()).decode('ascii')
        request = Request(self.url, data=body, method='PUT', headers={
            'If-None-Match': '*', 'x-amz-checksum-sha256': checksum,
        })
        try:
            with S3_HTTP.open(request, timeout=2.5) as response:
                if response.status != 200:
                    raise RuntimeError('Unexpected ledger PUT status')
                self.last_status = '200'
        except HTTPError as error:
            # 412 means the exact key already has a receipt. Its content is verified
            # independently in S3 by the evaluator; the checksum binds this PUT.
            with error:
                if error.code != 412:
                    raise
                self.last_status = '412 already paid'
        drop = self.drop_next
        self.drop_next = False
        # A rebuilt Launch VM still demonstrates a real lost response. Its first
        # PUT may be a replay after a crash; no second payment can be applied.
        return drop


class Runtime:
    def __init__(self) -> None:
        self.action = ''
        self.kind = ''
        self.schema = ''
        self.shop: subprocess.Popen | None = None
        self.sealed = False
        self.payment: Payment | None = None
        self.env = {'PATH': os.environ['PATH'], 'PGHOST': '127.0.0.1', 'PAYMENT_URL': PAYMENT_URL}

    def start_shop(self) -> None:
        self.shop = subprocess.Popen(
            ['php', '-S', '127.0.0.1:8081', 'shop.php'], cwd=APP, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace',
        )
        Thread(target=self.relay_php, args=(self.shop,), daemon=True).start()
        # Was 3 s. On 24 Nov 2026 PHP in a fresh Sydney MicroVM was not listening
        # within 3 s (ConnectionRefusedError), though it had been at 17:07 that day.
        started = monotonic()
        deadline = started + 20
        while True:
            if self.shop.poll() is not None:
                raise RuntimeError(f'PHP shop exited before readiness with code {self.shop.returncode}')
            try:
                status, _, elapsed = shop_request('GET', '/health')
                log_php('GET', '/health', status, elapsed)
                if status != 200:
                    raise RuntimeError('PHP shop could not read the database')
                log_took('PHP shop ready', started)
                return
            except ConnectionRefusedError:
                if monotonic() >= deadline:
                    raise
                sleep(0.025)

    @staticmethod
    def relay_php(process: subprocess.Popen) -> None:
        """PHP warnings and errors reach the log, at the severity PHP itself wrote; its per-request access lines
        do not. A line without a severity is unclassified PHP stderr, so it is a warning."""
        for line in process.stderr:
            line = line.strip()
            if line and not PHP_ACCESS.search(line):
                severity = PHP_SEVERITY.match(line)
                log_stage(f'php: {line}', PHP_LEVEL[severity[1]] if severity else 'warn')

    def setup(self, data: dict) -> None:
        if self.action:
            raise BadRequest('VM already configured')
        received = monotonic()
        run_id, action, kind = data.get('id'), data.get('action'), data.get('kind')
        team = data.get('team')
        if not isinstance(run_id, str) or not Hex32.fullmatch(run_id):
            raise BadRequest('Invalid run id')
        if action == 'launch':
            if set(data) != {'id', 'action', 'pay_url', 'receipt'}:
                raise BadRequest('Invalid Launch configuration')
        elif (
            action in ('migrate', 'compare') and kind in ('preserve', 'omit')
            and isinstance(team, str) and Team.fullmatch(team)
        ):
            fields = {'id', 'action', 'team', 'kind', 'input'}
            if action == 'compare' and kind == 'preserve':
                fields |= {'pay_url', 'receipt'}
            if set(data) != fields:
                raise BadRequest('Invalid Growth configuration')
        else:
            raise BadRequest('Invalid action, team or migration kind')
        JOURNAL.configure(run_id, action, team if action != 'launch' else None)
        log_stage(f'setup received: {action}' + (f' ({kind})' if kind else '')
                  + (f', {len(data["input"]) * 3 // 4 // 1024} KB input' if 'input' in data else ''))

        artifact = b''
        if action != 'launch':
            artifact = base64.b64decode(data['input'], validate=True)
            magic = b'SQLite format 3\0' if action == 'migrate' else b'PGDMP'
            if not artifact.startswith(magic) or len(artifact) > MAX_ARTIFACT:
                raise BadRequest('Invalid input artifact')
        if 'pay_url' in data:
            receipt = data['receipt']
            if not isinstance(receipt, str) or not Hex32.fullmatch(receipt):
                raise BadRequest('Invalid payment receipt')
            scope = f'{run_id}:{team or action}'
            self.env['PAYMENT_SCOPE'] = scope
            self.payment = Payment(scope, action == 'launch', ledger_url(data['pay_url']), receipt)
        # Setup is single-use, even if a local subprocess fails partway through.
        self.action, self.kind = action, kind or ''
        if action == 'launch':
            self.env.pop('SHOP_SCHEMA', None)
            self.env['SHOP_SQLITE'] = str(SQLITE)
            started = monotonic()
            with closing(sqlite3.connect(SQLITE)) as db, db:
                db.executescript((APP / 'schema.sql').read_text())
                db.execute('INSERT INTO customers VALUES (?, ?)', ('maya', 'Maya'))
            log_took('SQLite database created', started)
        else:
            self.schema = f'bf_{run_id}_{team}'
            self.env['SHOP_SCHEMA'] = self.schema
            cluster = str(STATE / 'postgres')
            started = monotonic()
            self.run([PG / 'initdb', '-D', cluster, '-A', 'trust', '-U', 'demo', '--locale=C'])
            log_took('initdb done', started)
            started = monotonic()
            self.run([
                PG / 'pg_ctl', '-D', cluster, '-l', STATE / 'postgres.log',
                '-o', '-h 127.0.0.1 -k /state', '-w', '-t', '10', 'start',
            ])
            self.run([PG / 'createdb', '-U', 'demo', 'shop'])
            log_took('PostgreSQL started (pg_ctl start, createdb)', started)
            started = monotonic()
            if action == 'migrate':
                SQLITE.write_bytes(artifact)
                self.run(['php', 'migrate.php', SQLITE, self.schema, kind])
                if SQLITE.read_bytes() != artifact:
                    raise RuntimeError('Migration modified its input checkpoint')
                log_took(f'migrate.php ({kind}: {"every table" if kind == "preserve" else "carts skipped"}) done',
                         started)
            else:
                DUMP.write_bytes(artifact)
                self.run([PG / 'pg_restore', '--exit-on-error', '-U', 'demo', '-d', 'shop', DUMP])
                log_took('pg_restore done', started)
        self.start_shop()
        log_took('setup finished', received)

    def run(self, cmd: list) -> None:
        # Tool chatter (initdb's banner and the like) stays out of the log; a failure shows its last words.
        try:
            subprocess.run(cmd, cwd=APP, env=self.env, check=True, timeout=12, capture_output=True, text=True,
                           errors='replace')
        except subprocess.CalledProcessError as error:
            tail = ' | '.join((error.stderr or error.stdout or '').strip().splitlines()[-3:])
            log_stage(f'{Path(str(cmd[0])).name} failed with code {error.returncode}: {tail}', 'error')
            raise

    def export(self) -> bytes:
        started = monotonic()
        if self.action == 'launch':
            if not self.sealed:
                if self.shop is None:
                    raise RuntimeError('Shop is not running')
                self.shop.terminate()
                self.shop.wait(timeout=3)
                self.sealed = True
                self.start_shop()  # Read the committed database through a fresh PHP process.
            value = SQLITE.read_bytes()
        elif self.action == 'migrate':
            self.run([
                PG / 'pg_dump', '-Fc', '-U', 'demo', '-d', 'shop', '--schema', self.schema, '-f', DUMP,
            ])
            value = DUMP.read_bytes()
        else:
            raise BadRequest('This stage does not produce an artifact')
        log_took(f'artifact exported: {len(value) // 1024} KB '
                 f'({"SQLite checkpoint" if self.action == "launch" else "pg_dump"})', started)
        return value


class Handler(BaseHTTPRequestHandler):
    runtime: Runtime
    timeout = 8

    def log_message(self, format, *args) -> None:
        pass  # Do not log capabilities, request bodies, or client-controlled paths.

    def send_body(
        self, status: int, body: bytes, content_type: str, elapsed: int | None = None, dropped: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if elapsed is not None:
            self.send_header('X-Shop-Elapsed-Ms', str(elapsed))
        if dropped:
            self.send_header('X-Payment-Reply-Dropped', '1')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, value: object) -> None:
        self.send_body(status, json.dumps(value).encode(), 'application/json')

    def read_json(self, limit: int) -> dict:
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= limit:
            raise BadRequest('Invalid request size')
        data = json.loads(self.rfile.read(size))
        if not isinstance(data, dict):
            raise BadRequest('JSON object required')
        return data


class PaymentHandler(Handler):
    @staticmethod
    def charge(payment: Payment) -> bool:
        started = monotonic()
        try:
            dropped = payment.charge()
        except BaseException:
            ms = round((monotonic() - started) * 1000)
            log_stage(f'payment ledger PUT failed in {ms} ms', 'error', ms)
            raise
        ms = round((monotonic() - started) * 1000)
        log_stage(f'payment ledger PUT {payment.last_status} in {ms} ms', 'info', ms)
        return dropped

    def do_POST(self) -> None:
        try:
            payment = self.runtime.payment
            if self.path != '/payments' or payment is None or self.read_json(1024) != payment.expected:
                raise BadRequest('Payment does not match the scenario')
            if self.charge(payment):
                # Close the response stream without writing any HTTP response at all.
                self.close_connection = True
            else:
                self.send_json(200, {'receipt': payment.receipt})
        except (TypeError, ValueError):
            self.send_json(400, {'error': 'Invalid payment'})
        except (BrokenPipeError, ConnectionResetError):
            return
        except (URLError, OSError, RuntimeError):
            self.send_json(502, {'error': 'Ledger PUT failed'})


class Gateway(Handler):
    def do_GET(self) -> None:
        self.route('GET')

    def do_POST(self) -> None:
        self.route('POST')

    def route(self, method: str) -> None:
        try:
            self.dispatch(method)
        except (TypeError, ValueError):
            self.send_json(400, {'error': 'Invalid stage request'})
        except (BrokenPipeError, ConnectionResetError):
            return
        except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
            # Never include bearer URLs from request data in an error or log.
            LOGGER.error('VM stage %s failed: %s', self.runtime.action, type(error).__name__)
            self.send_json(500, {'error': 'VM stage failed'})

    def dispatch(self, method: str) -> None:
        runtime = self.runtime
        match method, self.path:
            case ('GET', '/ready') | ('POST', '/aws/lambda-microvms/runtime/v1/ready'):
                self.send_json(200, {'status': 'ready'})
                return
            case ('POST', '/control/setup'):
                runtime.setup(self.read_json(2 * MAX_ARTIFACT))
                self.send_json(200, {'status': 'configured'})
                return
            case ('GET', '/control/log'):
                self.send_json(200, {'entries': buffered_entries()})
                return
            case ('GET', '/control/artifact'):
                value = runtime.export()
                if len(value) > MAX_ARTIFACT:
                    raise RuntimeError('Unexpected output artifact size')
                self.send_body(200, value, 'application/octet-stream')
                return
        if runtime.shop is None:
            self.send_json(503, {'error': 'Shop not configured'})
            return
        body = None
        intention = ''
        payment = runtime.payment
        drop_pending = False
        if method == 'GET' and self.path in {
            '/health', '/product', '/cart/cart-saved', '/orders/order-launch-mug', '/orders/order-growth-mug',
        }:
            pass
        elif method == 'POST' and self.path == '/cart' and runtime.action == 'launch' and not runtime.sealed:
            data = self.read_json(1024)
            if data.get('cart_id') not in ('cart-launch', 'cart-saved') or data.get('sku') != 'cloud-mug':
                raise BadRequest('Invalid Launch cart')
            body = json.dumps(data).encode()
        elif method == 'POST' and self.path == '/checkout' and not runtime.sealed and payment is not None:
            data = self.read_json(1024)
            intention = str(payment.expected['intention'])
            if (
                data != {'cart_id': payment.expected['cart_id']}
                or self.headers.get('Idempotency-Key') != intention
            ):
                raise BadRequest('Invalid checkout intention')
            body = json.dumps(data).encode()
            drop_pending = payment.drop_next
        else:
            self.send_json(404, {'error': 'No such stage route'})
            return
        status, payload, elapsed = shop_request(method, self.path, body, intention)
        dropped = drop_pending and payment is not None and not payment.drop_next
        log_php(method, self.path, status, elapsed, dropped)
        if self.path == '/checkout':
            log_stage(f'checkout {status} in {elapsed} ms'
                      + (' · reply dropped on purpose (a lost response; the retry must not charge twice)' if dropped else ''),
                      'warn' if dropped else 'error' if status >= 500 else 'info', elapsed)
        elif self.path == '/cart/cart-saved':
            # Info either way: a fast migration drops the cart by design. Whether it was expected is the judge's call.
            log_stage(f"Maya's saved cart: {status} ({'kept' if status == 200 else 'missing'}) in {elapsed} ms",
                      'info', elapsed)
        self.send_body(status, payload, 'application/json', elapsed, dropped)


def shutdown(signum, frame) -> None:
    log_stage(f'shutdown ({signal.Signals(signum).name})')
    sys.exit(0)


if __name__ == '__main__':
    runtime = Runtime()
    Gateway.runtime = PaymentHandler.runtime = runtime
    signal.signal(signal.SIGTERM, shutdown)
    with HTTPServer(('127.0.0.1', 8082), PaymentHandler) as payments:
        Thread(target=payments.serve_forever, daemon=True).start()
        with HTTPServer(('0.0.0.0', 8080), Gateway) as gateway:
            print('runtime up, waiting for setup', flush=True)  # Before setup there is no run to label an entry with.
            gateway.serve_forever()
