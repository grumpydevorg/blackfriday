import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import watch  # noqa: E402
import watch_web  # noqa: E402

RUN = '0123456789abcdef0123456789abcdef'


@pytest.fixture
def jsonl(tmp_path, monkeypatch):
    path = tmp_path / 'bf-watch.jsonl'
    monkeypatch.setenv('BF_WATCH_JSONL', str(path))
    return path


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize('message, tag, level, text, detail', [
    ('[EVENT] compare careful: VM running, endpoint ready in 3.21 s', 'EVENT', 'info',
     'compare careful: VM running, endpoint ready in 3.21 s', {'stage': 'compare', 'option': 'careful'}),
    ('[EVENT] launch -: boot failed with code 1', 'EVENT', 'error', 'launch -: boot failed with code 1',
     {'stage': 'launch', 'option': None}),
    ('[VM] PHP shop ready in 1.92 s', 'VM', 'info', 'PHP shop ready in 1.92 s', {}),
    ('[PHP] GET /cart/cart-saved 404 12 ms', 'PHP', 'warn', 'GET /cart/cart-saved 404 12 ms',
     {'method': 'GET', 'path': '/cart/cart-saved', 'status': 404, 'ms': 12.0}),
    ('[PHP] POST /checkout 500 80 ms', 'PHP', 'error', 'POST /checkout 500 80 ms',
     {'method': 'POST', 'path': '/checkout', 'status': 500, 'ms': 80.0}),
    ('[PHP] GET / 200 4 ms', 'PHP', 'info', 'GET / 200 4 ms', {'method': 'GET', 'path': '/', 'status': 200, 'ms': 4.0}),
    ('[WORKFLOW] open-round: callback registered', 'WORKFLOW', 'info', 'open-round: callback registered',
     {'step': 'open-round'}),
    ('[WORKFLOW] close-round: fallback to recorded evidence', 'WORKFLOW', 'warn',
     'close-round: fallback to recorded evidence', {'step': 'close-round'}),
    # Lambda text format and JSON-wrapped records
    ('[INFO]\t2026-11-25T01:02:03Z\tabc-req\t[VM] cart saved', 'VM', 'info', 'cart saved', {}),
    (json.dumps({'level': 'ERROR', 'message': '[WORKFLOW] judge: step exploded'}), 'WORKFLOW', 'error',
     'judge: step exploded', {'step': 'judge'}),
])
def test_contract_lines(message, tag, level, text, detail):
    assert watch.parse_contract(message) == {'tag': tag, 'level': level, 'text': text, 'detail': detail}


def test_non_contract_lines_are_not_contract():
    assert watch.parse_contract('START RequestId: abc Version: $LATEST') is None
    assert watch.parse_contract('[EVENTUAL] nope') is None


ACCESS = {'bf': 'access', 'level': 'debug', 'method': 'GET', 'route': '/api/state', 'status': 200, 'ms': 12,
          'phase': 'open', 'round': 'growth'}


@pytest.mark.parametrize('message', [
    json.dumps(ACCESS),
    json.dumps({'level': 'INFO', 'message': json.dumps(ACCESS)}),
    json.dumps({'type': 'function', 'record': ACCESS}),
    json.dumps({'type': 'function', 'record': json.dumps(ACCESS)}),
    '2026-11-25T01:02:03.000Z\tabc\tINFO\t' + json.dumps(ACCESS),
])
def test_web_access_in_every_shape(message):
    got = watch.parse_access(message)
    assert got == {'method': 'GET', 'route': '/api/state', 'status': 200, 'ms': 12, 'phase': 'open',
                   'round': 'growth', 'level': 'debug'}


def test_web_access_level_and_whitelist():
    assert watch.parse_access(json.dumps({'bf': 'access', 'method': 'POST', 'route': '/api/pick', 'status': 409,
                                          'ms': 3}))['level'] == 'warn'
    assert watch.parse_access(json.dumps({'bf': 'access', 'method': 'GET', 'route': '/x', 'status': 503,
                                          'ms': 3}))['level'] == 'error'
    leaked = watch.parse_access(json.dumps({**ACCESS, 'token': 'secret', 'player': 'Ann', 'cookie': 'c'}))
    assert not {'token', 'player', 'cookie'} & set(leaked)
    assert watch.parse_access('{"type":"platform.invokeSummary"}') is None
    assert watch.parse_access('plain line') is None


def test_jsonl_line_shape(jsonl, capsys):
    watch.emit('GAME', 'watching run 01234567 · open Growth · 3 players')
    watch.emit('PHP', 'GET /a 404 3 ms', level='warn', source='black-friday-four-evaluator',
               detail={'status': 404})
    watch.emit('ERROR', 'boom')
    first, second, third = records(jsonl)
    assert set(first) == {'ts', 'tag', 'level', 'source', 'text', 'detail'}
    assert first['tag'] == 'GAME' and first['level'] == 'info' and first['source'] == 'arena'
    assert first['detail'] == {}
    assert datetime.fromisoformat(first['ts'].replace('Z', '+00:00')).tzinfo == timezone.utc
    assert (second['level'], second['source'], second['detail']) == ('warn', 'black-friday-four-evaluator', {'status': 404})
    assert (third['tag'], third['level']) == ('ERROR', 'error')
    assert capsys.readouterr().out.count('\n') == 3


def test_debug_goes_to_jsonl_not_stdout(jsonl, capsys):
    watch.handle_log('WEB', json.dumps(ACCESS), 'web/black-friday-four')
    assert capsys.readouterr().out == ''
    [event] = records(jsonl)
    assert (event['tag'], event['level'], event['source']) == ('WEB', 'debug', 'web/black-friday-four')
    assert event['text'] == 'GET /api/state 200 12 ms · open growth'
    assert event['detail']['route'] == '/api/state'
    watch.handle_log('WEB', json.dumps({**ACCESS, 'level': 'info', 'route': '/api/pick'}), 'web/black-friday-four')
    assert capsys.readouterr().out == '', 'routine access lines stay off the room screen'
    assert records(jsonl)[-1]['detail']['route'] == '/api/pick'
    watch.handle_log('WEB', json.dumps({**ACCESS, 'level': 'error', 'route': '/api/pick', 'status': 503}),
                     'web/black-friday-four')
    out = capsys.readouterr().out
    assert '[WEB]' in out and '/api/pick' in out


def test_contract_lines_become_their_own_events(jsonl, capsys):
    watch.handle_log('EVENT', '[VM] PHP shop ready in 1.92 s', 'black-friday-four-evaluator')
    watch.handle_log('WORKFLOW', '[WORKFLOW] open-round: started', 'black-friday-four-workflow')
    assert [(e['tag'], e['source']) for e in records(jsonl)] == [
        ('VM', 'black-friday-four-evaluator'), ('WORKFLOW', 'black-friday-four-workflow')]
    assert capsys.readouterr().out.count('\n') == 2


def test_names_and_links_stay_out_of_the_jsonl(jsonl, capsys):
    watch.emit('GAME', '+1 joined (3 players): Ann', public='+1 joined (3 players)')
    watch.emit('VM', f'run {RUN} https://pay.example/x?X-Amz-Signature=abc done')
    first, second = records(jsonl)
    assert 'Ann' not in first['text']
    assert RUN not in second['text'] and RUN[:8] in second['text'] and 'X-Amz' not in second['text']
    assert 'Ann' in capsys.readouterr().out.splitlines()[0]
    top = watch.results_line({'round': 'growth', 'market': 1, 'rows': [{'name': 'Ann', 'cumulative_cents': 500}]},
                             names=False)
    assert 'Ann' not in top


def test_log_source_is_the_short_group_name():
    arn = 'arn:aws:logs:ap-southeast-2:075168677499:log-group:/aws/lambda/web/black-friday-four.local:*'
    assert watch.log_source(arn) == 'web/black-friday-four.local'
    assert watch.log_source('/aws/lambda/black-friday-four-workflow') == 'black-friday-four-workflow'


def test_tail_lines_and_follow(tmp_path):
    path = tmp_path / 'log.jsonl'
    assert watch_web.tail_lines(path) == ([], 0)
    path.write_text(''.join(f'{{"n":{i}}}\n' for i in range(50)) + '{"n":')
    lines, offset = watch_web.tail_lines(path, 5)
    assert lines == [f'{{"n":{i}}}' for i in range(45, 50)]
    assert path.read_bytes()[offset:] == b'{"n":'

    seen: list = []
    clock = iter(range(0, 1000))
    gen = watch_web.follow(path, backlog=2, poll=0, heartbeat=3, sleep=lambda _: None, clock=lambda: next(clock))
    seen += [next(gen), next(gen)]
    assert seen == ['{"n":48}', '{"n":49}']
    with open(path, 'a') as handle:
        handle.write('50}\n')
    assert next(gen) == '{"n":50}'
    assert next(gen) is None                       # heartbeat after silence
    path.write_text('{"n":"new"}\n')                # truncated: read again from the start
    assert next(gen) == '{"n":"new"}'
    fresh = tmp_path / 'fresh.jsonl'                 # rotated: a different file is moved into place
    fresh.write_text('{"n":"rotated"}\n{"n":"x"}\n')
    fresh.replace(path)
    assert [next(gen), next(gen)] == ['{"n":"rotated"}', '{"n":"x"}']
