"""AWS Event evaluator: referee the trusted PHP shop in fresh roleless MicroVMs."""

import base64
import json
import os
import re
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import scoring

MAX_ARTIFACT = 1024 * 1024
VM_LIFESPAN = 120
PRICE = 2500
MARGIN = scoring.MARGIN
LAUNCH_COST = scoring.LAUNCH_COST
RETURN_DRAW = scoring.RETURN_DRAW
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
            raise RuntimeError(f'Missing prerequisite: {key}')
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
    def __init__(self, endpoint: str, token: str, deadline: float) -> None:
        self.base = endpoint.rstrip('/')
        self.token = token
        self.deadline = deadline

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
        return self.request(path, **kwargs).expect(status, path)

    def cart(self, cart: str) -> Reply:
        return self.check('/cart', body={'cart_id': cart, 'sku': 'cloud-mug'})

    def checkout(self, intention: str, cart: str, status: int = 200) -> Reply:
        return self.check('/checkout', status, body={'cart_id': cart}, intention=intention)


@dataclass(frozen=True)
class Rehearsal:
    id: str
    action: str
    team: str
    kind: str
    architecture: dict | None = None

    @classmethod
    def parse(cls, event: dict) -> 'Rehearsal':
        if not isinstance(event, dict):
            raise TypeError('Event must be an object')
        run_id, action = event.get('id'), event.get('action')
        team, kind = event.get('team'), event.get('kind')
        if not isinstance(run_id, str) or not HEX32.fullmatch(run_id):
            raise ValueError('Invalid run id')
        if action == 'launch' and set(event) == {'id', 'action'}:
            return cls(run_id, action, '', '')
        team_ok = isinstance(team, str) and scoring.TEAM_ID.fullmatch(team) is not None and kind in scoring.KINDS
        if team_ok and action == 'migrate' and set(event) == {'id', 'action', 'team', 'kind'}:
            return cls(run_id, action, team, kind)
        if (
            team_ok and action == 'compare' and set(event) == {'id', 'action', 'team', 'kind', 'architecture'}
            and scoring.valid_architecture(event['architecture'])
        ):
            return cls(run_id, action, team, kind, event['architecture'])
        raise ValueError('Invalid action, team, migration kind or unexpected event field')

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
            fleet.finish(prior['vm_id'], deadline)
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
        return {'checkpoint': digest, 'launch': {
            'checkout_statuses': [lost.status, retry.status], 'charges_before_retry': charged,
            'charges': self.observe_charge(store, 'launch'), 'verified_sales': 1, 'cart_status': cart.status,
            'order_status': order.status, 'profit_cents': MARGIN - LAUNCH_COST,
        }}

    def capacity(self) -> dict:
        """Score this team's architecture on the Virtual RDS wave. Pure Python: no MicroVM involved."""
        from virtual_rds import Plan, run_round

        plan = Plan(self.architecture['storage'], self.architecture['replicas'], self.architecture['cache'])
        result, _ = run_round(plan, RETURN_DRAW)
        return {
            'storage': plan.storage, 'replicas': plan.replicas, 'cache': plan.cached,
            'verified_sales': result['verified_sales'], 'wave_verified_sales': result['wave_verified_sales'],
            'wave_storage_rejections': result['wave_storage_rejections'],
            'cost_cents': result['modeled_cost_cents'], 'profit_cents': result['profit_cents'],
        }

    def growth(self, store: Store, shop: Shop, architecture: dict | None) -> dict:
        health = shop.check('/health')
        history = self.verify_order(store, shop, 'launch')
        if health.json().get('database') != 'pgsql':
            raise RuntimeError('Growth did not use PostgreSQL')
        branch = {
            'kind': self.kind, 'schema': f'bf_{self.id}_{self.team}', 'database': 'pgsql',
            'health_status': health.status, 'history_status': history.status,
        }
        if self.action == 'migrate':
            artifact = shop.check('/control/artifact').body
            digest = sha256(artifact).hexdigest()
            store.put_once(self.artifact_key(self.step, digest, 'branch.dump'), artifact, 'application/octet-stream')
            return {**branch, 'dump': digest}

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
        probability = scoring.return_probability(self.kind, checkout.elapsed if checkout else None)
        charges = self.observe_charge(store, self.team)
        if charges != sold:
            raise RuntimeError('Growth ledger does not agree with verified sales')
        maya = scoring.maya_sales(sold, probability)
        return {**branch, 'cart_status': cart.status, 'checkout_status': checkout.status if checkout else None,
                'checkout_ms': checkout.elapsed if checkout else None, 'charges': charges, 'verified_sales': sold,
                'return_probability': probability, 'returns_next_week': RETURN_DRAW < probability,
                'maya_sales': maya, 'architecture': architecture,
                'migration_cost_cents': scoring.KINDS[self.kind],
                'profit_cents': scoring.growth_profit(architecture['profit_cents'], maya, self.kind)}


def handler(event: dict, context) -> dict:
    stage = Rehearsal.parse(event)
    region = os.environ['AWS_REGION']
    store, fleet = Store(region), Fleet(region)
    cleanup_deadline = monotonic() + context.get_remaining_time_in_millis() / 1000 - 3
    work_deadline = cleanup_deadline - 20
    key = stage.result_key(stage.step)
    candidate = store.get(key)
    if candidate is None:
        architecture = stage.capacity() if stage.action == 'compare' else None
        data = stage.setup(store, fleet, work_deadline)
        if stage.action == 'launch' or (stage.action == 'compare' and stage.kind == 'preserve'):
            phase = stage.team or stage.action
            data.update(store.payment_capability(stage.ledger_key(phase), stage.receipt(phase)))
        request_budget(work_deadline, 1)
        vm = fleet.run(f'{stage.id}-{stage.step}')
        vm_id = vm['microvmId']
        try:
            shop = fleet.connect(vm, work_deadline)
            shop.check('/control/setup', body=data, timeout=35)
            result = stage.launch(store, shop) if stage.action == 'launch' else stage.growth(store, shop, architecture)
            # Persist the evaluated candidate before termination. A retry can finish
            # cleanup and return it without doing a new checkout or a new migration.
            store.put_once(key, json.dumps({**result, 'vm_id': vm_id}).encode(), 'application/json')
        finally:
            fleet.finish(vm_id, cleanup_deadline)
        candidate = store.require(key)
        result = json.loads(candidate)
        if result['vm_id'] != vm_id:
            fleet.finish(result['vm_id'], cleanup_deadline)
    else:
        result = json.loads(candidate)
        fleet.finish(result['vm_id'], cleanup_deadline)
    result.pop('dump', None)  # The workflow exposes the branch result, not its storage details.
    return {**result, 'vm_state': 'terminated'}
