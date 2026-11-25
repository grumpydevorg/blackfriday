"""Run the Durable handler against a fake context that checkpoints steps, as the real SDK does."""

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

RUN = 'a' * 32
ROUND_IDS = ['launch', 'growth', 'blackfriday', 'christmas']
RECORDED = json.loads((HERE / 'rounds.json').read_text())['recorded_evidence']


def stub_model():
    """Just enough model to test the workflow's mechanics: it draws once per player and echoes its inputs."""
    module = types.ModuleType('model')
    module.ROUND_IDS = ROUND_IDS
    module.calls = []

    def validate_players(value):
        if not isinstance(value, list):
            raise ValueError('players must be a list')
        seen, rows = set(), []
        for row in value:
            if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] not in seen:
                seen.add(row['id'])
                rows.append({'id': row['id'], 'name': str(row.get('name') or 'Player'), 'house': bool(row.get('house')),
                             'option': row.get('option') or 'default'})
        return rows

    def plan(round_id, players, previous, randbelow):
        module.calls.append(('plan', round_id, previous and previous['round']))
        return {'round': round_id, 'market': randbelow(100),
                'players': players, 'draws': {p['id']: {'roll': randbelow(100), 'noise': randbelow(21) - 10}
                                              for p in players}}

    def score(round_id, plan, previous, evidence):
        module.calls.append(('score', round_id, evidence))
        rows = [{'id': p['id'], 'name': p['name'], 'rank': index + 1, 'option': p['option'],
                 'roll': plan['draws'][p['id']]['roll']} for index, p in enumerate(plan['players'])]
        return {'round': round_id, 'market': plan['market'], 'states': {}, 'baseline': {}, 'bots': [],
                'rows': rows, 'summary': {'market': plan['market'], 'options': {}}}

    module.validate_players, module.plan, module.score = validate_players, plan, score
    return module


def real_model():
    path = HERE / 'model.py'
    if not path.exists():
        pytest.skip('model.py has not landed')
    spec = importlib.util.spec_from_file_location('model', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_workflow(monkeypatch, model_module):
    sdk = types.ModuleType('aws_durable_execution_sdk_python')
    sdk.CallbackTimeoutError = type('CallbackTimeoutError', (Exception,), {})
    sdk.DurableContext = object
    sdk.durable_execution = lambda function: function
    config = types.ModuleType('aws_durable_execution_sdk_python.config')
    config.CallbackConfig = lambda timeout: ('callback', timeout)
    config.Duration = types.SimpleNamespace(from_hours=lambda hours: hours)
    monkeypatch.setitem(sys.modules, 'aws_durable_execution_sdk_python', sdk)
    monkeypatch.setitem(sys.modules, 'aws_durable_execution_sdk_python.config', config)
    monkeypatch.setitem(sys.modules, 'model', model_module)
    monkeypatch.setenv('RUN_TABLE', 'table')
    monkeypatch.setenv('EVALUATOR_ARN', 'arn:evaluator:1')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'ap-southeast-2')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'x')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'x')
    sys.modules.pop('workflow', None)
    import workflow

    return workflow


def player(name, option='default', house=False):
    return {'id': name.lower().replace(' ', ''), 'name': name, 'house': house, 'option': option, 'picks': {}}


ROOM = [player('Ada Byron'), player('Grace Hopper'), player('House', house=True)]


def compare_result(option, cart_kept):
    return {'option': option, 'kind': 'preserve' if option == 'careful' else 'omit', 'cart_status': 200,
            'cart_kept': cart_kept, 'checkout_status': 200, 'checkout_ms': 40, 'charges': 1, 'verified_sales': 1,
            'health_status': 200, 'history_status': 200, 'vm_id': f'vm-{option}', 'vm_state': 'terminated'}


# DynamoDB reserved words that name attributes in this table's items; a bare use in an expression is a ValidationException.
RESERVED = frozenset(('lines', 'data', 'size', 'name', 'status', 'count'))


def reject_reserved_words(kwargs):
    """What DynamoDB does: a reserved word used as an attribute path (not as a function name) must go through
    ExpressionAttributeNames, and every `#name` used must be defined (and every defined name used)."""
    names = kwargs.get('ExpressionAttributeNames', {})
    used = set()
    for key in ('UpdateExpression', 'ConditionExpression'):
        expression = kwargs.get(key, '')
        used |= set(re.findall(r'#\w+', expression))
        for word in re.findall(r'(?<![:#\w])([A-Za-z_]\w*)(?!\w|\s*\()', expression):
            if word.lower() in RESERVED:
                raise ClientError({'Error': {'Code': 'ValidationException', 'Message':
                                  f'Invalid {key}: Attribute name is a reserved keyword; reserved keyword: {word}'}},
                                  'UpdateItem')
    if used != set(names):
        raise ClientError({'Error': {'Code': 'ValidationException', 'Message':
                          f'Invalid expression: names used {sorted(used)} but defined {sorted(names)}'}}, 'UpdateItem')


class FakeDB:
    """Records every UpdateItem; `fail` conditional-fails the arena writes whose phase matches.
    Timeline writes are kept apart in `timeline_calls`, with whether a step was running when each arrived."""

    def __init__(self):
        self.calls, self.fail_phase = [], None
        self.timeline_calls, self.timeline_error, self.context = [], None, None

    def update_item(self, **kwargs):
        reject_reserved_words(kwargs)
        if kwargs['Key']['pk']['S'].startswith('timeline#'):
            self.timeline_calls.append({**kwargs, 'in_step': self.context.in_step if self.context else None})
            if self.timeline_error:
                raise self.timeline_error
            return
        self.calls.append(kwargs)
        phase = kwargs['ExpressionAttributeValues'].get(':phase', {}).get('S')
        if phase is not None and phase == self.fail_phase:
            raise ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem')

    def arena(self):
        return [call for call in self.calls if call['Key']['pk']['S'] == 'arena' and ':phase' in call['ExpressionAttributeValues']]

    def results(self):
        return {call['Key']['pk']['S']: json.loads(call['ExpressionAttributeValues'][':data']['S'])
                for call in self.calls if call['Key']['pk']['S'].startswith('results#')}

    def timeline_lines(self):
        return [json.loads(item['S']) for call in self.timeline_calls
                for item in call['ExpressionAttributeValues'][':new']['L']]


def values(call):
    return {key: value['S'] for key, value in call['ExpressionAttributeValues'].items()}


class FakeContext:
    """Steps and invokes are checkpointed by name: a replay returns the recorded result and never calls the function."""

    def __init__(self, checkpoints, rooms, workflow, fail=(), launch_fails=False, db=None):
        self.checkpoints, self.rooms, self.workflow = checkpoints, rooms, workflow
        self.fail, self.launch_fails, self.db = set(fail), launch_fails, db
        self.ops, self.invoked, self.step_calls, self.in_step = [], [], [], False
        self.execution_context = types.SimpleNamespace(durable_execution_arn='arn:durable')

    def operation(self, kind, name):
        self.ops.append((kind, name))

    def step(self, function, name):
        self.operation('step', name)
        self.step_calls.append(name)
        if name not in self.checkpoints:
            self.in_step = True
            try:
                self.checkpoints[name] = json.loads(json.dumps(function(None)))
            finally:
                self.in_step = False
        return self.checkpoints[name]

    def create_callback(self, name, config):
        self.operation('callback', name)
        context = self
        round_id = name.removeprefix('await-close-')

        class Callback:
            callback_id = f'cb-{name}'

            def result(self):
                room = context.rooms[round_id]
                if isinstance(room, Exception):
                    raise room
                return room if isinstance(room, str) else json.dumps(
                    {'action': 'close', 'round': round_id, 'players': room})

        return Callback()

    def invoke(self, function, event, name):
        self.operation('invoke', name)
        self.invoked.append((event['action'], event.get('option'), len(self.db.arena()) if self.db else 0))
        if name in self.checkpoints:
            return self.checkpoints[name]
        if event['action'] == 'launch':
            if self.launch_fails:
                raise RuntimeError('launch broke')
            result = {'vm_state': 'terminated', 'checkpoint': 'c' * 64, 'vm_id': 'vm-launch',
                      'launch': {'profit_cents': 900}}
        else:
            if (event['action'], event['option']) in self.fail:
                raise RuntimeError('MicroVM broke')
            base = {'vm_state': 'terminated', 'vm_id': f'vm-{name}'}
            result = base if event['action'] == 'migrate' else compare_result(event['option'], event['option'] == 'careful')
        self.checkpoints[name] = result
        return result

    def parallel(self, functions, name):
        self.operation('parallel', name)
        results = [function(self) for function in functions]
        return types.SimpleNamespace(throw_if_error=lambda: None, get_results=lambda: results)


def rooms_for(room=ROOM):
    return {round_id: room for round_id in ROUND_IDS}


def run(workflow, monkeypatch, checkpoints=None, rooms=None, rolls=None, db=None, **options):
    db = db or FakeDB()
    monkeypatch.setattr(workflow, 'DB', db)
    monkeypatch.setattr(workflow, 'register', lambda *args: None)
    draws = iter(rolls) if rolls is not None else None
    if draws is not None:
        monkeypatch.setattr(workflow.secrets, 'randbelow', lambda bound: next(draws) % bound)
    context = FakeContext({} if checkpoints is None else checkpoints, rooms or rooms_for(), workflow, db=db, **options)
    db.context = context
    result = workflow.handler({'id': RUN}, context)
    return context, db, result


@pytest.fixture
def workflow(monkeypatch):
    return load_workflow(monkeypatch, stub_model())


def test_every_operation_name_is_unique(workflow, monkeypatch):
    context, _, _ = run(workflow, monkeypatch)
    names = [name for _, name in context.ops]
    assert len(names) == len(set(names))
    assert {'register-execution', 'launch-shop', 'migrate-every-option', 'check-every-option',
            'publish-final'} <= set(names)
    for round_id in ROUND_IDS:
        assert {f'await-close-{round_id}', f'publish-open-{round_id}', f'plan-{round_id}',
                f'score-{round_id}', f'publish-results-{round_id}'} <= set(names)


def test_all_real_evidence_is_gathered_before_round_one_and_never_after(workflow, monkeypatch):
    context, db, _ = run(workflow, monkeypatch)
    assert sorted((action, option) for action, option, _ in context.invoked) == [
        ('compare', 'careful'), ('compare', 'fast'), ('launch', None), ('migrate', 'careful'), ('migrate', 'fast'),
    ]
    # No arena write has happened yet at the time of every invoke, so none came after the first open.
    assert {published for _, _, published in context.invoked} == {0}
    order = [name for _, name in context.ops]
    assert max(order.index(name) for name in ('launch-shop', 'migrate-every-option', 'check-every-option')) < order.index('publish-open-launch')
    assert not [kind for kind, name in context.ops[order.index('publish-open-launch'):] if kind in ('invoke', 'parallel')]


def test_the_first_open_carries_launch_and_evidence_and_later_opens_carry_the_previous_results(workflow, monkeypatch):
    _, db, _ = run(workflow, monkeypatch)
    opens = [values(call) for call in db.arena() if values(call)[':phase'] == 'open']
    assert [o[':round'] for o in opens] == ROUND_IDS
    assert [o[':callback_id'] for o in opens] == [f'cb-await-close-{r}' for r in ROUND_IDS]
    launch = json.loads(opens[0][':launch_data'])
    assert launch == {'profit_cents': 900, 'source': 'real'}
    evidence = json.loads(opens[0][':evidence_data'])
    assert evidence['careful']['source'] == evidence['fast']['source'] == 'real'
    assert evidence['careful']['cart_kept'] is True and evidence['fast']['cart_kept'] is False
    assert ':launch_data' not in opens[1] and ':evidence_data' not in opens[1]
    assert [o.get(':results_round') for o in opens] == [None, 'launch', 'growth', 'blackfriday']
    first = next(call for call in db.arena() if values(call)[':phase'] == 'open')
    assert values(first)[':old0'] == 'starting' and ':expround' not in values(first)
    later = [call for call in db.arena() if values(call)[':phase'] == 'open'][1:]
    assert [values(call)[':old0'] for call in later] == ['closing'] * 3
    assert [values(call)[':expround'] for call in later] == ROUND_IDS[:3]


def test_four_results_items_and_one_final(workflow, monkeypatch):
    _, db, result = run(workflow, monkeypatch)
    assert sorted(db.results()) == sorted(f'results#{RUN}#{r}' for r in ROUND_IDS)
    for round_id in ROUND_IDS:
        data = db.results()[f'results#{RUN}#{round_id}']
        assert set(data) == {'round', 'market', 'summary', 'rows'} and data['round'] == round_id
    item = next(call for call in db.calls if call['Key']['pk']['S'] == f'results#{RUN}#growth')
    assert values(item)[':id'] == RUN and values(item)[':round'] == 'growth'
    finals = [values(call) for call in db.arena() if values(call)[':phase'] == 'final']
    assert len(finals) == 1
    assert finals[0][':results_round'] == 'christmas' and finals[0][':expround'] == 'christmas'
    assert result == {'id': RUN, 'execution_arn': 'arn:durable'}
    # Each round's results are written before the next open announces them, and the final comes last.
    kinds = [('results' if call['Key']['pk']['S'] != 'arena' else values(call)[':phase']) for call in db.calls]
    assert kinds == ['open', 'results', 'open', 'results', 'open', 'results', 'open', 'results', 'final']


def test_state_threads_through_the_rounds_and_score_gets_the_evidence(workflow, monkeypatch):
    run(workflow, monkeypatch)
    calls = workflow.model.calls
    assert [c[:3] for c in calls if c[0] == 'plan'] == [
        ('plan', 'launch', None), ('plan', 'growth', 'launch'), ('plan', 'blackfriday', 'growth'),
        ('plan', 'christmas', 'blackfriday')]
    evidence = [c[2] for c in calls if c[0] == 'score']
    assert all(set(e) == {'careful', 'fast'} for e in evidence)


def test_a_failing_option_falls_back_alone(workflow, monkeypatch):
    context, db, _ = run(workflow, monkeypatch, fail={('compare', 'fast')})
    evidence = json.loads(values(db.arena()[0])[':evidence_data'])
    assert evidence['careful']['source'] == 'real' and evidence['careful']['vm_id'] == 'vm-careful'
    assert evidence['fast'] == {**RECORDED['fast'], 'source': 'recorded'}
    # The failure did not stop the other option's check or the run.
    assert ('compare', 'careful') in [(a, o) for a, o, _ in context.invoked]
    assert len(db.results()) == 4


def test_a_failed_migration_skips_that_options_check_and_records_it(workflow, monkeypatch):
    context, db, _ = run(workflow, monkeypatch, fail={('migrate', 'careful')})
    evidence = json.loads(values(db.arena()[0])[':evidence_data'])
    assert evidence['careful'] == {**RECORDED['careful'], 'source': 'recorded'}
    assert evidence['fast']['source'] == 'real'
    assert [(a, o) for a, o, _ in context.invoked if a == 'compare'] == [('compare', 'fast')]


def test_every_option_failing_and_a_failing_launch_still_play_four_rounds(workflow, monkeypatch):
    context, db, _ = run(workflow, monkeypatch, launch_fails=True, fail={('migrate', 'careful'), ('migrate', 'fast')})
    first = values(db.arena()[0])
    assert json.loads(first[':launch_data']) == {'source': 'recorded'}
    evidence = json.loads(first[':evidence_data'])
    assert {o['source'] for o in evidence.values()} == {'recorded'}
    assert not [a for a, _, _ in context.invoked if a == 'compare']
    assert len(db.results()) == 4


def test_a_bad_compare_result_is_a_failure_of_that_option(workflow, monkeypatch):
    original = FakeContext.invoke

    def invoke(self, function, event, name):
        result = original(self, function, event, name)
        if event['action'] == 'compare' and event['option'] == 'careful':
            return {**result, 'vm_state': 'running'}
        return result

    monkeypatch.setattr(FakeContext, 'invoke', invoke)
    _, db, _ = run(workflow, monkeypatch)
    evidence = json.loads(values(db.arena()[0])[':evidence_data'])
    assert evidence['careful']['source'] == 'recorded' and evidence['fast']['source'] == 'real'


def test_a_replay_with_different_dice_never_redraws(workflow, monkeypatch):
    checkpoints = {}
    run(workflow, monkeypatch, checkpoints, rolls=range(1000))
    recorded = json.loads(json.dumps(checkpoints))
    asked = []
    monkeypatch.setattr(workflow.secrets, 'randbelow', lambda bound: asked.append(bound) or 0)
    again = FakeContext(checkpoints, rooms_for(), workflow, db=FakeDB())
    monkeypatch.setattr(workflow, 'DB', again.db)
    workflow.handler({'id': RUN}, again)
    assert asked == []
    assert checkpoints == recorded
    assert 'plan-launch' in again.step_calls


def test_a_callback_for_another_round_is_rejected(workflow, monkeypatch):
    rooms = rooms_for()
    rooms['launch'] = json.dumps({'action': 'close', 'round': 'growth', 'players': ROOM})
    with pytest.raises(ValueError, match='launch'):
        run(workflow, monkeypatch, rooms=rooms)
    rooms['launch'] = json.dumps({'action': 'migrate', 'players': ROOM})
    with pytest.raises(ValueError):
        run(workflow, monkeypatch, rooms=rooms)


def test_a_garbage_player_row_does_not_stop_the_run(workflow, monkeypatch):
    room = [*ROOM, 'not a row', None, {'name': 'No id'}, {'id': 7}, dict(ROOM[0])]
    _, db, _ = run(workflow, monkeypatch, rooms=rooms_for(room))
    assert len(db.results()) == 4
    assert [row['id'] for row in db.results()[f'results#{RUN}#launch']['rows']] == [p['id'] for p in ROOM]


def test_a_players_value_that_is_not_a_list_fails_the_round_not_the_roster(workflow, monkeypatch):
    rooms = rooms_for()
    rooms['launch'] = json.dumps({'action': 'close', 'round': 'launch', 'players': 'nope'})
    with pytest.raises(ValueError):
        run(workflow, monkeypatch, rooms=rooms)


def test_an_expired_callback_publishes_expired_and_stops(workflow, monkeypatch):
    rooms = rooms_for()
    rooms['growth'] = workflow.CallbackTimeoutError('late')
    db = FakeDB()
    with pytest.raises(workflow.CallbackTimeoutError):
        run(workflow, monkeypatch, rooms=rooms, db=db)
    last = values(db.arena()[-1])
    assert last[':phase'] == 'expired' and last[':expround'] == 'growth'
    assert (last[':old0'], last[':old1']) == ('open', 'closing')
    assert set(db.results()) == {f'results#{RUN}#launch'}


def test_a_lost_conditional_write_is_not_an_error(workflow, monkeypatch):
    db = FakeDB()
    db.fail_phase = 'open'
    _, db, _ = run(workflow, monkeypatch, db=db)
    assert len(db.results()) == 4


def test_other_dynamodb_errors_are_raised(workflow, monkeypatch):
    class Broken(FakeDB):
        def update_item(self, **kwargs):
            raise ClientError({'Error': {'Code': 'ProvisionedThroughputExceededException'}}, 'UpdateItem')

    monkeypatch.setattr(workflow, 'register', lambda *args: None)
    monkeypatch.setattr(workflow, 'DB', Broken())
    with pytest.raises(ClientError):
        workflow.handler({'id': RUN}, FakeContext({}, rooms_for(), workflow))


@pytest.mark.parametrize('bad', ['', 'A' * 32, 'a' * 31, None, 7])
def test_the_run_id_must_be_32_lowercase_hex(workflow, bad):
    with pytest.raises(ValueError, match='Invalid'):
        workflow.handler({'id': bad}, FakeContext({}, {}, workflow))


def test_the_real_model_plays_four_rounds_with_a_garbage_row(monkeypatch):
    module = real_model()
    flow = load_workflow(monkeypatch, module)
    room = [{'id': 'ada1', 'name': 'Ada Byron', 'house': False, 'option': 'large', 'picks': {'launch': 'large'}},
            {'id': 'grace1', 'name': 'Grace Hopper', 'house': False, 'option': 'nonsense', 'picks': {}},
            {'id': 'house', 'name': 'House', 'house': True, 'picks': {}},
            'garbage', None, {'id': 'Bad Id!', 'name': 'x'}, {'id': 'ada1', 'name': 'Duplicate'}]
    rooms = {}
    for round_id in ROUND_IDS:
        options = [o['id'] for r in module.ROUNDS if r['id'] == round_id for o in r['options']]
        rooms[round_id] = [{**row, 'option': options[-1]} if isinstance(row, dict) and row.get('id') == 'ada1' else row
                           for row in room]
    context, db, _ = run(flow, monkeypatch, rooms=rooms, fail={('compare', 'fast')})
    results = db.results()
    assert sorted(results) == sorted(f'results#{RUN}#{r}' for r in ROUND_IDS)
    for round_id in ROUND_IDS:
        rows = results[f'results#{RUN}#{round_id}']['rows']
        assert len(rows) >= module.MIN_PLAYERS and [r['rank'] for r in rows][0] == 1
        assert len({row['id'] for row in rows}) == len(rows)
    names = [name for _, name in context.ops]
    assert len(names) == len(set(names))
    assert len([call for call in db.arena() if values(call)[':phase'] == 'final']) == 1


# ---- The presenter's Behind-the-scenes timeline ----------------------------------------------------------------

EXPECTED_IDS = ['registered', 'launch', 'migrate-careful', 'migrate-fast', 'compare-careful', 'compare-fast',
                *[f'{kind}-{round_id}' for round_id in ROUND_IDS for kind in ('open', 'close', 'plan', 'score', 'results')],
                'final']
LINE_KEYS = {'id', 't', 'tag', 'text', 'detail', 'log_group'}
DETAIL_KEYS = {'duration_s', 'vm_id', 'execution_arn', 'round', 'option', 'cart_status', 'charges', 'market', 'players',
               'picked', 'source', 'checkout_status', 'checkout_ms', 'bots', 'best_option', 'leader', 'run_id'}


def test_timeline_lines_are_appended_only_inside_steps_with_the_contract_expression(workflow, monkeypatch):
    monkeypatch.setenv('AWS_LAMBDA_LOG_GROUP_NAME', '/aws/lambda/durable')
    _, db, _ = run(workflow, monkeypatch)
    assert db.timeline_calls and all(call['in_step'] is True for call in db.timeline_calls)
    for call in db.timeline_calls:
        assert call['Key'] == {'pk': {'S': f'timeline#{RUN}'}}
        assert call['UpdateExpression'] == (
            'SET run_id = :run, #lines = list_append(if_not_exists(#lines, :empty), :new)')
        assert call['ConditionExpression'] == 'attribute_not_exists(#lines) OR size(#lines) < :cap'
        assert call['ExpressionAttributeNames'] == {'#lines': 'lines'}
        assert call['ExpressionAttributeValues'][':cap'] == {'N': '300'}
        assert call['ExpressionAttributeValues'][':empty'] == {'L': []}
        assert call['ExpressionAttributeValues'][':run'] == {'S': RUN}
        assert all(set(item) == {'S'} for item in call['ExpressionAttributeValues'][':new']['L'])


def test_every_expected_id_appears_exactly_once_in_a_normal_run(workflow, monkeypatch):
    _, db, _ = run(workflow, monkeypatch)
    ids = [item['id'] for item in db.timeline_lines()]
    assert sorted(ids) == sorted(EXPECTED_IDS)
    assert ids[0] == 'registered' and ids[-1] == 'final'
    assert ids.index('compare-fast') < ids.index('open-launch')
    for round_id in ROUND_IDS:
        assert ids.index(f'open-{round_id}') < ids.index(f'close-{round_id}') < ids.index(f'results-{round_id}')


def test_the_evidence_lines_get_their_own_checkpointed_step_before_round_one(workflow, monkeypatch):
    context, _, _ = run(workflow, monkeypatch)
    order = [name for _, name in context.ops]
    assert order.index('check-every-option') < order.index('timeline-evidence') < order.index('publish-open-launch')
    assert len(order) == len(set(order))


def test_every_line_has_only_allowed_keys_and_no_secrets(workflow, monkeypatch):
    monkeypatch.setenv('AWS_LAMBDA_LOG_GROUP_NAME', '/aws/lambda/durable')
    flow = load_workflow(monkeypatch, stub_model())
    monkeypatch.setattr(flow, 'EVALUATOR', 'arn:aws:lambda:ap-southeast-2:123456789012:function:evaluator:$LATEST')
    rooms = rooms_for([{**ROOM[0], 'token': 'secret-token', 'token_hash': 'hash', 'action_token': 'act'}, *ROOM[1:]])
    _, db, _ = run(flow, monkeypatch, rooms=rooms)
    lines = db.timeline_lines()
    assert lines
    for item in lines:
        assert set(item) <= LINE_KEYS and {'id', 't', 'tag', 'text'} <= set(item)
        assert item['tag'] in {'GAME', 'DURABLE', 'EVENT', 'MICROVM', 'WEB'}
        assert isinstance(item['t'], int) and len(item['text']) <= 100
        assert set(item.get('detail', {})) <= DETAIL_KEYS
    blob = json.dumps(lines).lower()
    for word in ('token', 'cookie', 'passphrase', 'presenter', 'callback', 'cb-await', 'secret', 'hash'):
        assert word not in blob
    groups = {item['id']: item.get('log_group') for item in lines}
    assert groups['registered'] == groups['open-launch'] == groups['final'] == '/aws/lambda/durable'
    assert groups['launch'] == groups['migrate-careful'] == groups['compare-fast'] == '/aws/lambda/evaluator'


def test_log_group_is_omitted_when_unknown(workflow, monkeypatch):
    monkeypatch.delenv('AWS_LAMBDA_LOG_GROUP_NAME', raising=False)
    _, db, _ = run(workflow, monkeypatch)  # EVALUATOR_ARN is 'arn:evaluator:1' here: no function name to parse
    assert all('log_group' not in item for item in db.timeline_lines())


def test_the_evidence_and_round_texts_are_human_and_durations_come_from_the_evaluator(monkeypatch):
    module = real_model()
    flow = load_workflow(monkeypatch, module)
    rooms = {round_id: [player('Ada Byron', option=[o['id'] for r in module.ROUNDS if r['id'] == round_id
                                                      for o in r['options']][-1]),
                        player('Grace Hopper', option=None), player('House', house=True)] for round_id in ROUND_IDS}
    _, db, _ = run(flow, monkeypatch, rooms=rooms)
    lines = {item['id']: item for item in db.timeline_lines()}
    assert lines['registered']['text'] == 'Execution registered'
    assert lines['launch']['text'] == 'Launch shop (real MicroVM)'  # the fake launch reports no checkout statuses
    assert lines['compare-careful']['text'] == 'Careful migration check: cart kept (200)'
    assert lines['compare-fast']['text'] == 'Fast migration check: cart LOST (200)'
    assert lines['compare-careful']['detail']['checkout_ms'] == 40
    assert lines['compare-careful']['detail']['duration_s'] == 0.04
    assert lines['migrate-careful']['detail']['vm_id'] == 'vm-migrate-careful'
    assert 'duration_s' not in lines['launch'].get('detail', {})
    assert lines['open-growth']['text'] == 'Round 2 · Growth open'
    assert lines['close-launch']['text'] == 'Close received: 1/2 picked'
    assert lines['close-launch']['detail'] == {'round': 'launch', 'players': 2, 'picked': 1}
    assert lines['plan-launch']['text'].startswith('Market draw ') and lines['plan-launch']['text'].count('%') == 1
    assert 'Bot ' in lines['plan-launch']['text'] and lines['plan-launch']['detail']['bots']
    assert lines['plan-blackfriday']['text'].split(';')[0].endswith('×')
    assert lines['score-launch']['text'].startswith('Best option ') and '; leader ' in lines['score-launch']['text']
    assert lines['results-launch']['text'] == 'Results published'
    assert lines['final']['text'] == 'Final results published'


def test_a_failed_launch_and_migrations_still_get_their_lines_with_the_source_recorded(workflow, monkeypatch):
    _, db, _ = run(workflow, monkeypatch, launch_fails=True, fail={('migrate', 'careful'), ('migrate', 'fast')})
    lines = {item['id']: item for item in db.timeline_lines()}
    assert sorted(item['id'] for item in db.timeline_lines()) == sorted(EXPECTED_IDS)
    assert lines['launch']['detail']['source'] == 'recorded' and 'recorded' in lines['launch']['text']
    for option in ('careful', 'fast'):
        assert lines[f'migrate-{option}']['detail']['source'] == 'recorded'
        assert lines[f'compare-{option}']['detail']['source'] == 'recorded'
        assert 'vm_id' not in lines[f'compare-{option}']['detail']
        assert 'duration_s' not in lines[f'compare-{option}']['detail']
    assert lines['compare-fast']['text'] == 'Fast migration check (recorded): cart LOST (404)'


def test_a_full_replay_writes_no_timeline_lines(workflow, monkeypatch):
    checkpoints = {}
    _, first, _ = run(workflow, monkeypatch, checkpoints=checkpoints)
    assert first.timeline_calls
    _, again, _ = run(workflow, monkeypatch, checkpoints=checkpoints)
    assert again.timeline_calls == [] and again.calls == []


@pytest.mark.parametrize('error', [
    RuntimeError('timeline table exploded'),
    ClientError({'Error': {'Code': 'ProvisionedThroughputExceededException'}}, 'UpdateItem'),
    ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'),
])
def test_a_failing_timeline_write_never_fails_or_changes_the_game(workflow, monkeypatch, error):
    clean_context, clean, _ = run(workflow, monkeypatch, rolls=[7] * 100)
    db = FakeDB()
    db.timeline_error = error
    context, db, result = run(workflow, monkeypatch, db=db, rolls=[7] * 100)
    assert db.timeline_calls and result == {'id': RUN, 'execution_arn': 'arn:durable'}
    assert db.calls == clean.calls and db.results() == clean.results()
    assert context.ops == clean_context.ops
    assert len([call for call in db.arena() if values(call)[':phase'] == 'final']) == 1


def test_a_line_that_cannot_be_built_is_swallowed_too(workflow, monkeypatch):
    def broken(*args):
        raise ValueError('cannot build')

    monkeypatch.setattr(workflow, 'results_lines', broken)
    monkeypatch.setattr(workflow, 'evidence_lines', broken)
    _, db, _ = run(workflow, monkeypatch)
    assert len(db.results()) == 4
    assert sorted(item['id'] for item in db.timeline_lines()) == sorted(
        ['registered', *[f'open-{r}' for r in ROUND_IDS], 'final'])


# ---- The `[WORKFLOW] <step>: <text>` log lines -----------------------------------------------------------------

STEP_LINE = re.compile(r'\[WORKFLOW\] ([a-z0-9-]+): (.+)')
STEP_NAMES = {'register-execution', 'timeline-evidence', 'publish-final',
              *[f'{kind}-{round_id}' for round_id in ROUND_IDS for kind in ('publish-open', 'plan', 'score', 'publish-results')]}


def capture_prints(workflow, monkeypatch, db):
    """Every print the workflow makes, with whether a durable step was running when it was made."""
    printed = []
    monkeypatch.setattr(workflow, 'print', lambda *args: printed.append((' '.join(map(str, args)), db.context.in_step)),
                        raising=False)
    return printed


def workflow_lines(printed):
    return [(text, in_step) for text, in_step in printed if text.startswith('[WORKFLOW]')]


def test_the_fake_rejects_bare_reserved_words_and_unused_or_missing_names():
    for word in ('lines', 'data', 'size', 'name', 'status', 'count'):
        with pytest.raises(ClientError, match=f'reserved keyword: {word}'):
            reject_reserved_words({'UpdateExpression': f'SET {word} = :x'})
        with pytest.raises(ClientError, match=f'reserved keyword: {word}'):
            reject_reserved_words({'ConditionExpression': f'attribute_not_exists({word}) OR size({word}) < :c'})
    reject_reserved_words({'UpdateExpression': 'SET #lines = :x', 'ConditionExpression': 'size(#lines) < :c',
                           'ExpressionAttributeNames': {'#lines': 'lines'}})
    with pytest.raises(ClientError, match='names used'):
        reject_reserved_words({'UpdateExpression': 'SET #lines = :x'})
    with pytest.raises(ClientError, match='names used'):
        reject_reserved_words({'UpdateExpression': 'SET x = :x', 'ExpressionAttributeNames': {'#lines': 'lines'}})


def test_every_dynamodb_write_in_a_run_avoids_bare_reserved_words(workflow, monkeypatch):
    _, db, _ = run(workflow, monkeypatch)
    assert db.timeline_calls and db.calls and all(call['in_step'] is True for call in db.timeline_calls)


def test_each_step_prints_exactly_one_workflow_line_and_only_inside_the_step(workflow, monkeypatch):
    db = FakeDB()
    printed = capture_prints(workflow, monkeypatch, db)
    run(workflow, monkeypatch, db=db)
    lines = workflow_lines(printed)
    assert all(in_step is True for _, in_step in lines)
    steps = [STEP_LINE.fullmatch(text).group(1) for text, _ in lines]
    assert sorted(steps) == sorted(STEP_NAMES)
    texts = dict((STEP_LINE.fullmatch(text).groups()) for text, _ in lines)
    assert texts['timeline-evidence'] == 'launch real, careful real (cart kept), fast real (cart lost)'
    assert texts['publish-open-growth'] == 'round open'
    assert re.fullmatch(r'market \d+, 0 bots, 2 players', texts['plan-launch'])


def test_a_recorded_fallback_is_named_in_the_evidence_line(workflow, monkeypatch):
    db = FakeDB()
    printed = capture_prints(workflow, monkeypatch, db)
    run(workflow, monkeypatch, db=db, launch_fails=True, fail={('migrate', 'fast')})
    texts = dict(STEP_LINE.fullmatch(text).groups() for text, _ in workflow_lines(printed))
    assert texts['timeline-evidence'] == 'launch recorded, careful real (cart kept), fast recorded (cart lost)'


def test_an_expired_close_prints_its_step_line(workflow, monkeypatch):
    db = FakeDB()
    printed = capture_prints(workflow, monkeypatch, db)
    expired = workflow.CallbackTimeoutError('timed out')
    with pytest.raises(workflow.CallbackTimeoutError):
        run(workflow, monkeypatch, db=db, rooms={**rooms_for(), 'launch': expired})
    assert [text for text, in_step in workflow_lines(printed) if in_step and 'expire-close-launch' in text] == [
        '[WORKFLOW] expire-close-launch: close callback timed out, run expired']


def test_a_full_replay_prints_no_workflow_lines(workflow, monkeypatch):
    checkpoints = {}
    db = FakeDB()
    printed = capture_prints(workflow, monkeypatch, db)
    run(workflow, monkeypatch, checkpoints=checkpoints, db=db)
    assert workflow_lines(printed)
    printed.clear()
    run(workflow, monkeypatch, checkpoints=checkpoints, db=FakeDB())
    assert printed == []


def test_workflow_lines_never_carry_secrets_ids_or_names(workflow, monkeypatch):
    db = FakeDB()
    printed = capture_prints(workflow, monkeypatch, db)
    rooms = rooms_for([{**ROOM[0], 'token': 'secret-token', 'action_token': 'act', 'passphrase': 'pp'}, *ROOM[1:]])
    run(workflow, monkeypatch, db=db, rooms=rooms)
    blob = '\n'.join(text for text, _ in workflow_lines(printed))
    assert blob
    for word in ('token', 'secret', 'cb-await', 'callback', RUN, RUN[:8], 'arn:', 'Ada', 'Grace', 'adabyron', 'House'):
        assert word not in blob
    assert re.search(r'[0-9a-f]{32}', blob) is None
