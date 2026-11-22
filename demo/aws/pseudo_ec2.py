# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3==1.44.9", "pydantic==2.13.4"]
# ///
"""Run one virtual EC2 instance backed by a reviewed Git fixture in Lambda."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qsl
from urllib.request import urlopen
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

from botocore.exceptions import BotoCoreError, ClientError
from emulator_shop import IMAGE, WAITER, emulator
from evaluator import Fleet, Shop
from operate import REGION
from submission_vm import announce, approve, approved_fleet, connect_fixture

EC2_VERSION = '2016-11-15'
VIRTUAL_TYPE = 't4g.micro'
STOCK = '000000000000'
QUOTE = re.compile(r'/quote\?quantity=[1-9][0-9]?\Z')


class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Instance:
    virtual_id: str
    reservation: str
    token: str
    microvm_id: str | None = None
    shop: Shop | None = None
    registered: bool = False
    terminated: bool = False


def state(running: bool) -> dict:
    return {'code': 16, 'name': 'running'} if running else {'code': 48, 'name': 'terminated'}


class Controller:
    def __init__(self, ec2, elb, fleet: Fleet, commit: str):
        self.ec2, self.elb, self.fleet, self.commit = ec2, elb, fleet, commit
        self.instance: Instance | None = None
        subnets = ec2.describe_subnets(Filters=[{'Name': 'default-for-az', 'Values': ['true']}])['Subnets']
        first, second = sorted(subnets, key=lambda subnet: subnet['AvailabilityZone'])[:2]
        self.subnet = first['SubnetId']
        self.name = f'black-friday-{commit[:12]}'
        self.ami = ec2.register_image(
            Name=self.name, Architecture='arm64', VirtualizationType='hvm', RootDeviceName='/dev/xvda',
            BlockDeviceMappings=[{'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 8}}],
        )['ImageId']
        ec2.get_waiter('image_available').wait(ImageIds=[self.ami], WaiterConfig=WAITER)
        alb = elb.create_load_balancer(Name='black-friday-pseudo', Subnets=[self.subnet, second['SubnetId']])
        self.group = elb.create_target_group(
            Name='black-friday-pseudo', Protocol='HTTP', Port=80, VpcId=first['VpcId'], HealthCheckPath='/health',
        )['TargetGroups'][0]['TargetGroupArn']
        elb.create_listener(
            LoadBalancerArn=alb['LoadBalancers'][0]['LoadBalancerArn'], Protocol='HTTP', Port=80,
            DefaultActions=[{'Type': 'forward', 'TargetGroupArn': self.group}],
        )

    def launch(self, ami: str, token: str) -> Instance:
        # There is exactly one approved mapping and no caller-supplied AWS ARN.
        if ami != self.ami:
            raise QueryError('InvalidAMIID.NotFound', 'Use the virtual AMI returned by describe-images')
        if self.instance is not None:
            if token == self.instance.token and self.instance.shop is not None:
                return self.instance
            raise QueryError('InstanceLimitExceeded', 'This fixture session admits one launch; start a new session')
        reservation = self.ec2.run_instances(
            ImageId=ami, InstanceType=VIRTUAL_TYPE, MinCount=1, MaxCount=1, ClientToken=token, SubnetId=self.subnet,
        )
        node = Instance(reservation['Instances'][0]['InstanceId'], reservation['ReservationId'], token)
        self.instance = node
        try:
            self.ec2.get_waiter('instance_running').wait(InstanceIds=[node.virtual_id], WaiterConfig=WAITER)
            actual = self.ec2.describe_instances(InstanceIds=[node.virtual_id])['Reservations'][0]['Instances'][0]
            if actual['ImageId'] != self.ami:
                raise RuntimeError('Emulator instance did not boot from the approved virtual AMI')
            vm = self.fleet.run(node.virtual_id)
            node.microvm_id = vm['microvmId']
            shop = connect_fixture(self.fleet, vm)
            # Only publish after authenticated guest readiness and registration both succeed.
            self.elb.register_targets(TargetGroupArn=self.group, Targets=[{'Id': node.virtual_id}])
            node.registered = True
            node.shop = shop
            announce(f'{node.virtual_id} uses {ami} → {node.microvm_id} (ready, no execution role)')
            return node
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        node = self.instance
        if node is None or node.terminated:
            return
        # Disable routing before doing network I/O, including on an incomplete teardown.
        node.shop = None
        try:
            if node.registered:
                self.elb.deregister_targets(TargetGroupArn=self.group, Targets=[{'Id': node.virtual_id}])
                node.registered = False
        finally:
            try:
                if node.microvm_id is not None:
                    self.fleet.finish(node.microvm_id, monotonic() + 30)
                    announce(f'Real MicroVM terminated: {node.microvm_id}')
            finally:
                self.ec2.terminate_instances(InstanceIds=[node.virtual_id])
                self.ec2.get_waiter('instance_terminated').wait(InstanceIds=[node.virtual_id], WaiterConfig=WAITER)
        node.terminated = True
        announce(f'Virtual instance terminated: {node.virtual_id}')

    def describe(self, node: Instance) -> dict:
        if node.shop is not None and monotonic() >= node.shop.deadline:
            self.close()
        return {'instanceId': node.virtual_id, 'imageId': self.ami, 'instanceType': VIRTUAL_TYPE,
                'instanceState': state(not node.terminated)}

    def query(self, action: str, fields: dict[str, str]) -> dict:
        node = self.instance
        if action == 'DescribeImages' and fields in ({}, {'ImageId.1': self.ami}):
            return {'imagesSet': [{'imageId': self.ami, 'imageState': 'available', 'name': self.name,
                                   'architecture': 'arm64', 'imageOwnerId': STOCK,
                                   'description': f'Reviewed Git commit {self.commit}'}]}
        if action == 'RunInstances':
            token = fields.pop('ClientToken', None) or uuid4().hex
            if fields.keys() == {'ImageId', 'InstanceType', 'MinCount', 'MaxCount'} and (
                fields['InstanceType'], fields['MinCount'], fields['MaxCount'],
            ) == (VIRTUAL_TYPE, '1', '1') and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', token):
                launched = self.launch(fields['ImageId'], token)
                return {'reservationId': launched.reservation, 'ownerId': STOCK,
                        'instancesSet': [self.describe(launched)]}
        if action == 'DescribeInstances' and not fields:
            return {'reservationSet': [] if node is None else [
                {'reservationId': node.reservation, 'ownerId': STOCK, 'instancesSet': [self.describe(node)]},
            ]}
        if action in ('DescribeInstances', 'TerminateInstances') and fields.keys() == {'InstanceId.1'}:
            if node is None or node.virtual_id != fields['InstanceId.1']:
                raise QueryError('InvalidInstanceID.NotFound', 'Unknown virtual instance ID')
            if action == 'DescribeInstances':
                return {'reservationSet': [{'reservationId': node.reservation, 'ownerId': STOCK,
                                            'instancesSet': [self.describe(node)]}]}
            previous = state(not node.terminated)
            self.close()
            return {'instancesSet': [{'instanceId': node.virtual_id, 'previousState': previous,
                                      'currentState': state(False)}]}
        raise QueryError('InvalidParameterValue', 'Unsupported command or fields; see the pseudo-EC2 runbook')

    def route(self, path: str) -> tuple[int, bytes]:
        node = self.instance
        if node is not None and node.shop is not None:
            try:
                reply = node.shop.request('/ready' if path == '/health' else path)
                if reply.status == 200:
                    return 200, reply.body
            except (OSError, ValueError, RuntimeError):
                pass
        return 503, b'{"error":"No healthy registered MicroVM"}'


def xml_fields(root: Element, values: dict) -> None:
    for key, value in values.items():
        child = SubElement(root, key)
        if isinstance(value, dict):
            xml_fields(child, value)
        elif isinstance(value, list):
            for item in value:
                xml_fields(SubElement(child, 'item'), item)
        else:
            child.text = str(value)


def parse_query(body: bytes) -> tuple[str, dict[str, str]]:
    pairs = parse_qsl(body.decode('ascii'), keep_blank_values=True, strict_parsing=True, max_num_fields=12)
    fields = dict(pairs)
    if len(fields) != len(pairs) or fields.pop('Version', '') != EC2_VERSION:
        raise ValueError('Invalid EC2 Query request')
    return fields.pop('Action', ''), fields


def server_for(port: int, controller: Controller) -> HTTPServer:
    class Gateway(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, format, *args) -> None:
            pass

        def local(self) -> bool:
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}' and 'Origin' not in self.headers

        def reply(self, status: int, body: bytes, media: str = 'application/json') -> None:
            self.send_response(status)
            self.send_header('Content-Type', media)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def xml(self, name: str, body: dict, status: int) -> None:
            root = Element(name, {'xmlns': f'http://ec2.amazonaws.com/doc/{EC2_VERSION}/'})
            xml_fields(root, {'requestId': uuid4().hex, **body})
            self.reply(status, tostring(root), 'text/xml')

        def error(self, code: str, text: str, status: int = 400) -> None:
            root = Element('Response')
            xml_fields(root, {'Errors': {'Error': {'Code': code, 'Message': text}}, 'RequestID': uuid4().hex})
            self.reply(status, tostring(root), 'text/xml')

        def do_GET(self) -> None:
            if not self.local():
                self.send_error(403)
            elif self.path == '/health' or QUOTE.fullmatch(self.path):
                self.reply(*controller.route(self.path))
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path != '/' or not self.local():
                self.send_error(403)
                return
            lengths = self.headers.get_all('Content-Length', [])
            if (len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,4}', lengths[0])
                    or not 0 < int(lengths[0]) <= 4096 or 'Transfer-Encoding' in self.headers):
                self.error('InvalidParameterValue', 'Expected a bounded EC2 Query body')
                return
            try:
                action, fields = parse_query(self.rfile.read(int(lengths[0])))
                result = controller.query(action, fields)
            except QueryError as error:
                self.error(error.code, str(error))
                return
            except ValueError:
                self.error('InvalidParameterValue', 'Invalid EC2 Query request')
                return
            except (BotoCoreError, ClientError, RuntimeError, OSError) as error:
                announce(f'Pseudo-EC2 operation failed: {type(error).__name__}: {error}')
                self.error('InternalError', 'Provisioning or teardown failed; see the operator terminal', 500)
                return
            self.xml(f'{action}Response', result, 200)

    return HTTPServer(('127.0.0.1', port), Gateway)


@contextmanager
def serving(server: HTTPServer):
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join()


def proof(base: str, controller: Controller) -> dict:
    def cli(*args: str, rejected: str | None = None) -> dict:
        env = {**os.environ, 'AWS_CONFIG_FILE': '/dev/null', 'AWS_SHARED_CREDENTIALS_FILE': '/dev/null',
               'AWS_EC2_METADATA_DISABLED': 'true'}
        command = ['aws', '--no-sign-request', '--no-cli-pager', '--output', 'json', '--region', REGION,
                   '--endpoint-url', base, '--cli-read-timeout', '125', 'ec2', *args]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=135, check=False)
        if rejected is not None:
            if result.returncode == 0 or rejected not in result.stderr:
                raise RuntimeError(f'Expected CLI rejection {rejected}: {result.stderr.strip()}')
            return {}
        if result.returncode:
            raise RuntimeError(f'AWS CLI {args[0]} failed: {result.stderr.strip()}')
        return json.loads(result.stdout)

    def get(path: str, expected: int) -> dict:
        try:
            response = urlopen(base + path, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            if response.status != expected:
                raise RuntimeError(f'{path}: expected {expected}, got {response.status}')
            return json.load(response)

    ami, = cli('describe-images')['Images']
    if ami['ImageId'] != controller.ami:
        raise RuntimeError('The CLI did not see the registered virtual AMI')
    get('/health', 503)
    cli('run-instances', '--image-id', 'ami-fffffffffffffffff', '--instance-type', VIRTUAL_TYPE,
        '--count', '1', rejected='InvalidAMIID.NotFound')
    if cli('describe-instances')['Reservations']:
        raise RuntimeError('The rejected AMI created a virtual reservation')
    node, = cli('run-instances', '--image-id', ami['ImageId'], '--instance-type', VIRTUAL_TYPE,
                '--count', '1', '--client-token', 'reviewed-fixture')['Instances']
    identity = node['InstanceId']
    if node['State']['Name'] != 'running' or node['ImageId'] != controller.ami:
        raise RuntimeError('The CLI did not get the healthy mapped instance')
    quote = get('/quote?quantity=3', 200)
    if quote != {'sku': 'cloud-mug', 'quantity': 3, 'total_cents': 7500}:
        raise RuntimeError('The reviewed release did not compute the expected quote')
    terminated, = cli('terminate-instances', '--instance-ids', identity)['TerminatingInstances']
    if terminated['CurrentState']['Name'] != 'terminated':
        raise RuntimeError('The CLI did not observe termination')
    get('/quote?quantity=3', 503)
    actual = controller.instance
    if actual is None or not actual.microvm_id:
        raise RuntimeError('No real MicroVM was mapped to the virtual instance')
    return {'commit': controller.commit, 'virtual_ami': controller.ami, 'virtual_instance': identity,
            'microvm': actual.microvm_id, 'quote': quote, 'after_termination_http': 503}


def stop(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--emulator-image', default=IMAGE)
    parser.add_argument('--serve', action='store_true', help='Leave the loopback endpoint open for presenter CLI commands')
    parser.add_argument('--port', type=int, default=0, help='Loopback port (0 chooses a free port)')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    try:
        artifact = approve(args.manifest, args.archive)
        with (
            approved_fleet(artifact) as fleet,
            TemporaryDirectory(prefix='black-friday-pseudo-') as work,
            emulator(args.emulator_image, Path(work)) as (local, options),
            closing(local.client('ec2', **options)) as ec2,
            closing(local.client('elbv2', **options)) as elb,
        ):
            controller = Controller(ec2, elb, fleet, artifact.commit)
            try:
                with server_for(args.port, controller) as server:
                    if args.serve:
                        announce(f'Pseudo-EC2 ready: http://127.0.0.1:{server.server_port} · AMI {controller.ami}')
                        server.serve_forever()
                    else:
                        with serving(server) as base:
                            result = proof(base, controller)
            finally:
                controller.close()
        if not args.serve:
            print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, EOFError, tarfile.TarError, RuntimeError, BotoCoreError, ClientError,
            subprocess.SubprocessError) as error:
        announce(f'Pseudo-EC2: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
