import gzip
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from submissions import Config, Intake, SubmissionError, make_server

OLD, NEW, LATE = '1' * 40, '2' * 40, '3' * 40
SECRET = os.urandom(32)
CUTOFF = datetime(2030, 1, 1, tzinfo=UTC)


class FixtureSource:
    def __init__(self):
        self.commits = {OLD: b'old', NEW: b'new', LATE: b'late'}

    def ahead(self, repository_id, base, head):
        return list(self.commits).index(head) > list(self.commits).index(base)

    def archive(self, repository_id, sha):
        if sha not in self.commits:
            raise SubmissionError('Archive unavailable')
        return gzip.compress(self.commits[sha], mtime=0)


@contextmanager
def running(intake):
    with make_server(0, intake, SECRET) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}/github/push'
        finally:
            server.shutdown()
            thread.join()


def push(url, before, after, *, signature=True, ref='refs/heads/submit/round-1', repo=42, **flags):
    body = json.dumps({
        'repository': {'id': repo}, 'ref': ref, 'before': before, 'after': after,
        'forced': False, 'deleted': False, **flags,
    }).encode()
    digest = hmac.digest(SECRET if signature else b'invalid', body, 'sha256').hex()
    request = Request(url, body, {
        'Content-Type': 'application/json', 'X-GitHub-Event': 'push', 'X-Hub-Signature-256': f'sha256={digest}',
    })
    try:
        reply = urlopen(request, timeout=3)
    except HTTPError as error:
        reply = error
    with reply:
        return reply.status, json.load(reply)['status']


@pytest.fixture
def contest(tmp_path):
    config = Config.model_validate({'teams': {'demo': 42}, 'rounds': {'round-1': CUTOFF.isoformat()}})
    now = [CUTOFF - timedelta(seconds=1)]
    return Intake(tmp_path / 'admissions.sqlite', config, FixtureSource(), lambda: now[0]), now


def test_frozen_export_uses_newer_pinned_archive_even_if_events_arrive_out_of_order(contest, tmp_path):
    intake, now = contest
    with running(intake) as url:
        assert push(url, OLD, NEW) == (202, 'queued')
        assert push(url, '0' * 40, OLD) == (202, 'queued')
        assert push(url, OLD, NEW) == (200, 'duplicate')
        assert intake.status('round-1')['teams'][0]['status'] == 'not_submitted'
        # Work accepted over HTTP survives restarting the intake process.
        intake = Intake(intake.database, intake.config, intake.source, intake.clock)
        assert intake.process_next()
        assert intake.process_next()
        assert intake.status('round-1')['teams'][0]['latest_push'] == {'commit': OLD, 'status': 'stale'}
        output = tmp_path / 'source.tar.gz'
        with pytest.raises(SubmissionError, match='still open'):
            intake.export('round-1', 'demo', output)
        assert not output.exists()
        now[0] = CUTOFF
        assert push(url, NEW, LATE) == (409, 'closed')

    result = intake.export('round-1', 'demo', output)
    assert result['commit'] == NEW
    assert result['sha256'] == sha256(output.read_bytes()).hexdigest()
    assert gzip.decompress(output.read_bytes()) == b'new'
    state = intake.status('round-1')
    assert state['status'] == 'closed'
    assert state['teams'][0]['commit'] == NEW


def test_wrong_signature_repo_ref_and_rewrites_cannot_submit(contest):
    intake, _ = contest
    with running(intake) as url:
        assert push(url, OLD, NEW, signature=False) == (401, 'invalid_signature')
        assert push(url, OLD, NEW, repo=99) == (200, 'ignored')
        assert push(url, OLD, NEW, ref='refs/heads/main') == (200, 'ignored')
        assert push(url, OLD, NEW, forced=True) == (409, 'rewrite_rejected')
        assert push(url, OLD, '0' * 40, deleted=True) == (409, 'rewrite_rejected')
    assert intake.status('round-1')['teams'][0]['status'] == 'not_submitted'


def test_archive_failure_blocks_export_until_the_same_submission_is_retried(contest, tmp_path):
    intake, now = contest
    with running(intake) as url:
        assert push(url, '0' * 40, OLD) == (202, 'queued')
        assert intake.process_next()
        missing = '9' * 40
        assert push(url, OLD, missing) == (202, 'queued')
        assert intake.process_next()
        assert intake.status('round-1')['teams'][0]['latest_push'] == {'commit': missing, 'status': 'unavailable'}
        assert intake.status('round-1')['teams'][0]['commit'] == OLD
        now[0] = CUTOFF
        state = intake.status('round-1')
        assert state['status'] == 'blocked'
        assert state['teams'][0]['unresolved'] == [{'commit': missing, 'status': 'unavailable'}]
        output = tmp_path / 'source.tar.gz'
        with pytest.raises(SubmissionError, match='unavailable'):
            intake.export('round-1', 'demo', output)
        assert not output.exists()
        intake.source.commits[missing] = b'recovered'
        assert push(url, OLD, missing) == (202, 'queued')
        assert intake.process_next()
    assert intake.export('round-1', 'demo', output)['commit'] == missing


def test_blocked_team_cannot_make_another_teams_timely_push_miss_cutoff(tmp_path):
    now = [CUTOFF - timedelta(seconds=1)]
    started, unblock = Event(), Event()

    class SlowSource(FixtureSource):
        def archive(self, repository_id, sha):
            if repository_id == 42:
                started.set()
                if not unblock.wait(5):
                    raise AssertionError('Test did not release blocked archive')
            return super().archive(repository_id, sha)

    config = Config.model_validate({
        'teams': {'first': 42, 'second': 43}, 'rounds': {'round-1': CUTOFF.isoformat()},
    })
    intake = Intake(tmp_path / 'admissions.sqlite', config, SlowSource(), lambda: now[0])
    early = tmp_path / 'early.tar.gz'
    with running(intake) as url, ThreadPoolExecutor(max_workers=1) as worker:
        assert push(url, '0' * 40, OLD, repo=43) == (202, 'queued')
        assert intake.process_next()
        assert push(url, '0' * 40, OLD, repo=42) == (202, 'queued')
        blocked = worker.submit(intake.process_next)
        try:
            assert started.wait(5)
            assert push(url, OLD, NEW, repo=43) == (202, 'queued')
            now[0] = CUTOFF
            assert push(url, NEW, LATE, repo=43) == (409, 'closed')
            settling = intake.status('round-1')['status']
            try:
                intake.export('round-1', 'second', early)
            except SubmissionError:
                pass
        finally:
            unblock.set()
        assert blocked.result(timeout=5)
        assert intake.process_next()

    result = intake.export('round-1', 'second', tmp_path / 'final.tar.gz')
    assert result['commit'] == NEW
    assert settling == 'settling'
    assert not early.exists()
    assert intake.status('round-1')['status'] == 'closed'
