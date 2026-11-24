"""Run the Durable handler against a fake context that checkpoints steps, as the real SDK does."""

import functools
import json
import sys
import types
from pathlib import Path

import pytest

import scoring

sys.path.insert(0, str(Path(__file__).parent))

RUN = 'a' * 32


def team(name, option, house=False):
    return {'id': name.lower().replace(' ', ''), 'name': name, 'house': house, 'option': option}


HOUSES = [team('House A', 'lift', True), team('House B', 'refactor', True)]


def stub_sdk(monkeypatch):
    sdk = types.ModuleType('aws_durable_execution_sdk_python')
    sdk.CallbackTimeoutError = type('CallbackTimeoutError', (Exception,), {})
    sdk.DurableContext = object
    sdk.durable_execution = lambda function: function
    config = types.ModuleType('aws_durable_execution_sdk_python.config')
    config.CallbackConfig = lambda timeout: ('callback', timeout)
    config.Duration = types.SimpleNamespace(from_hours=lambda hours: hours)
    monkeypatch.setitem(sys.modules, 'aws_durable_execution_sdk_python', sdk)
    monkeypatch.setitem(sys.modules, 'aws_durable_execution_sdk_python.config', config)
    monkeypatch.setenv('RUN_TABLE', 'table')
    monkeypatch.setenv('EVALUATOR_ARN', 'arn:evaluator:1')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'ap-southeast-2')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'x')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'x')
    sys.modules.pop('workflow', None)
    import workflow

    return workflow


@functools.cache
def option_result(option):
    from evaluator import Rehearsal

    capacity = Rehearsal.parse({'id': RUN, 'action': 'compare', 'team': option, 'kind': 'preserve'}).capacity()
    return {'cart_status': 200, 'maya_sales': 2, 'returns_next_week': True, 'return_probability': 75,
            'architecture': capacity}


class FakeContext:
    """Steps are checkpointed by name: a replay returns the recorded result and never calls the function."""

    def __init__(self, checkpoints, roster):
        self.checkpoints, self.roster = checkpoints, roster
        self.events, self.step_calls, self.branch_names = [], [], []
        self.execution_context = types.SimpleNamespace(durable_execution_arn='arn:durable')

    def step(self, function, name):
        self.step_calls.append(name)
        if name not in self.checkpoints:
            self.checkpoints[name] = json.loads(json.dumps(function(None)))
        return self.checkpoints[name]

    def create_callback(self, name, config):
        context = self

        class Callback:
            callback_id = f'cb-{name}'

            def result(self):
                if name == 'await-migrate':
                    return json.dumps({'action': 'migrate', 'teams': context.roster})
                return json.dumps({'action': 'compare'})

        return Callback()

    def invoke(self, function, event, name):
        self.events.append(event)
        self.branch_names.append(name)
        if event['action'] == 'launch':
            return {'vm_state': 'terminated', 'checkpoint': 'c' * 64, 'vm_id': 'vm-launch',
                    'launch': {'profit_cents': 900}}
        base = {'vm_state': 'terminated', 'vm_id': f'vm-{event["action"]}-{event["team"]}', 'kind': 'preserve',
                'health_status': 200, 'history_status': 200}
        return base if event['action'] == 'migrate' else {**base, **option_result(event['team'])}

    def parallel(self, functions, name):
        self.branch_names.append(name)
        results = [function(self) for function in functions]
        return types.SimpleNamespace(throw_if_error=lambda: None, get_results=lambda: results)


def run(workflow, monkeypatch, checkpoints, roster, rolls):
    published = []
    monkeypatch.setattr(workflow, 'publish', lambda run_id, arn, previous, phase, fields: published.append((phase, fields)))
    monkeypatch.setattr(workflow, 'register', lambda *args: None)
    draws = iter(rolls)
    monkeypatch.setattr(workflow.secrets, 'randbelow', lambda bound: next(draws) % bound)
    context = FakeContext(checkpoints, roster)
    workflow.handler({'id': RUN}, context)
    return context, published


def scoreboard(published):
    return json.loads(next(fields['outcomes_data'] for phase, fields in published if phase == 'compared'))


def test_the_draws_are_one_checkpointed_step_and_a_replay_never_redraws(monkeypatch):
    workflow = stub_sdk(monkeypatch)
    roster = HOUSES + [team('Scalers', 'scale')]
    checkpoints = {}
    context, published = run(workflow, monkeypatch, checkpoints, roster, rolls=[4, 29, 9] + [50] * 20)
    assert context.step_calls.count('migration-risk') == 1
    plan = checkpoints['migration-risk']
    # Three teams, so three bots; their options come first (draws 4, 29, 9 -> 1, 2, 0), then six rolls.
    assert [t['name'] for t in plan['teams']] == ['House A', 'House B', 'Scalers', 'Bot Lambo', 'Bot Turbo', 'Bot Kevin']
    assert len(plan['risks']) == 6
    # The checkpoint records every draw; a replay with different dice reads it back and never draws.
    again, replayed = run(workflow, monkeypatch, checkpoints, roster, rolls=[0] * 50)
    assert checkpoints['migration-risk'] == plan
    assert again.events == context.events
    assert again.step_calls.count('migration-risk') == 1


def test_one_migrate_and_one_compare_microvm_per_option_however_many_teams(monkeypatch):
    workflow = stub_sdk(monkeypatch)
    options = ['refactor'] * 20 + ['lift'] * 12 + ['scale'] * 8
    roster = HOUSES + [team(f'Player {n}', option) for n, option in enumerate(options)]
    context, published = run(workflow, monkeypatch, {}, roster, rolls=[37] * 400)
    stages = [(e['action'], e['team']) for e in context.events if e['action'] != 'launch']
    assert sorted(stages) == sorted((action, option) for action in ('migrate', 'compare') for option in ('lift', 'scale', 'refactor'))
    assert all(set(e) == {'id', 'action', 'team', 'kind'} and e['kind'] == 'preserve' for e in context.events if e['action'] != 'launch')
    assert 'migrate-lift' in context.branch_names and 'compare-refactor' in context.branch_names
    assert len(set(context.branch_names)) == len(context.branch_names), 'durable step and branch names are unique'
    rows = scoreboard(published)
    assert len(rows) == 42 + 0 and {row['option'] for row in rows} == {'lift', 'scale', 'refactor'}


def test_only_the_options_in_use_are_judged(monkeypatch):
    workflow = stub_sdk(monkeypatch)
    roster = [team('House A', 'lift', True), team('House B', 'lift', True)] + [team(f'Player {n}', 'lift') for n in range(5)]
    context, _ = run(workflow, monkeypatch, {}, roster, rolls=[50] * 100)
    assert sorted({e['team'] for e in context.events if e['action'] != 'launch'}) == ['lift']
    assert len([e for e in context.events if e['action'] == 'compare']) == 1


def test_each_team_keeps_its_own_roll_and_broke_is_roll_under_risk(monkeypatch):
    workflow = stub_sdk(monkeypatch)
    roster = [
        team('House A', 'lift', True), team('House B', 'refactor', True),
        team('Edge Five', 'lift'), team('Edge Thirty', 'refactor'), team('Under Ten', 'scale'), team('Over Ten', 'scale'),
    ]
    # Six teams, no bots, so exactly six rolls: 5 is not under 5, 4 is; 30 is not under 30, 29 is; 9 is under 10, 10 is not.
    context, published = run(workflow, monkeypatch, {}, roster, rolls=[5, 29, 5, 30, 9, 10])
    rows = {row['id']: row for row in scoreboard(published)}
    assert [(rows[i]['roll'], rows[i]['risk'], rows[i]['broke']) for i in ('housea', 'houseb', 'edgefive', 'edgethirty', 'underten', 'overten')] == [
        (5, 5, False), (29, 30, True), (5, 5, False), (30, 30, False), (9, 10, True), (10, 10, False)]
    # Teams on the same option share the real checks but not the outcome.
    assert rows['underten']['cumulative_cents'] == -1800 and rows['overten']['cumulative_cents'] == 19200
    assert rows['houseb']['cumulative_cents'] == 2500 and rows['edgethirty']['cumulative_cents'] == 23500
    assert rows['housea']['cumulative_cents'] == rows['edgefive']['cumulative_cents'] == 14500
    assert [row['rank'] for row in scoreboard(published)][:2] == [1, 2]


def test_a_room_of_zero_gets_two_houses_and_four_bots_labelled_as_bots(monkeypatch):
    workflow = stub_sdk(monkeypatch)
    context, published = run(workflow, monkeypatch, {}, list(HOUSES), rolls=[7] * 20)
    rows = scoreboard(published)
    assert len(rows) == 6
    assert sorted(row['name'] for row in rows if row['bot']) == sorted(scoring.BOTS[:4])
    assert sorted(row['name'] for row in rows if not row['bot']) == ['House A', 'House B']


def test_no_bots_when_six_or_more_teams_are_in(monkeypatch):
    workflow = stub_sdk(monkeypatch)
    roster = HOUSES + [team(f'Player {n}', 'scale') for n in range(4)]
    _, published = run(workflow, monkeypatch, {}, roster, rolls=[50] * 20)
    assert not any(row['bot'] for row in scoreboard(published))
    assert len(scoreboard(published)) == 6


def test_draw_risks_rolls_once_per_team_in_order_and_broke_is_roll_under_risk():
    asked, rolls = [], iter([0, 4, 5, 9, 10, 29, 30, 99])
    teams = [{'id': f't{n}', 'option': option} for n, option in enumerate(
        ['lift', 'lift', 'lift', 'scale', 'scale', 'refactor', 'refactor', 'refactor'])]

    def randbelow(bound):
        asked.append(bound)
        return next(rolls)

    result = scoring.draw_risks(teams, randbelow)
    assert asked == [100] * 8
    assert [result[t['id']]['broke'] for t in teams] == [True, True, False, True, False, True, False, False]
    assert [(r['roll'], r['risk']) for r in result.values()][:3] == [(0, 5), (4, 5), (5, 5)]


def test_the_default_roll_source_is_secrets_randbelow_in_range():
    teams = [{'id': 'a', 'option': 'lift'}]
    for _ in range(200):
        assert 0 <= scoring.draw_risks(teams)['a']['roll'] < 100
    assert scoring.draw_risks.__defaults__[0].__module__ == 'secrets'
    assert scoring.plan_round.__defaults__[0].__module__ == 'secrets'


@pytest.mark.parametrize('bad', [
    [{'id': 'housea', 'name': 'House A', 'house': True, 'option': 'omit'}],
    [{'id': 'housea', 'name': 'House A', 'house': True, 'release': 'A', 'kind': 'preserve', 'architecture': {}}],
])
def test_the_workflow_rejects_a_roster_that_is_not_in_the_option_shape(monkeypatch, bad):
    workflow = stub_sdk(monkeypatch)
    context = FakeContext({}, bad)
    monkeypatch.setattr(workflow, 'publish', lambda *args: None)
    monkeypatch.setattr(workflow, 'register', lambda *args: None)
    with pytest.raises(ValueError):
        workflow.handler({'id': RUN}, context)
    assert 'migration-risk' not in context.step_calls
