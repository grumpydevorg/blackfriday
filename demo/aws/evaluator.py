"""AWS Event evaluator: referee the trusted PHP shop in fresh roleless MicroVMs."""

import base64
import json
import os
import re
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep, time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

MAX_ARTIFACT = 1024 * 1024
VM_LIFESPAN = 120
PRICE = 2500
LEVELS = ('debug', 'info', 'warn', 'error')
ACTIONS = ('launch', 'migrate', 'compare')


def real_options() -> dict[str, str]:
    """The options whose evidence is really run in a MicroVM, as option id -> migration kind, from rounds.json (the
    one table the game, the workflow and this worker share; the worker package ships it). The kind follows what the
    recorded evidence says the migration does to the saved cart: kept -> `preserve` (copies every table), lost ->
    `omit` (skips carts). The schema and ledger ids reuse the option id."""
    table = json.loads(Path(__file__).with_name('rounds.json').read_text())
    return {
        option['id']: 'preserve' if table['recorded_evidence'][option['id']]['cart_kept'] else 'omit'
        for rnd in table['rounds'] for option in rnd['options'] if option['evidence'] == 'real'
    }


OPTIONS = real_options()
HEX32 = re.compile(r'[0-9a-f]{32}')
AWS_CONFIG = Config(connect_timeout=3, read_timeout=6, retries={'total_max_attempts': 2})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = build_opener(NoRedirect())


def request_budget(deadline: float, maximum: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError('Evaluator deadline elapsed')
    return min(remaining, maximum)


def pause(deadline: float) -> None:
    sleep(request_budget(deadline, 0.4))


def seconds(started: float) -> str:
    return f'{monotonic() - started:.2f} s'


def millis(started: float) -> int:
    return round((monotonic() - started) * 1000)


# Anything URL- or receipt-shaped. The VM already redacts before it truncates (vm_runtime.redact); this is the same
# rule applied again here, in case a VM entry still carries one. It changes nothing in a clean entry.
SECRET = re.compile(r'https?://\S+|\S*X-Amz-\S*|[0-9a-fA-F]{32,}')
VM_FIELDS = {'seq', 't', 'kind', 'level', 'run', 'action', 'option', 'msg'}
PHP_FIELDS = {'method', 'path', 'status', 'ms'}


def number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def vm_entry(raw: object) -> dict | None:
    """One VM log entry exactly as the VM wrote it, or None when it is not a valid one: the field set, types and
    closed values of the contract. A stage entry may add `ms`; a PHP entry always has method, path, status, ms."""
    if not isinstance(raw, dict):
        return None
    php = raw.get('kind') == 'php'
    allowed = VM_FIELDS | PHP_FIELDS if php else VM_FIELDS | {'ms'}
    if not VM_FIELDS <= set(raw) <= allowed or (php and not PHP_FIELDS <= set(raw)):
        return None
    run, option, message = raw['run'], raw['option'], raw['msg']
    valid = (
        number(raw['seq']) and number(raw['t']) and raw['kind'] in ('stage', 'php') and raw['level'] in LEVELS
        and isinstance(run, str) and re.fullmatch(r'[0-9a-f]{8}', run) and raw['action'] in ACTIONS
        and (option is None or (isinstance(option, str) and option in OPTIONS)) and isinstance(message, str)
        and len(message) <= 200
        and number(raw.get('ms', 0))
    )
    if valid and php:
        valid = (
            isinstance(raw['method'], str) and re.fullmatch(r'[A-Z]{3,7}', raw['method'])
            and isinstance(raw['path'], str) and raw['path'].startswith('/') and '?' not in raw['path']
            and number(raw['status'])
        )
    if not valid:
        return None
    entry = {**raw, 'msg': SECRET.sub('<redacted>', message)[:200]}
    if php:
        entry['path'] = SECRET.sub('<redacted>', raw['path'])
    return entry if entry != raw else raw


def relay_lines(entries: list) -> tuple[list[str], int]:
    """`[VM] <json>` for each valid entry, in the order the VM numbered them, and how many were dropped as invalid."""
    valid = [entry for entry in map(vm_entry, entries) if entry is not None]
    valid.sort(key=lambda entry: entry['seq'])
    return [f'[VM] {json.dumps(entry, separators=(",", ":"))}' for entry in valid], len(entries) - len(valid)


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    elapsed: int | None
    payment_reply_dropped: bool

    def expect(self, status: int, path: str) -> 'Reply':
        if self.status != status:
            raise RuntimeError(f'{path} returned HTTP {self.status}, expected {status}')
        return self

    def json(self) -> dict:
        return json.loads(self.body)


class Store:
    def __init__(self, region: str) -> None:
        self.bucket = os.environ['ARTIFACT_BUCKET']
        self.client = boto3.client('s3', region_name=region, config=AWS_CONFIG.merge(Config(
            signature_version='s3v4', s3={'addressing_style': 'virtual', 'us_east_1_regional_endpoint': 'regional'},
            request_checksum_calculation='when_required',
        )))

    def get(self, key: str) -> bytes | None:
        try:
            value = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response['Error']['Code'] == 'NoSuchKey':
                return None
            raise
        with closing(value['Body']) as body:
            result = body.read(MAX_ARTIFACT + 1)
        if len(result) > MAX_ARTIFACT:
            raise RuntimeError('Unexpected S3 artifact size')
        return result

    def require(self, key: str) -> bytes:
        result = self.get(key)
        if result is None:
            raise RuntimeError(f'Missing prerequisite: {key.split("/", 2)[-1]}')  # without the run id
        return result

    def put_once(self, key: str, body: bytes, content_type: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=body, IfNoneMatch='*', ContentType=content_type,
            )
        except ClientError as error:
            if error.response['Error']['Code'] != 'PreconditionFailed':
                raise

    def payment_capability(self, key: str, receipt: bytes) -> dict[str, str]:
        checksum = base64.b64encode(sha256(receipt).digest()).decode('ascii')
        url = self.client.generate_presigned_url(
            'put_object', Params={
                'Bucket': self.bucket, 'Key': key, 'IfNoneMatch': '*', 'ChecksumSHA256': checksum,
            }, ExpiresIn=VM_LIFESPAN, HttpMethod='PUT',
        )
        return {'pay_url': url, 'receipt': receipt.decode('ascii')}


class Fleet:
    def __init__(self, region: str) -> None:
        self.client = boto3.client('lambda-microvms', region_name=region, config=AWS_CONFIG)
        self.image = os.environ['IMAGE_ARN']
        self.version = os.environ['IMAGE_VERSION']
        self.connector = f'arn:{self.image.split(":")[1]}:lambda:{region}:aws:network-connector:aws-network-connector:'

    def run(self, identity: str) -> dict:
        return self.client.run_microvm(
            imageIdentifier=self.image, imageVersion=self.version,
            # Retries of the AWS API call are one provisioning request, while a retry
            # of unfinished work must be able to start over in a fresh MicroVM.
            clientToken=f'{identity}-{uuid4().hex}', maximumDurationInSeconds=VM_LIFESPAN,
            ingressNetworkConnectors=[self.connector + 'HTTP_INGRESS'],
            egressNetworkConnectors=[self.connector + 'INTERNET_EGRESS'],
            # VM logging to CloudWatch is not possible: RunMicrovm refuses it without an executionRoleArn, and the VM
            # must keep no AWS role. The VM keeps its own log and the evaluator relays it (Rehearsal.relay).
            logging={'disabled': {}},
        )

    def get(self, vm_id: str) -> dict:
        return self.client.get_microvm(microvmIdentifier=vm_id)

    def connect(self, vm: dict, deadline: float) -> 'Shop':
        vm_id = vm['microvmId']
        while True:
            if not vm.get('endpoint'):
                vm = self.get(vm_id)
            if vm.get('endpoint'):
                try:
                    token = self.client.create_microvm_auth_token(
                        microvmIdentifier=vm_id, expirationInMinutes=3, allowedPorts=[{'port': 8080}],
                    )['authToken']['X-aws-proxy-auth']
                    break
                except ClientError as error:
                    if error.response['Error']['Code'] not in ('ConflictException', 'ResourceNotFoundException'):
                        raise
            pause(deadline)
        endpoint = vm['endpoint']
        shop = Shop(endpoint if endpoint.startswith('https://') else f'https://{endpoint}', token, deadline)
        while True:
            try:
                ready = shop.request('/ready', timeout=3)
                if ready.status == 200:
                    return shop
                if ready.status not in (404, 409, 425, 429, 500, 502, 503, 504):
                    ready.expect(200, '/ready')
            except (URLError, TimeoutError, ConnectionError):
                pass
            pause(deadline)

    def finish(self, vm_id: str, deadline: float) -> None:
        requested = False
        while True:
            request_budget(deadline, 1)
            try:
                state = self.get(vm_id)['state']
            except self.client.exceptions.ResourceNotFoundException:
                # The newly allocated ID may not be visible yet. A 404 is not
                # termination evidence and must not be reported as success.
                state = None
            if state == 'TERMINATED':
                return
            if not requested and state != 'TERMINATING':
                try:
                    self.client.terminate_microvm(microvmIdentifier=vm_id)
                    requested = True
                except (self.client.exceptions.ConflictException, self.client.exceptions.ResourceNotFoundException):
                    pass
            pause(deadline)


class Shop:
    def __init__(self, endpoint: str, token: str, deadline: float, emit=None) -> None:
        self.base = endpoint.rstrip('/')
        self.token = token
        self.deadline = deadline
        self.emit = emit  # called with (msg, level, ms) per shop check; never given a body or a header

    def request(self, path: str, body: dict | None = None, intention: str = '', timeout: float = 7) -> Reply:
        headers = {'X-aws-proxy-auth': self.token, 'X-aws-proxy-port': '8080'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if intention:
            headers['Idempotency-Key'] = intention
        req = Request(self.base + path, None if body is None else json.dumps(body).encode(), headers)
        try:
            response = HTTP.open(req, timeout=request_budget(self.deadline, timeout))
        except HTTPError as error:
            response = error
        with response:
            elapsed = response.headers.get('X-Shop-Elapsed-Ms')
            payload = response.read(MAX_ARTIFACT + 1)
            if len(payload) > MAX_ARTIFACT:
                raise RuntimeError(f'{path} returned an oversized body')
            return Reply(
                response.status, payload, None if elapsed is None else int(elapsed),
                response.headers.get('X-Payment-Reply-Dropped') == '1',
            )

    def check(self, path: str, status: int = 200, **kwargs) -> Reply:
        started = monotonic()
        reply = self.request(path, **kwargs)
        if self.emit and not path.startswith('/control/'):
            ms = millis(started)
            self.emit(f'check {path}: HTTP {reply.status} in {ms} ms'
                      + ('' if reply.status == status else f' (expected {status})'),
                      'info' if reply.status == status else 'error', ms)
        return reply.expect(status, path)

    def log(self) -> list:
        """The VM's own log entries. Best effort after the work deadline: the VM must be read before it is terminated."""
        self.deadline = max(self.deadline, monotonic() + 6)
        entries = self.request('/control/log', timeout=5).expect(200, '/control/log').json()['entries']
        if not isinstance(entries, list):
            raise ValueError('Unexpected VM log')
        return entries

    def cart(self, cart: str) -> Reply:
        return self.check('/cart', body={'cart_id': cart, 'sku': 'cloud-mug'})

    def checkout(self, intention: str, cart: str, status: int = 200) -> Reply:
        return self.check('/checkout', status, body={'cart_id': cart}, intention=intention)


@dataclass(frozen=True)
class Rehearsal:
    """One stage: the Launch shop, or a growth migration of one option ('careful' or 'fast') and its check.
    `team` is the option id, which doubles as the schema and ledger id; `kind` is its migration kind."""

    id: str
    action: str
    team: str
    kind: str

    @classmethod
    def parse(cls, event: dict) -> 'Rehearsal':
        if not isinstance(event, dict):
            raise TypeError('Event must be an object')
        run_id, action, option = event.get('id'), event.get('action'), event.get('option')
        if not isinstance(run_id, str) or not HEX32.fullmatch(run_id):
            raise ValueError('Invalid run id')
        if action == 'launch' and set(event) == {'id', 'action'}:
            return cls(run_id, action, '', '')
        if action in ('migrate', 'compare') and set(event) == {'id', 'action', 'option'} and option in OPTIONS:
            return cls(run_id, action, option, OPTIONS[option])
        raise ValueError('Invalid action, option or unexpected event field')

    def event(self, msg: str, level: str = 'info', ms: int | None = None) -> None:
        """One evaluator line: `[EVENT] <json>` with t, level, action, option, step, msg and, for a timed step, ms."""
        if level not in LEVELS:
            raise ValueError(f'Invalid log level {level}')
        entry = {
            't': round(time() * 1000), 'level': level, 'action': self.action, 'option': self.team or None,
            'step': self.step, 'msg': msg[:200], **({} if ms is None else {'ms': ms}),
        }
        print(f'[EVENT] {json.dumps(entry, separators=(",", ":"))}', flush=True)

    def took(self, msg: str, started: float, level: str = 'info') -> None:
        self.event(f'{msg} in {seconds(started)}', level, millis(started))

    def relay(self, shop: Shop) -> None:
        """Print the VM's buffered log entries unchanged, as `[VM] <json>`, in order (MicroVM logging to CloudWatch
        is not possible). Never raises."""
        try:
            lines, dropped = relay_lines(shop.log()[-500:])
            for line in lines:
                print(line, flush=True)
            if dropped:
                self.event(f'{dropped} VM log entries were not valid and were dropped', 'warn')
        except Exception as error:
            self.event(f'VM log not read: {type(error).__name__}', 'warn')

    @property
    def root(self) -> str:
        return f'runs/{self.id}'

    @property
    def step(self) -> str:
        return f'{self.action}-{self.team}' if self.team else self.action

    def result_key(self, step: str) -> str:
        return f'{self.root}/{step}/result.json'

    def artifact_key(self, step: str, digest: str, filename: str) -> str:
        return f'{self.root}/{step}/{digest}/{filename}'

    def ledger_key(self, phase: str) -> str:
        intention = 'launch-mug' if phase == 'launch' else 'growth-mug'
        return f'{self.root}/ledger/{phase}/{intention}'

    def receipt(self, phase: str) -> bytes:
        return sha256(f'black-friday-payment:{self.id}:{phase}'.encode()).hexdigest()[:32].encode('ascii')

    def observe_charge(self, store: Store, phase: str) -> int:
        actual = store.get(self.ledger_key(phase))
        if actual is None:
            return 0
        if actual != self.receipt(phase):
            raise RuntimeError('Ledger receipt does not match the deterministic payment')
        return 1

    def verify_order(self, store: Store, shop: Shop, phase: str) -> Reply:
        launch = phase == 'launch'
        intention = 'launch-mug' if launch else 'growth-mug'
        order = shop.check(f'/orders/order-{intention}')
        receipt = store.require(self.ledger_key(phase)).decode('ascii')
        if receipt.encode('ascii') != self.receipt(phase) or order.json() != {
            'id': f'order-{intention}', 'customer_id': 'maya', 'cart_id': 'cart-launch' if launch else 'cart-saved',
            'intention': intention, 'amount_cents': PRICE, 'receipt': receipt,
        }:
            raise RuntimeError(f'Persisted order for {intention} does not match the ledger')
        return order

    def setup(self, store: Store, fleet: Fleet, deadline: float) -> dict:
        data = {'id': self.id, 'action': self.action}
        if self.team:
            data['team'] = self.team
            data['kind'] = self.kind
            step, field, filename = (
                ('launch', 'checkpoint', 'checkpoint.sqlite') if self.action == 'migrate'
                else (f'migrate-{self.team}', 'dump', 'branch.dump')
            )
            prior = json.loads(store.require(self.result_key(step)))
            started = monotonic()
            fleet.finish(prior['vm_id'], deadline)
            self.took('previous stage VM terminated', started)
            digest = prior[field]
            artifact = store.require(self.artifact_key(step, digest, filename))
            if sha256(artifact).hexdigest() != digest:
                raise RuntimeError('Input artifact SHA-256 does not match its result')
            data['input'] = base64.b64encode(artifact).decode('ascii')
        return data

    def launch(self, store: Store, shop: Shop) -> dict:
        if shop.check('/health').json().get('database') != 'sqlite':
            raise RuntimeError('Launch did not use SQLite')
        shop.check('/product')
        shop.cart('cart-launch')
        lost = shop.checkout('launch-mug', 'cart-launch', 502)
        if not lost.payment_reply_dropped:
            raise RuntimeError('First checkout did not observe a dropped payment reply')
        charged = self.observe_charge(store, 'launch')
        if charged != 1:
            raise RuntimeError('Missing ledger charge after the lost checkout response')
        shop.check('/orders/order-launch-mug', 404)
        retry = shop.checkout('launch-mug', 'cart-launch')
        shop.cart('cart-saved')
        artifact = shop.check('/control/artifact').body
        if not artifact.startswith(b'SQLite format 3\0'):
            raise RuntimeError('Invalid SQLite checkpoint')
        cart = shop.check('/cart/cart-saved')
        order = self.verify_order(store, shop, 'launch')
        digest = sha256(artifact).hexdigest()
        store.put_once(self.artifact_key(self.step, digest, 'checkpoint.sqlite'), artifact, 'application/octet-stream')
        self.event(f'artifact saved: {len(artifact) // 1024} KB (SQLite checkpoint)')
        return {'checkpoint': digest, 'launch': {
            'checkout_statuses': [lost.status, retry.status], 'charges_before_retry': charged,
            'charges': self.observe_charge(store, 'launch'), 'verified_sales': 1, 'cart_status': cart.status,
            'order_status': order.status,
        }}

    def growth(self, store: Store, shop: Shop) -> dict:
        health = shop.check('/health')
        history = self.verify_order(store, shop, 'launch')
        if health.json().get('database') != 'pgsql':
            raise RuntimeError('Growth did not use PostgreSQL')
        if self.action == 'migrate':
            artifact = shop.check('/control/artifact').body
            digest = sha256(artifact).hexdigest()
            store.put_once(self.artifact_key(self.step, digest, 'branch.dump'), artifact, 'application/octet-stream')
            self.event(f'artifact saved: {len(artifact) // 1024} KB (pg_dump)')
            return {
                'option': self.team, 'kind': self.kind, 'schema': f'bf_{self.id}_{self.team}', 'database': 'pgsql',
                'health_status': health.status, 'history_status': history.status, 'dump': digest,
            }

        # `preserve` migrated the carts, so Maya checks out; `omit` did not, so her cart is gone. That
        # missing cart is the expected result of omit, not an error, and nothing is sold or charged.
        preserve = self.kind == 'preserve'
        cart = shop.check('/cart/cart-saved', 200 if preserve else 404)
        checkout = shop.checkout('growth-mug', 'cart-saved') if preserve else None
        if checkout is not None:
            self.verify_order(store, shop, self.team)
            if checkout.elapsed is None:
                raise RuntimeError('Missing checkout timing evidence')
            sold = 1
        else:
            shop.check('/orders/order-growth-mug', 404)
            sold = 0
        charges = self.observe_charge(store, self.team)
        if charges != sold:
            raise RuntimeError('Growth ledger does not agree with verified sales')
        return {
            'option': self.team, 'kind': self.kind, 'cart_status': cart.status, 'cart_kept': cart.status == 200,
            'checkout_status': checkout.status if checkout else None,
            'checkout_ms': checkout.elapsed if checkout else None, 'charges': charges, 'verified_sales': sold,
            'health_status': health.status, 'history_status': history.status,
        }


def handler(event: dict, context) -> dict:
    stage = Rehearsal.parse(event)
    region = os.environ['AWS_REGION']
    store, fleet = Store(region), Fleet(region)
    cleanup_deadline = monotonic() + context.get_remaining_time_in_millis() / 1000 - 3
    work_deadline = cleanup_deadline - 20
    key = stage.result_key(stage.step)
    candidate = store.get(key)
    if candidate is None:
        try:
            data = stage.setup(store, fleet, work_deadline)
            if stage.action == 'launch' or (stage.action == 'compare' and stage.kind == 'preserve'):
                phase = stage.team or stage.action
                data.update(store.payment_capability(stage.ledger_key(phase), stage.receipt(phase)))
            request_budget(work_deadline, 1)
            vm = fleet.run(f'{stage.id}-{stage.step}')
        except Exception as error:
            stage.event(f'stage failed before a VM started: {type(error).__name__}', 'error')
            raise
        vm_id = vm['microvmId']
        requested = monotonic()
        stage.event(f'VM requested: {vm_id} (run {stage.id[:8]})')
        shop = None
        try:
            shop = fleet.connect(vm, work_deadline)
            shop.emit = stage.event
            stage.took('VM running, endpoint ready', requested)
            sent = monotonic()
            shop.check('/control/setup', body=data, timeout=35)
            stage.took('setup sent, VM configured', sent)
            result = stage.launch(store, shop) if stage.action == 'launch' else stage.growth(store, shop)
            # Persist the evaluated candidate before termination. A retry can finish
            # cleanup and return it without doing a new checkout or a new migration.
            store.put_once(key, json.dumps({**result, 'vm_id': vm_id}).encode(), 'application/json')
        except Exception as error:
            stage.event(f'stage failed: {type(error).__name__}', 'error')
            raise
        finally:
            if shop is not None:
                stage.relay(shop)
            terminating = monotonic()
            fleet.finish(vm_id, cleanup_deadline)
            stage.took('VM terminated', terminating)
        candidate = store.require(key)
        result = json.loads(candidate)
        if result['vm_id'] != vm_id:
            fleet.finish(result['vm_id'], cleanup_deadline)
    else:
        result = json.loads(candidate)
        fleet.finish(result['vm_id'], cleanup_deadline)
        stage.event('result already saved, VM terminated')
    result.pop('dump', None)  # The workflow exposes the branch result, not its storage details.
    return {**result, 'vm_state': 'terminated'}
