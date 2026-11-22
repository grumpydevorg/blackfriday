"""AWS Query API for a disposable local cloud running the trusted BLACK FRIDAY shop."""

import logging
import os
import re
import signal
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

from arena import APP, CUSTOMER, Handler, Ledger, Payments, request, shop

LOGGER = logging.getLogger(__name__)
REGION = 'us-west-2'
ACCOUNT = '000000000000'
IMAGE = 'ami-00000000000000001'
INSTANCE_TYPE = 't4g.micro'
DB_CLASS = 'db.t4g.micro'
DB_HOST = 'postgres'
DATABASE_TAG = 'BlackFridayDatabase'
NAME = re.compile('[a-z][a-z0-9]*(?:-[a-z0-9]+)*')
SHOP_ROUTE = re.compile(r'/(?:health|product|cart|checkout|(?:cart|orders)/[a-z0-9-]+)')


class QueryError(Exception):
    def __init__(self, message: str, code: str = 'InvalidParameterValue', status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Query:
    def __init__(self, form: dict[str, list[str]]) -> None:
        if any(len(values) != 1 for values in form.values()):
            raise QueryError('Repeated query parameter')
        self.fields = {key: values[0] for key, values in form.items()}

    def take(self, key: str, default: str | None = None) -> str:
        value = self.fields.pop(key, default)
        if not value:
            raise QueryError(f'{key} is required')
        return value

    def expect(self, key: str, expected: str, *, optional: bool = False) -> None:
        if self.take(key, expected if optional else None) != expected:
            raise QueryError(f'{key} must be {expected}')

    def name(self, key: str) -> str:
        value = self.take(key)
        if len(value) > 32 or not NAME.fullmatch(value):
            raise QueryError(f'{key} must be a 1–32 character lowercase resource name')
        return value

    def members(self, prefix: str, suffix: str = '') -> list[str]:
        values = []
        while (key := f'{prefix}.{len(values) + 1}{suffix}') in self.fields:
            values.append(self.take(key))
        return values

    def end(self) -> None:
        if self.fields:
            raise QueryError(f'Unsupported query parameters: {", ".join(sorted(self.fields))}')


@dataclass(frozen=True)
class Members:
    tag: str
    values: list[dict[str, 'XmlValue']]


type XmlValue = str | int | dict[str, XmlValue] | Members


def xml_fields(element: Element, fields: dict[str, XmlValue]) -> None:
    for key, value in fields.items():
        child = SubElement(element, key)
        if isinstance(value, Members):
            for member in value.values:
                xml_fields(SubElement(child, value.tag), member)
        elif isinstance(value, dict):
            xml_fields(child, value)
        else:
            child.text = str(value)


def describe_state(running: bool) -> dict[str, XmlValue]:
    return {'code': 16 if running else 48, 'name': 'running' if running else 'terminated'}


@dataclass
class Instance:
    id: str
    base: str
    runtime: ExitStack
    database: str | None
    running: bool = True

    def describe(self) -> dict[str, XmlValue]:
        result: dict[str, XmlValue] = {
            'instanceId': self.id, 'imageId': IMAGE, 'instanceType': INSTANCE_TYPE,
            'instanceState': describe_state(self.running),
        }
        if self.database is not None:
            result['tagSet'] = Members('item', [{'key': DATABASE_TAG, 'value': self.database}])
        return result

    def healthy(self) -> bool:
        if not self.running:
            return False
        try:
            return request(self.base, '/health').status == 200
        except (OSError, HTTPException, ValueError):
            return False


@dataclass(frozen=True)
class Database:
    name: str
    scope: str

    @property
    def database(self) -> str:
        return f'bf_{self.scope}'

    @property
    def schema(self) -> str:
        return f'bf_{self.scope}_preserve'

    def describe(self, status: str = 'available') -> dict[str, XmlValue]:
        return {
            'DBInstanceIdentifier': self.name, 'DBInstanceStatus': status,
            'DBInstanceClass': DB_CLASS, 'Engine': 'postgres', 'DBName': self.database,
            'MasterUsername': 'demo', 'Endpoint': {'Address': DB_HOST, 'Port': 5432},
        }


@dataclass
class TargetGroup:
    arn: str
    name: str
    targets: dict[str, Instance] = field(default_factory=dict)
    cursor: int = 0

    def describe(self) -> dict[str, XmlValue]:
        return {
            'TargetGroupArn': self.arn, 'TargetGroupName': self.name, 'Protocol': 'HTTP',
            'Port': 80, 'TargetType': 'instance', 'HealthCheckPath': '/health',
            'LoadBalancerArns': Members('member', []),
        }

    def pick(self) -> Instance | None:
        running = [target for target in self.targets.values() if target.running]
        for _ in running:
            self.cursor %= len(running)
            target = running[self.cursor]
            self.cursor += 1
            if target.healthy():
                return target
        return None


def seed_sqlite(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript((APP / 'schema.sql').read_text())
        db.execute('INSERT INTO customers VALUES (?, ?)', (CUSTOMER, 'Maya'))


def database_admin(statement: str) -> None:
    # Instance DDL only uses generated identifiers, never request text.
    subprocess.run(
        ['php', '-r', 'require "db.php"; postgres()->exec($argv[1]);', statement],
        cwd=APP, env={**os.environ, 'PGHOST': DB_HOST, 'PGDATABASE': 'postgres'},
        check=True, timeout=10,
    )


class Cloud:
    def __init__(self, path: Path, payment_url: str, runtimes: ExitStack) -> None:
        self.path = path
        self.payment_url = payment_url
        self.runtimes = runtimes
        self.lock = Lock()
        self.databases: dict[str, Database] = {}
        self.instances: dict[str, Instance] = {}
        self.reservations: dict[str, list[Instance]] = {}
        self.tokens: dict[str, tuple[tuple[int, str | None], str]] = {}
        self.balancer: tuple[str, str] | None = None
        self.groups: dict[str, TargetGroup] = {}
        self.listener: tuple[str, TargetGroup] | None = None

    def reservation(self, identity: str, selected: list[Instance]) -> dict[str, XmlValue]:
        return {
            'reservationId': identity, 'ownerId': ACCOUNT,
            'instancesSet': Members('item', [instance.describe() for instance in selected]),
        }

    def instance(self, identity: str) -> Instance:
        if identity not in self.instances:
            raise QueryError(f'Unknown virtual instance: {identity}', 'InvalidInstanceID.NotFound')
        return self.instances[identity]

    def run_instances(self, q: Query) -> dict[str, XmlValue]:
        q.expect('ImageId', IMAGE)
        q.expect('InstanceType', INSTANCE_TYPE)
        count = q.take('MinCount')
        if count not in ('1', '2', '3', '4') or q.take('MaxCount') != count:
            raise QueryError('Use --count 1 through 4 (exact count)')
        token = q.take('ClientToken') if 'ClientToken' in q.fields else None
        db_name = None
        if 'TagSpecification.1.ResourceType' in q.fields:
            q.expect('TagSpecification.1.ResourceType', 'instance')
            q.expect('TagSpecification.1.Tag.1.Key', DATABASE_TAG)
            db_name = q.name('TagSpecification.1.Tag.1.Value')
        q.end()
        spec = (int(count), db_name)
        if token is not None and token in self.tokens:
            previous, reservation = self.tokens[token]
            if spec != previous:
                raise QueryError('ClientToken was used with different launch parameters', 'IdempotentParameterMismatch')
            return self.reservation(reservation, self.reservations[reservation])
        db = self.databases.get(db_name) if db_name is not None else None
        if db_name is not None and db is None:
            raise QueryError(f'Create virtual RDS database {db_name} before launching its shop')
        if sum(i.running for i in self.instances.values()) + spec[0] > 4:
            raise QueryError('At most four virtual instances can run at once', 'InstanceLimitExceeded')
        if len(self.reservations) >= 32:
            raise QueryError('Reservation limit reached; start a fresh simulator session', 'InstanceLimitExceeded')
        created = []
        with ExitStack() as launch:
            for _ in range(spec[0]):
                identity = uuid4().hex
                runtime = launch.enter_context(ExitStack())
                work = Path(runtime.enter_context(TemporaryDirectory(dir=self.path)))
                if db is None:
                    sqlite_path = work / 'shop.sqlite'
                    seed_sqlite(sqlite_path)
                    base = runtime.enter_context(shop(self.payment_url, f'{identity}:preserve', sqlite=sqlite_path))
                else:
                    base = runtime.enter_context(shop(
                        self.payment_url, f'{db.scope}:preserve', schema=db.schema, database=db.database,
                    ))
                created.append(Instance(f'i-{identity[:17]}', base, runtime, db_name))
            self.runtimes.enter_context(launch.pop_all())
        self.instances.update((instance.id, instance) for instance in created)
        reservation = f'r-{uuid4().hex[:17]}'
        self.reservations[reservation] = created
        if token is not None:
            self.tokens[token] = spec, reservation
        return self.reservation(reservation, created)

    def describe_instances(self, q: Query) -> dict[str, XmlValue]:
        ids = q.members('InstanceId')
        q.end()
        selected = {self.instance(identity).id for identity in ids}
        reservations = []
        for identity, instances in self.reservations.items():
            matching = [instance for instance in instances if not ids or instance.id in selected]
            if matching:
                reservations.append(self.reservation(identity, matching))
        return {'reservationSet': Members('item', reservations)}

    def terminate_instances(self, q: Query) -> dict[str, XmlValue]:
        ids = q.members('InstanceId')
        q.end()
        if not ids:
            raise QueryError('At least one InstanceId is required')
        instances = [self.instance(identity) for identity in dict.fromkeys(ids)]
        changes = []
        for instance in instances:
            previous = describe_state(instance.running)
            instance.runtime.close()
            instance.running = False
            changes.append({'instanceId': instance.id, 'previousState': previous, 'currentState': describe_state(False)})
        return {'instancesSet': Members('item', changes)}

    def create_database(self, q: Query) -> dict[str, XmlValue]:
        name = q.name('DBInstanceIdentifier')
        q.expect('DBInstanceClass', DB_CLASS)
        q.expect('Engine', 'postgres')
        q.end()
        if name in self.databases:
            raise QueryError(f'Virtual database {name} already exists', 'DBInstanceAlreadyExists')
        if len(self.databases) >= 4:
            raise QueryError('At most four virtual databases can exist', 'InstanceQuotaExceeded')
        db = Database(name, uuid4().hex)
        with ExitStack() as rollback:
            rollback.callback(database_admin, f'DROP DATABASE IF EXISTS {db.database} WITH (FORCE)')
            database_admin(f'CREATE DATABASE {db.database}')
            with TemporaryDirectory(dir=self.path) as folder:
                seed = Path(folder) / 'seed.sqlite'
                seed_sqlite(seed)
                subprocess.run(
                    ['php', 'migrate.php', str(seed), db.schema, 'preserve'],
                    cwd=APP, env={**os.environ, 'PGHOST': DB_HOST, 'PGDATABASE': db.database},
                    check=True, timeout=10,
                )
            database_admin(f'ALTER DATABASE {db.database} SET search_path TO {db.schema}')
            rollback.pop_all()
        self.databases[name] = db
        return {'DBInstance': db.describe()}

    def describe_databases(self, q: Query) -> dict[str, XmlValue]:
        name = q.name('DBInstanceIdentifier') if 'DBInstanceIdentifier' in q.fields else None
        q.end()
        if name is not None and name not in self.databases:
            raise QueryError(f'Unknown virtual database: {name}', 'DBInstanceNotFound', 404)
        records = [db.describe() for db in self.databases.values() if name is None or name == db.name]
        return {'DBInstances': Members('DBInstance', records)}

    def delete_database(self, q: Query) -> dict[str, XmlValue]:
        name = q.name('DBInstanceIdentifier')
        q.expect('SkipFinalSnapshot', 'true')
        q.expect('DeleteAutomatedBackups', 'true', optional=True)
        q.end()
        db = self.databases.get(name)
        if db is None:
            raise QueryError(f'Unknown virtual database: {name}', 'DBInstanceNotFound', 404)
        if any(instance.running and instance.database == name for instance in self.instances.values()):
            raise QueryError(f'Terminate shops using {name} first', 'InvalidDBInstanceState')
        database_admin(f'DROP DATABASE IF EXISTS {db.database} WITH (FORCE)')
        del self.databases[name]
        return {'DBInstance': db.describe('deleting')}

    def create_balancer(self, q: Query) -> dict[str, XmlValue]:
        name = q.name('Name')
        q.expect('Type', 'application', optional=True)
        q.end()
        if self.balancer is not None and self.balancer[0] != name:
            raise QueryError('This simulator exposes one load balancer', 'TooManyLoadBalancers')
        if self.balancer is None:
            self.balancer = name, self.elb_arn(f'loadbalancer/app/{name}')
        name, arn = self.balancer
        return {'LoadBalancers': Members('member', [{
            'LoadBalancerArn': arn, 'LoadBalancerName': name, 'DNSName': '127.0.0.1',
            'Type': 'application', 'State': {'Code': 'active'},
        }])}

    def elb_arn(self, prefix: str) -> str:
        return f'arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:{prefix}/{uuid4().hex[:16]}'

    def create_group(self, q: Query) -> dict[str, XmlValue]:
        name = q.name('Name')
        q.expect('Protocol', 'HTTP')
        q.expect('Port', '80')
        q.expect('TargetType', 'instance', optional=True)
        q.expect('HealthCheckPath', '/health', optional=True)
        q.end()
        group = next((group for group in self.groups.values() if group.name == name), None)
        if group is None:
            if len(self.groups) >= 4:
                raise QueryError('At most four virtual target groups can exist', 'TooManyTargetGroups')
            group = TargetGroup(self.elb_arn(f'targetgroup/{name}'), name)
            self.groups[group.arn] = group
        return {'TargetGroups': Members('member', [group.describe()])}

    def group(self, arn: str) -> TargetGroup:
        if arn not in self.groups:
            raise QueryError('Unknown virtual target group', 'TargetGroupNotFound')
        return self.groups[arn]

    def create_listener(self, q: Query) -> dict[str, XmlValue]:
        alb = q.take('LoadBalancerArn')
        q.expect('Protocol', 'HTTP')
        q.expect('Port', '80')
        q.expect('DefaultActions.member.1.Type', 'forward')
        arn = q.take('DefaultActions.member.1.TargetGroupArn')
        q.end()
        if self.balancer is None or alb != self.balancer[1]:
            raise QueryError('Unknown virtual load balancer', 'LoadBalancerNotFound')
        group = self.group(arn)
        if self.listener is not None and self.listener[1] is not group:
            raise QueryError('HTTP port 80 already has a listener for another group', 'DuplicateListener')
        if self.listener is None:
            self.listener = self.elb_arn(f'listener/{alb.split(":", 5)[-1].removeprefix("loadbalancer/")}'), group
        return {'Listeners': Members('member', [{
            'ListenerArn': self.listener[0], 'LoadBalancerArn': alb, 'Protocol': 'HTTP', 'Port': 80,
            'DefaultActions': Members('member', [{'Type': 'forward', 'TargetGroupArn': arn}]),
        }])}

    def register_targets(self, q: Query, *, remove: bool = False) -> dict[str, XmlValue]:
        group = self.group(q.take('TargetGroupArn'))
        ids = []
        while (key := f'Targets.member.{len(ids) + 1}.Id') in q.fields:
            ids.append(q.take(key))
            q.expect(f'Targets.member.{len(ids)}.Port', '80', optional=True)
        q.end()
        if not ids:
            raise QueryError('Provide at least one instance target')
        if remove:
            for identity in ids:
                group.targets.pop(identity, None)
        else:
            instances = [self.instance(identity) for identity in ids]
            if not all(instance.running for instance in instances):
                raise QueryError('Only running instances can be registered', 'InvalidTarget')
            group.targets.update((instance.id, instance) for instance in instances)
        return {}

    def describe_health(self, q: Query) -> dict[str, XmlValue]:
        group = self.group(q.take('TargetGroupArn'))
        q.end()
        results = []
        active = self.listener is not None and self.listener[1] is group
        for instance in group.targets.values():
            status: dict[str, XmlValue]
            if not instance.running:
                status = {'State': 'unused', 'Reason': 'Target.InvalidState'}
            elif not active:
                status = {'State': 'unused', 'Reason': 'Target.NotInUse'}
            elif instance.healthy():
                status = {'State': 'healthy'}
            else:
                status = {'State': 'unhealthy', 'Reason': 'Target.FailedHealthChecks'}
            results.append({'Target': {'Id': instance.id, 'Port': 80}, 'HealthCheckPort': '80', 'TargetHealth': status})
        return {'TargetHealthDescriptions': Members('member', results)}

    def actions(self, action: str, version: str, q: Query) -> dict[str, XmlValue]:
        match version, action:
            case '2016-11-15', 'RunInstances': return self.run_instances(q)
            case '2016-11-15', 'DescribeInstances': return self.describe_instances(q)
            case '2016-11-15', 'TerminateInstances': return self.terminate_instances(q)
            case '2014-10-31', 'CreateDBInstance': return self.create_database(q)
            case '2014-10-31', 'DescribeDBInstances': return self.describe_databases(q)
            case '2014-10-31', 'DeleteDBInstance': return self.delete_database(q)
            case '2015-12-01', 'CreateLoadBalancer': return self.create_balancer(q)
            case '2015-12-01', 'CreateTargetGroup': return self.create_group(q)
            case '2015-12-01', 'CreateListener': return self.create_listener(q)
            case '2015-12-01', 'RegisterTargets': return self.register_targets(q)
            case '2015-12-01', 'DeregisterTargets': return self.register_targets(q, remove=True)
            case '2015-12-01', 'DescribeTargetHealth': return self.describe_health(q)
            case _: raise QueryError(f'{action} is not supported in this simulator', 'InvalidAction')


class Server(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, port: int, handler: type[BaseHTTPRequestHandler], cloud: Cloud) -> None:
        super().__init__(('0.0.0.0', port), handler)
        self.cloud = cloud

    def get_request(self) -> tuple:
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class LocalHandler(Handler):
    server: Server

    def local(self) -> bool:
        return (
            self.headers.get('Host', '').split(':', 1)[0] in {'127.0.0.1', 'localhost'}
            and 'Origin' not in self.headers
        )

    def read_body(self, maximum: int) -> bytes:
        length = self.headers.get('Content-Length', '')
        if not length.isascii() or not length.isdecimal() or len(length) > 5 or not 0 < int(length) <= maximum:
            raise QueryError(f'Expected Content-Length from 1 to {maximum} bytes')
        return self.rfile.read(int(length))


class Control(LocalHandler):
    def do_GET(self) -> None:
        if self.local() and self.path == '/ready':
            self.send_json(200, {'mode': 'virtual-aws', 'ready': True})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if not self.local() or self.path != '/':
            self.send_error(403)
            return
        action, version = '', ''
        request_id = str(uuid4())
        try:
            if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                raise QueryError('Expected an AWS Query form POST')
            form = parse_qs(self.read_body(16384).decode(), keep_blank_values=True, max_num_fields=64)
            query = Query(form)
            action, version = query.take('Action'), query.take('Version')
            with self.server.cloud.lock:
                result = self.server.cloud.actions(action, version, query)
        except ValueError:
            self.error(QueryError('Invalid AWS Query form'), version, request_id)
        except QueryError as error:
            self.error(error, version, request_id)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            LOGGER.exception('Virtual infrastructure operation failed: %s', action)
            self.error(QueryError('Local infrastructure operation failed; see simulator logs', 'InternalFailure', 500), version, request_id)
        else:
            service = {'2016-11-15': 'ec2', '2014-10-31': 'rds', '2015-12-01': 'elasticloadbalancing'}[version]
            root = Element(f'{action}Response', xmlns=f'http://{service}.amazonaws.com/doc/{version}/')
            if service == 'ec2':
                xml_fields(root, {'requestId': request_id, **result})
            else:
                xml_fields(root, {f'{action}Result': result, 'ResponseMetadata': {'RequestId': request_id}})
            self.send_body(200, tostring(root, encoding='utf-8'), 'text/xml')

    def error(self, error: QueryError, version: str, request_id: str) -> None:
        fields: dict[str, XmlValue] = {'Code': error.code, 'Message': str(error)}
        if version == '2016-11-15':
            root = Element('Response')
            xml_fields(root, {'Errors': {'Error': fields}, 'RequestID': request_id})
        else:
            root = Element('ErrorResponse')
            xml_fields(root, {'Error': {'Type': 'Sender' if error.status < 500 else 'Receiver', **fields}, 'RequestId': request_id})
        self.send_body(error.status, tostring(root, encoding='utf-8'), 'text/xml')


class DataPlane(LocalHandler):
    def forward(self) -> None:
        if not self.local():
            self.send_error(403)
            return
        if not SHOP_ROUTE.fullmatch(self.path):
            self.send_json(404, {'error': 'Not a shop route'})
            return
        headers = {key: value for key in ('Content-Type', 'X-Customer-ID', 'Idempotency-Key')
                   if (value := self.headers.get(key)) is not None}
        try:
            body = self.read_body(4096) if self.command == 'POST' else None
        except QueryError as error:
            self.send_json(400, {'error': str(error)})
            return
        cloud = self.server.cloud
        with cloud.lock:
            target = cloud.listener[1].pick() if cloud.listener is not None else None
        if target is None:
            self.send_json(503, {'error': 'No healthy virtual ALB targets'})
            return
        with closing(HTTPConnection('127.0.0.1', urlsplit(target.base).port, timeout=5)) as upstream:
            try:
                upstream.request(self.command, self.path, body, headers)
                response = upstream.getresponse()
                payload = response.read()
            except (OSError, HTTPException):
                self.send_json(502, {'error': 'Virtual target did not reply'})
                return
        self.send_response(response.status)
        self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('X-Black-Friday-Instance', target.id)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    do_GET = forward
    do_POST = forward


@contextmanager
def serving(server: Payments | Server) -> Iterator[None]:
    with server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def main() -> None:
    stopped = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    logging.basicConfig(level=logging.INFO)
    with TemporaryDirectory(prefix='black-friday-sim-') as folder, ExitStack() as stack:
        path = Path(folder)
        payments = Payments(Ledger(path / 'payments.sqlite'))
        stack.enter_context(serving(payments))
        runtimes = stack.enter_context(ExitStack())
        cloud = Cloud(path, f'http://127.0.0.1:{payments.server_port}/payments', runtimes)
        stack.enter_context(serving(Server(8781, DataPlane, cloud)))
        stack.enter_context(serving(Server(8780, Control, cloud)))
        LOGGER.info('Virtual AWS Query API :8780, shop ALB :8781 — no AWS account access')
        stopped.wait()


if __name__ == '__main__':
    main()
