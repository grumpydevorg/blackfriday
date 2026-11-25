"""Packaging, deployment configuration, the presenter proxy and the evaluator's event shapes."""

import ast
import base64
import importlib.util
import io
import json
import re
import sys
import threading
import time
from hashlib import sha256
from http.client import HTTPConnection
from pathlib import Path

import pytest

AWS_DIR = Path(__file__).parent
STDLIB = set(sys.stdlib_module_names)
INSTALLED = {'boto3', 'botocore', 'aws_durable_execution_sdk_python'}
RUN = 'a' * 32


def load_operate(monkeypatch, deployment):
    monkeypatch.setenv('BLACK_FRIDAY_DEPLOYMENT', deployment)
    spec = importlib.util.spec_from_file_location(f'operate_{deployment}', AWS_DIR / 'operate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def definitions(monkeypatch, tmp_path):
    return load_operate(monkeypatch, 'fourround').package_definitions(tmp_path)


# --- packages -------------------------------------------------------------------------------------------------


def test_the_web_package_contains_every_file_the_web_function_loads(monkeypatch, tmp_path):
    files, _ = definitions(monkeypatch, tmp_path)['web']
    sources = ''.join(path.read_text() for name, path in files.items() if name.endswith('.mjs'))
    needed = set(re.findall(r"from '\./([\w.-]+)'", sources)) | set(re.findall(r"new URL\('\./([\w./-]+)'", sources))
    assert {'game.mjs', 'host_auth.mjs', 'roster.mjs', 'rounds.json', 'player.html', 'host.html', 'round.js',
            'round.css', 'art/cloud-surge.jpg'} <= needed
    assert 'options.json' not in needed
    assert not needed - set(files), needed - set(files)
    for name, path in files.items():
        assert path.is_file(), name
    assert (AWS_DIR / 'art' / 'cloud-surge.jpg').stat().st_size <= 300_000
    assert not {'scoring.py', 'options.json'} & set(files)


def test_the_workflow_package_has_the_game_and_no_one_round_files(monkeypatch, tmp_path):
    files, trees = definitions(monkeypatch, tmp_path)['workflow']
    assert set(files) == {'workflow.py', 'model.py', 'rounds.json'}
    assert set(trees) == {''}, 'only the SDK tree: no virtual_rds, arena or scoring'
    assert files['rounds.json'] == AWS_DIR / 'rounds.json'


def test_the_worker_package_is_the_evaluator_alone(monkeypatch, tmp_path):
    files, _ = definitions(monkeypatch, tmp_path)['worker']
    assert set(files) == {'evaluator.py'}


@pytest.mark.parametrize('kind', ['worker', 'workflow'])
def test_python_packages_import_only_what_they_ship(monkeypatch, tmp_path, kind):
    files, _ = definitions(monkeypatch, tmp_path)[kind]
    shipped = {Path(name).stem for name in files if name.endswith('.py')}
    for name, path in files.items():
        if not name.endswith('.py'):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ''] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            )
            for module in modules:
                root = module.split('.')[0]
                assert root in STDLIB | INSTALLED | shipped, f'{name} imports {module}, which is not packaged'


def test_the_image_ships_the_runtime_and_the_php_shop(monkeypatch, tmp_path):
    files, _ = definitions(monkeypatch, tmp_path)['image']
    assert set(files) == {'Dockerfile', 'aws/vm_runtime.py', 'schema.sql', 'db.php', 'shop.php', 'migrate.php'}


def test_the_web_role_may_invoke_every_workflow_version_not_just_the_newest():
    """After a deploy the previous Web Function revision keeps serving; it must still be able to start a round."""
    template = (AWS_DIR / 'rehearsal.yaml').read_text()
    block = template[template.index('Action: lambda:InvokeFunction', template.index('WebRole:')):][:700]
    assert "Resource: !Sub '${Workflow.Arn}:*'" in block
    assert 'Resource: !Ref WorkflowVersion' not in block


def test_the_workflow_may_write_and_the_web_may_read_the_results_items():
    template = (AWS_DIR / 'rehearsal.yaml').read_text()
    workflow = template[template.index('WorkflowRole:'):template.index('  Workflow:\n')]
    web = template[template.index('WebRole:'):template.index('Outputs:')]
    assert 'dynamodb:UpdateItem' in workflow
    assert 'dynamodb:GetItem' in web
    assert 'DurableConfig: {ExecutionTimeout: 172800' in template


# --- deployments ----------------------------------------------------------------------------------------------


def test_fourround_is_a_third_deployment_beside_the_frozen_two(monkeypatch):
    four = load_operate(monkeypatch, 'fourround')
    assert (four.STACK, four.ARTIFACTS, four.WEB_FUNCTION) == (
        'BlackFridayFourRound', 'BlackFridayFourRoundArtifacts', 'black-friday-four')
    round_ = load_operate(monkeypatch, 'round')
    assert (round_.STACK, round_.ARTIFACTS, round_.WEB_FUNCTION) == (
        'BlackFridayRound', 'BlackFridayRoundArtifacts', 'black-friday-round')
    rehearsal = load_operate(monkeypatch, 'rehearsal')
    assert (rehearsal.STACK, rehearsal.WEB_FUNCTION) == ('BlackFridayRehearsal', 'black-friday-arena')
    # The presenter's key and passphrase are shared with the round, not duplicated.
    assert four.PRESENTER_PARAMETER == round_.PRESENTER_PARAMETER == '/black-friday/round/presenter-key'
    assert four.PASSPHRASE_PARAMETER == round_.PASSPHRASE_PARAMETER == '/black-friday/round/presenter-passphrase'
    # The artifact bucket prefixes must not collide: one is not a prefix of the other.
    assert not four.ARTIFACTS.lower().startswith(round_.ARTIFACTS.lower())


def test_an_unknown_deployment_is_refused(monkeypatch):
    with pytest.raises(SystemExit, match='fourround'):
        load_operate(monkeypatch, 'two-rounds')


@pytest.mark.parametrize('deployment, message', [
    ('round', 'round-fallback-2026-11-25'),
    ('rehearsal', 'rehearsal-fallback-2026-11-24b'),
])
def test_deploy_refuses_the_frozen_deployments_before_touching_aws(monkeypatch, deployment, message):
    operate = load_operate(monkeypatch, deployment)
    monkeypatch.setattr(operate, 'account_session', lambda: pytest.fail('deploy must refuse before AWS'))
    with pytest.raises(SystemExit, match=message) as refusal:
        operate.deploy()
    if deployment == 'round':
        assert 'git archive' in str(refusal.value)


def test_deploy_accepts_fourround_and_goes_on_to_the_account_check(monkeypatch):
    operate = load_operate(monkeypatch, 'fourround')
    monkeypatch.setattr(operate, 'ACCOUNT', '')
    with pytest.raises(SystemExit, match='BLACK_FRIDAY_AWS_ACCOUNT'):
        operate.deploy()


def test_the_passphrase_command_sets_the_shared_parameter_for_round_and_fourround(monkeypatch):
    for deployment in ('round', 'fourround'):
        operate = load_operate(monkeypatch, deployment)
        puts = []
        ssm = type('Ssm', (), {
            'put_parameter': lambda self, **kw: puts.append(kw),
            'describe_parameters': lambda self, **kw: {'Parameters': [{'Version': 3}]},
        })()
        monkeypatch.setattr(operate, 'account_session', lambda: type('S', (), {'client': lambda self, name: ssm})())
        monkeypatch.setattr(operate.sys, 'stdin', io.StringIO('a long passphrase\n'))
        operate.passphrase()
        assert [put['Name'] for put in puts] == ['/black-friday/round/presenter-passphrase']
    with pytest.raises(SystemExit, match='passphrase'):
        load_operate(monkeypatch, 'rehearsal').passphrase()


# --- the presenter proxy --------------------------------------------------------------------------------------


class FakeResponse:
    status = 200
    headers = {'Content-Type': 'application/json'}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"ok": true}'


class FakeSession:
    def client(self, name):
        assert name == 'ssm'
        return type('Ssm', (), {'get_parameter': lambda self, **kw: {'Parameter': {'Value': 'k' * 64}}})()


@pytest.fixture
def proxy(monkeypatch):
    operate = load_operate(monkeypatch, 'fourround')
    seen, servers = [], []

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            return FakeResponse()

    class Server(operate.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(operate, 'account_session', lambda: FakeSession())
    monkeypatch.setattr(operate, 'stage_url', lambda session: 'https://abc.lambda-web.ap-southeast-2.on.aws')
    monkeypatch.setattr(operate, 'build_opener', lambda *handlers: Opener())
    monkeypatch.setattr(operate, 'ThreadingHTTPServer', Server)
    port = 18770
    thread = threading.Thread(target=operate.present, args=(port,), daemon=True)
    thread.start()
    for _ in range(100):
        if servers:
            break
        time.sleep(0.05)
    assert servers, 'the presenter proxy did not start'

    def call(method, path, headers=None, body=b''):
        connection = HTTPConnection('127.0.0.1', port, timeout=5)
        connection.request(method, path, body or None, {'Content-Length': str(len(body)), **(headers or {})})
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    yield call, seen
    servers[0].shutdown()
    thread.join(5)


def test_the_proxy_forwards_the_four_round_presenter_routes(proxy):
    call, seen = proxy
    for path in ('/host', '/api/host/state', '/api/host/qr.svg', '/api/host/timeline', '/api/state', '/round.js',
                 '/round.css', '/round.js?v=3f9a1c2b', '/round.css?v=3f9a1c2b'):
        assert call('GET', path) == 200, path
    action = {'X-Rehearsal-Action': '1', 'X-Rehearsal-Run': 'token-1'}
    for path in ('/api/host/start', '/api/host/reset'):
        assert call('POST', path, action) == 200, path
    assert call('POST', '/api/host/close', {**action, 'X-Rehearsal-Round': 'growth'}) == 200
    assert call('POST', '/api/host/remove', {**action, 'Content-Type': 'application/json'}, b'{"id":"abc"}') == 200
    posts = {request.full_url.rsplit('/', 1)[1]: request for request in seen if request.method == 'POST'}
    assert set(posts) == {'start', 'reset', 'close', 'remove'}
    close = {name.lower(): value for name, value in posts['close'].headers.items()}
    assert close['x-rehearsal-round'] == 'growth'
    assert close['x-rehearsal-run'] == 'token-1'
    assert close['x-presenter-key'] == 'k' * 64
    assert posts['remove'].data == b'{"id":"abc"}'


def test_the_proxy_refuses_what_is_not_a_four_round_route_or_lacks_the_action_header(proxy):
    call, seen = proxy
    action = {'X-Rehearsal-Action': '1'}
    for path in ('/api/launch', '/api/migrate', '/api/compare', '/api/reset', '/api/host/state', '/api/join'):
        assert call('POST', path, action) == 403, path
    assert call('POST', '/api/host/start') == 403, 'no X-Rehearsal-Action'
    assert call('POST', '/api/host/start', action, b'{"x":1}') == 403, 'start has no body'
    assert call('POST', '/api/host/close', {**action, 'Origin': 'http://evil.example'}) == 403
    assert call('GET', '/api/host/start') == 404
    assert call('GET', '/api/launch') == 404
    assert call('GET', '/round.js?v=../../etc') == 404
    assert call('GET', '/api/host/timeline?x=1') == 404
    assert seen == []


# --- evaluator ------------------------------------------------------------------------------------------------


@pytest.fixture
def evaluator(monkeypatch):
    monkeypatch.setenv('AWS_REGION', 'ap-southeast-2')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'ap-southeast-2')
    monkeypatch.setenv('ARTIFACT_BUCKET', 'bucket')
    sys.modules.pop('evaluator', None)
    import evaluator as module
    yield module
    sys.modules.pop('evaluator', None)


def test_the_evaluator_imports_no_scoring_capacity_or_virtual_rds(evaluator):
    imported = {
        alias.name.split('.')[0]
        for node in ast.walk(ast.parse((AWS_DIR / 'evaluator.py').read_text()))
        for alias in (node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else [])
    } | {
        node.module.split('.')[0]
        for node in ast.walk(ast.parse((AWS_DIR / 'evaluator.py').read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {'scoring', 'virtual_rds', 'arena', 'capacity', 'model'} & imported
    assert not hasattr(evaluator.Rehearsal, 'capacity')


def test_events_are_launch_or_migrate_and_compare_an_option(evaluator):
    parse = evaluator.Rehearsal.parse
    assert parse({'id': RUN, 'action': 'launch'}).step == 'launch'
    careful = parse({'id': RUN, 'action': 'migrate', 'option': 'careful'})
    fast = parse({'id': RUN, 'action': 'compare', 'option': 'fast'})
    assert (careful.team, careful.kind, careful.step) == ('careful', 'preserve', 'migrate-careful')
    assert (fast.team, fast.kind, fast.step) == ('fast', 'omit', 'compare-fast')
    for bad in (
        {'id': RUN, 'action': 'launch', 'option': 'careful'},
        {'id': RUN, 'action': 'migrate'},
        {'id': RUN, 'action': 'migrate', 'option': 'stay'},
        {'id': RUN, 'action': 'migrate', 'option': 'lift'},
        {'id': RUN, 'action': 'compare', 'team': 'careful', 'kind': 'preserve'},
        {'id': RUN, 'action': 'compare', 'option': 'fast', 'kind': 'omit'},
        {'id': 'x', 'action': 'launch'},
        {'id': RUN, 'action': 'evaluate', 'option': 'fast'},
    ):
        with pytest.raises(ValueError):
            parse(bad)
    with pytest.raises(TypeError):
        parse([])


class FakeStore:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.capabilities = []

    def get(self, key):
        return self.objects.get(key)

    def require(self, key):
        if key not in self.objects:
            raise RuntimeError(f'Missing prerequisite: {key}')
        return self.objects[key]

    def put_once(self, key, body, content_type):
        self.objects.setdefault(key, body)

    def payment_capability(self, key, receipt):
        self.capabilities.append(key)
        return {'pay_url': 'https://bucket.s3.ap-southeast-2.amazonaws.com/x', 'receipt': receipt.decode('ascii')}


def reply(evaluator, status, body=b'{}', elapsed=None):
    return evaluator.Reply(status, body if isinstance(body, bytes) else json.dumps(body).encode(), elapsed, False)


def make_shop(evaluator, stage, routes):
    class Shop(evaluator.Shop):
        def __init__(self):
            super().__init__('https://shop', 'token', time.monotonic() + 60)
            self.calls = []

        def request(self, path, body=None, intention='', timeout=7):
            self.calls.append((path, body))
            return routes[path]

    return Shop()


def growth_world(evaluator, option, *, cart):
    stage = evaluator.Rehearsal.parse({'id': RUN, 'action': 'compare', 'option': option})
    launch_order = {'id': 'order-launch-mug', 'customer_id': 'maya', 'cart_id': 'cart-launch',
                    'intention': 'launch-mug', 'amount_cents': 2500, 'receipt': stage.receipt('launch').decode()}
    store = FakeStore({stage.ledger_key('launch'): stage.receipt('launch')})
    routes = {
        '/health': reply(evaluator, 200, {'database': 'pgsql'}),
        '/orders/order-launch-mug': reply(evaluator, 200, launch_order),
    }
    if cart:
        store.objects[stage.ledger_key(option)] = stage.receipt(option)
        routes['/cart/cart-saved'] = reply(evaluator, 200, {'id': 'cart-saved'})
        routes['/checkout'] = reply(evaluator, 200, elapsed=420)
        routes['/orders/order-growth-mug'] = reply(evaluator, 200, {
            'id': 'order-growth-mug', 'customer_id': 'maya', 'cart_id': 'cart-saved', 'intention': 'growth-mug',
            'amount_cents': 2500, 'receipt': stage.receipt(option).decode()})
    else:
        routes['/cart/cart-saved'] = reply(evaluator, 404)
        routes['/orders/order-growth-mug'] = reply(evaluator, 404)
    return stage, store, make_shop(evaluator, stage, routes)


COMPARE_KEYS = {'option', 'kind', 'cart_status', 'cart_kept', 'checkout_status', 'checkout_ms', 'charges',
                'verified_sales', 'health_status', 'history_status'}


def test_compare_careful_keeps_the_cart_and_sells_once(evaluator):
    stage, store, shop = growth_world(evaluator, 'careful', cart=True)
    result = stage.growth(store, shop)
    assert set(result) == COMPARE_KEYS
    assert result == {
        'option': 'careful', 'kind': 'preserve', 'cart_status': 200, 'cart_kept': True, 'checkout_status': 200,
        'checkout_ms': 420, 'charges': 1, 'verified_sales': 1, 'health_status': 200, 'history_status': 200,
    }


def test_compare_fast_omits_the_cart_and_that_is_not_an_error(evaluator):
    stage, store, shop = growth_world(evaluator, 'fast', cart=False)
    result = stage.growth(store, shop)
    assert set(result) == COMPARE_KEYS
    assert result == {
        'option': 'fast', 'kind': 'omit', 'cart_status': 404, 'cart_kept': False, 'checkout_status': None,
        'checkout_ms': None, 'charges': 0, 'verified_sales': 0, 'health_status': 200, 'history_status': 200,
    }
    assert all(path != '/checkout' for path, _ in shop.calls)


def test_a_cart_that_survives_omit_or_vanishes_under_preserve_is_an_error(evaluator):
    stage, store, shop = growth_world(evaluator, 'fast', cart=True)
    with pytest.raises(RuntimeError, match='cart/cart-saved returned HTTP 200, expected 404'):
        stage.growth(store, shop)
    stage, store, shop = growth_world(evaluator, 'careful', cart=False)
    with pytest.raises(RuntimeError, match='cart/cart-saved returned HTTP 404, expected 200'):
        stage.growth(store, shop)


def test_a_charge_the_omit_shop_should_not_have_made_is_an_error(evaluator):
    stage, store, shop = growth_world(evaluator, 'fast', cart=False)
    store.objects[stage.ledger_key('fast')] = stage.receipt('fast')
    with pytest.raises(RuntimeError, match='Growth ledger does not agree'):
        stage.growth(store, shop)


def run_handler(evaluator, monkeypatch, option, *, cart):
    """The whole handler for one compare event, against fakes for S3 and the MicroVM fleet."""
    stage, store, shop = growth_world(evaluator, option, cart=cart)
    dump = b'PGDMP' + b'\0' * 16
    digest = sha256(dump).hexdigest()
    store.objects[stage.result_key(f'migrate-{option}')] = json.dumps({'dump': digest, 'vm_id': 'vm-migrate'}).encode()
    store.objects[stage.artifact_key(f'migrate-{option}', digest, 'branch.dump')] = dump
    setups = []

    class Fleet:
        def finish(self, vm_id, deadline):
            pass

        def run(self, identity):
            return {'microvmId': 'vm-compare'}

        def connect(self, vm, deadline):
            real = shop.request

            def request(path, body=None, intention='', timeout=7):
                if path == '/control/setup':
                    setups.append(body)
                    return reply(evaluator, 200)
                return real(path, body, intention, timeout)

            shop.request = request
            return shop

    monkeypatch.setattr(evaluator, 'Store', lambda region: store)
    monkeypatch.setattr(evaluator, 'Fleet', lambda region: Fleet())
    context = type('Context', (), {'get_remaining_time_in_millis': lambda self: 300_000})()
    result = evaluator.handler({'id': RUN, 'action': 'compare', 'option': option}, context)
    return result, setups, store


def test_the_handler_returns_the_contract_compare_shape(evaluator, monkeypatch):
    result, setups, store = run_handler(evaluator, monkeypatch, 'careful', cart=True)
    assert set(result) == COMPARE_KEYS | {'vm_id', 'vm_state'}
    assert (result['vm_id'], result['vm_state'], result['cart_kept']) == ('vm-compare', 'terminated', True)
    assert set(setups[0]) == {'id', 'action', 'team', 'kind', 'input', 'pay_url', 'receipt'}
    assert (setups[0]['team'], setups[0]['kind']) == ('careful', 'preserve')
    assert store.capabilities and 'careful' in store.capabilities[0]


def test_the_handler_runs_omit_without_a_payment_capability(evaluator, monkeypatch):
    result, setups, store = run_handler(evaluator, monkeypatch, 'fast', cart=False)
    assert set(result) == COMPARE_KEYS | {'vm_id', 'vm_state'}
    assert (result['cart_status'], result['cart_kept'], result['charges'], result['verified_sales']) == (404, False, 0, 0)
    assert set(setups[0]) == {'id', 'action', 'team', 'kind', 'input'}
    assert (setups[0]['team'], setups[0]['kind']) == ('fast', 'omit')
    assert store.capabilities == []
    assert base64.b64decode(setups[0]['input']).startswith(b'PGDMP')


# --- the MicroVM runtime --------------------------------------------------------------------------------------


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', '/usr/bin')
    sys.modules.pop('vm_runtime', None)
    import vm_runtime
    monkeypatch.setattr(vm_runtime, 'STATE', tmp_path)
    monkeypatch.setattr(vm_runtime, 'SQLITE', tmp_path / 'shop.sqlite')
    monkeypatch.setattr(vm_runtime, 'DUMP', tmp_path / 'branch.dump')
    ran = []
    monkeypatch.setattr(vm_runtime.Runtime, 'run', lambda self, cmd: ran.append([str(part) for part in cmd]))
    monkeypatch.setattr(vm_runtime.Runtime, 'start_shop', lambda self: None)
    instance = vm_runtime.Runtime()
    instance.ran = ran
    yield vm_runtime, instance
    sys.modules.pop('vm_runtime', None)


def migrate_event(kind, **extra):
    sqlite = base64.b64encode(b'SQLite format 3\0' + b'\0' * 16).decode()
    return {'id': RUN, 'action': 'migrate', 'team': 'fast' if kind == 'omit' else 'careful', 'kind': kind,
            'input': sqlite, **extra}


@pytest.mark.parametrize('kind', ['preserve', 'omit'])
def test_the_runtime_migrates_both_kinds(runtime, kind):
    vm_runtime, instance = runtime
    event = migrate_event(kind)
    (vm_runtime.SQLITE).write_bytes(base64.b64decode(event['input']))
    instance.setup(event)
    assert any(cmd[:2] == ['php', 'migrate.php'] and cmd[-1] == kind for cmd in instance.ran)


def test_omit_compare_takes_no_payment_capability_and_preserve_compare_requires_it(runtime, monkeypatch):
    vm_runtime, instance = runtime
    dump = base64.b64encode(b'PGDMP' + b'\0' * 16).decode()
    omit = {'id': RUN, 'action': 'compare', 'team': 'fast', 'kind': 'omit', 'input': dump}
    with pytest.raises(vm_runtime.BadRequest):
        vm_runtime.Runtime().setup({**omit, 'pay_url': 'https://x', 'receipt': 'b' * 32})
    instance.setup(omit)
    assert instance.kind == 'omit' and instance.payment is None
    preserve = {**omit, 'team': 'careful', 'kind': 'preserve'}
    with pytest.raises(vm_runtime.BadRequest):
        vm_runtime.Runtime().setup(preserve)
    for kind in ('', 'lift', None):
        with pytest.raises(vm_runtime.BadRequest):
            vm_runtime.Runtime().setup({**omit, 'kind': kind})
