# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3==1.44.9"]
# ///
"""Prove a shop checkout with stock AWS Emulator inventory and a real Lambda MicroVM."""

import argparse
import configparser
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3
from botocore.config import Config
from evaluator import VM_LIFESPAN, Fleet, Rehearsal, Shop, Store, pause
from operate import REGION, STACK, account_session, stack_outputs

IMAGE = 'black-friday-aws-emulator:1.0.556'
EMULATOR_HOME = '/run/emulator/.aws/emulator/9297'
WAITER = {'Delay': 1, 'MaxAttempts': 15}
SHOP_GET = frozenset(('/health', '/product', '/cart/cart-saved', '/orders/order-launch-mug'))


def docker(*args: str) -> bytes:
    return subprocess.check_output(['docker', *args], timeout=20)


@contextmanager
def emulator(image: str, directory: Path) -> Iterator[tuple[boto3.Session, dict]]:
    container = f'black-friday-hybrid-{uuid4().hex[:10]}'
    docker(
        'run', '--detach', '--rm', '--name', container, '--memory=2g', '--memory-swap=2g',
        '--tmpfs', '/run/emulator:rw,noexec,nosuid,uid=1000,gid=1000,mode=0700',
        '--publish', '127.0.0.1::9297', image, '--bind', '0.0.0.0', '--telemetry', 'off',
    )
    try:
        address = docker('port', container, '9297/tcp').decode().strip()
        if not address.startswith('127.0.0.1:') or not address.removeprefix('127.0.0.1:').isdigit():
            raise RuntimeError('Emulator did not publish a loopback TCP port')
        config = configparser.ConfigParser()
        deadline = monotonic() + 10
        while True:
            cert = subprocess.run(
                ['docker', 'exec', container, 'dd', f'if={EMULATOR_HOME}/ca.pem', 'status=none'],
                check=False, capture_output=True, timeout=3,
            )
            if cert.returncode == 0 and cert.stdout.endswith(b'-----END CERTIFICATE-----\n'):
                break
            pause(deadline)
        ca = directory / 'ca.pem'
        ca.write_bytes(cert.stdout)
        # Docker's copy API cannot read a tmpfs. Stream only into process memory;
        # no emulated keys are mounted on the host or sent to real AWS clients.
        credentials = docker('exec', container, 'dd', f'if={EMULATOR_HOME}/emulator_credentials', 'status=none')
        config.read_string(credentials.decode())
        profile = config['default']
        if not profile['aws_access_key_id'].startswith('EMULATOR'):
            raise RuntimeError('Expected generated AWS Emulator credentials')
        session = boto3.Session(
            aws_access_key_id=profile['aws_access_key_id'],
            aws_secret_access_key=profile['aws_secret_access_key'],
            region_name=REGION,
        )
        options = {
            'endpoint_url': f'https://{address}', 'verify': str(ca),
            'config': Config(connect_timeout=2, read_timeout=5, retries={'total_max_attempts': 1}),
        }
        with closing(session.client('sts', **options)) as sts:
            if sts.get_caller_identity()['Account'] != '000000000000':
                raise RuntimeError('Virtual AWS account check failed')
        yield session, options
    finally:
        docker('stop', '--timeout', '3', container)


@contextmanager
def microvm(fleet: Fleet, identity: str, setup: dict) -> Iterator[Shop]:
    vm = fleet.run(identity)
    vm_id = vm['microvmId']
    try:
        shop = fleet.connect(vm, monotonic() + VM_LIFESPAN - 20)
        state = fleet.get(vm_id)
        if state.get('executionRoleArn'):
            raise RuntimeError('The shop MicroVM unexpectedly has an execution role')
        shop.check('/control/setup', body=setup, timeout=35)
        print(f'{identity} → real MicroVM {vm_id} (no execution role)', flush=True)
        yield shop
    finally:
        fleet.finish(vm_id, monotonic() + 25)
        print(f'Real MicroVM {vm_id}: TERMINATED', flush=True)


@contextmanager
def ingress(elb, group: str, instance_id: str, shop: Shop) -> Iterator[str]:
    class Gateway(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, format, *args) -> None:
            pass

        def local(self) -> bool:
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}' and 'Origin' not in self.headers

        def do_GET(self) -> None:
            if not self.local():
                self.send_error(403)
                return
            if self.path not in SHOP_GET:
                self.send_error(404)
                return
            self.forward()

        def do_POST(self) -> None:
            if not self.local():
                self.send_error(403)
                return
            length = self.headers.get('Content-Length', '')
            if self.path not in ('/cart', '/checkout'):
                self.send_error(404)
                return
            if not length.isascii() or not length.isdecimal() or len(length) > 4 or not 0 < int(length) <= 1024:
                self.send_error(400)
                return
            try:
                body = json.loads(self.rfile.read(int(length)))
            except ValueError:
                self.send_error(400)
                return
            if not isinstance(body, dict):
                self.send_error(400)
                return
            self.forward(body)

        def forward(self, body: dict | None = None) -> None:
            targets = elb.describe_target_health(TargetGroupArn=group)['TargetHealthDescriptions']
            if not any(t['Target']['Id'] == instance_id and t['TargetHealth']['State'] != 'draining' for t in targets):
                self.send_error(503, 'No registered real shop')
                return
            try:
                reply = shop.request(self.path, body=body, intention=self.headers.get('Idempotency-Key', ''))
            except OSError:
                self.send_error(502, 'Real MicroVM did not respond')
                return
            self.send_response(reply.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(reply.body)))
            if reply.payment_reply_dropped:
                self.send_header('X-Payment-Reply-Dropped', '1')
            self.end_headers()
            self.wfile.write(reply.body)

    # Publish only on loopback; the real shop's ingress token stays in this process.
    with HTTPServer(('127.0.0.1', 0), Gateway) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join()


def respond(
    base: str, path: str, *, body: dict | None = None, intention: str = '',
    expected: int = 200, dropped: bool = False,
) -> dict:
    headers = {'Content-Type': 'application/json'} if body is not None else {}
    if intention:
        headers['Idempotency-Key'] = intention
    request = Request(base + path, None if body is None else json.dumps(body).encode(), headers)
    try:
        reply = urlopen(request, timeout=15)
    except HTTPError as error:
        reply = error
    with reply:
        if reply.status != expected:
            raise RuntimeError(f'{path}: expected HTTP {expected}, got HTTP {reply.status}')
        if dropped and reply.headers.get('X-Payment-Reply-Dropped') != '1':
            raise RuntimeError('Did not observe the deliberately lost payment reply')
        return json.load(reply) if expected == 200 else {}


def exercise(ec2, elb, fleet: Fleet, store: Store) -> None:
    run = uuid4().hex
    rehearsal = Rehearsal(run, 'launch', '')
    subnets = ec2.describe_subnets(Filters=[{'Name': 'default-for-az', 'Values': ['true']}])['Subnets']
    first, second = sorted(subnets, key=lambda subnet: subnet['AvailabilityZone'])[:2]
    images = ec2.describe_images(Owners=['amazon'], Filters=[
        {'Name': 'architecture', 'Values': ['arm64']},
        {'Name': 'name', 'Values': ['al2023-ami-*-kernel-*-arm64']},
    ])['Images']
    image = min(images, key=lambda item: item['ImageId'])['ImageId']
    load_balancer = elb.create_load_balancer(Name='black-friday', Subnets=[first['SubnetId'], second['SubnetId']])
    group = elb.create_target_group(
        Name='black-friday-shop', Protocol='HTTP', Port=80, VpcId=first['VpcId'], HealthCheckPath='/health',
    )['TargetGroups'][0]['TargetGroupArn']
    elb.create_listener(
        LoadBalancerArn=load_balancer['LoadBalancers'][0]['LoadBalancerArn'],
        Protocol='HTTP', Port=80, DefaultActions=[{'Type': 'forward', 'TargetGroupArn': group}],
    )
    print('AWS Emulator: created the virtual ALB, HTTP listener, and target group.', flush=True)
    virtual = ec2.run_instances(
        ImageId=image, InstanceType='t4g.micro', MinCount=1, MaxCount=1,
        SubnetId=first['SubnetId'], ClientToken=run,
    )['Instances'][0]['InstanceId']
    target = [{'Id': virtual}]
    try:
        ec2.get_waiter('instance_running').wait(InstanceIds=[virtual], WaiterConfig=WAITER)
        print(f'AWS Emulator: {virtual} running', flush=True)
        setup = {'id': run, 'action': 'launch'}
        setup.update(store.payment_capability(rehearsal.ledger_key('launch'), rehearsal.receipt('launch')))
        with microvm(fleet, virtual, setup) as shop, ingress(elb, group, virtual, shop) as base:
            respond(base, '/health', expected=503)
            elb.register_targets(TargetGroupArn=group, Targets=target)
            if respond(base, '/health').get('database') != 'sqlite':
                raise RuntimeError('Expected the PHP/SQLite Launch shop')
            respond(base, '/product')
            respond(base, '/cart', body={'cart_id': 'cart-launch', 'sku': 'cloud-mug'})
            checkout = {'body': {'cart_id': 'cart-launch'}, 'intention': 'launch-mug'}
            respond(base, '/checkout', **checkout, expected=502, dropped=True)
            if rehearsal.observe_charge(store, 'launch') != 1:
                raise RuntimeError('Payment was not recorded before the lost reply')
            respond(base, '/orders/order-launch-mug', expected=404)
            order = respond(base, '/checkout', **checkout)
            respond(base, '/cart', body={'cart_id': 'cart-saved', 'sku': 'cloud-mug'})
            shop.check('/control/artifact')  # Seals SQLite and restarts PHP before the readback.
            if (
                order != respond(base, '/orders/order-launch-mug')
                or order != rehearsal.verify_order(store, shop, 'launch').json()
                or respond(base, '/cart/cart-saved').get('id') != 'cart-saved'
            ):
                raise RuntimeError('Restarted PHP did not return the persisted cart and verified order')
            versions = store.client.list_object_versions(Bucket=store.bucket, Prefix=rehearsal.ledger_key('launch'))
            if len(versions.get('Versions', [])) != 1 or versions.get('DeleteMarkers') or versions.get('IsTruncated'):
                raise RuntimeError('Expected exactly one write-once payment object version')
            print(f'Checkout 502 → 200: one S3 charge; restarted PHP returned {order["id"]} and the saved cart.', flush=True)
            elb.deregister_targets(TargetGroupArn=group, Targets=target)
            respond(base, '/health', expected=503)
            print('HTTP 503: the bridge stopped routing after AWS Emulator deregistered the target.', flush=True)
    finally:
        ec2.terminate_instances(InstanceIds=[virtual])
        ec2.get_waiter('instance_terminated').wait(InstanceIds=[virtual], WaiterConfig=WAITER)
        print(f'AWS Emulator: {virtual} terminated', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default=IMAGE, help='Local Docker image built from aws/Emulator.Dockerfile')
    args = parser.parse_args()
    # Real clients must not inherit an emulator endpoint from the operator's shell.
    os.environ['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'] = 'true'
    session = account_session()
    outputs = stack_outputs(session, STACK)
    os.environ.update(
        ARTIFACT_BUCKET=outputs['ArtifactBucket'], IMAGE_ARN=outputs['ImageArn'], IMAGE_VERSION=outputs['ImageVersion'],
    )
    boto3.DEFAULT_SESSION = session  # Existing Fleet and Store use boto3's default session.
    with TemporaryDirectory(prefix='black-friday-hybrid-') as folder:
        directory = Path(folder)
        with (
            emulator(args.image, directory) as (local, options),
            closing(local.client('ec2', **options)) as ec2,
            closing(local.client('elbv2', **options)) as elb,
        ):
            exercise(ec2, elb, Fleet(REGION), Store(REGION))


if __name__ == '__main__':
    main()
