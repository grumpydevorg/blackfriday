# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic==2.13.4"]
# ///
"""Accept GitHub submission pushes as inert, pinned source archives. Never run them."""

import argparse
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from typing import Annotated, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

LOG = logging.getLogger(__name__)
WEBHOOK = '/github/push'
REF_PREFIX = 'refs/heads/submit/'
MAX_PUSH = 2 * 1024 * 1024
MAX_ARCHIVE = 8 * 1024 * 1024
ZERO_SHA = '0' * 40
Sha = Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{40}$')]
Team = Annotated[str, StringConstraints(pattern=r'^[a-z][a-z0-9-]{0,31}$')]
Round = Annotated[str, StringConstraints(pattern=r'^round-[1-9][0-9]{0,2}$')]
RepositoryId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid')
    teams: Annotated[dict[Team, RepositoryId], Field(min_length=1, max_length=64)]
    rounds: Annotated[dict[Round, AwareDatetime], Field(min_length=1, max_length=16)]


class Repository(BaseModel):
    model_config = ConfigDict(strict=True)
    id: RepositoryId


class Push(BaseModel):
    model_config = ConfigDict(strict=True)
    repository: Repository
    ref: Annotated[str, Field(max_length=1024)]
    before: Sha
    after: Sha
    forced: bool
    deleted: bool


class Comparison(BaseModel):
    status: str


class SubmissionError(Exception):
    pass


class Source(Protocol):
    def ahead(self, repository_id: int, base: str, head: str) -> bool: ...
    def archive(self, repository_id: int, sha: str) -> bytes: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token: str | None) -> None:
        self.headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if token:
            self.headers['Authorization'] = f'Bearer {token}'
        self.http = build_opener(NoRedirect())

    def api(self, repository_id: int, path: str):
        # Address the registered repository by ID, not a user-supplied URL or reusable name.
        request = Request(f'https://api.github.com/repositories/{repository_id}/{path}', headers=self.headers)
        return self.http.open(request, timeout=8)

    def ahead(self, repository_id: int, base: str, head: str) -> bool:
        with self.api(repository_id, f'compare/{base}...{head}?per_page=1') as reply:
            body = reply.read(MAX_ARCHIVE + 1)
        if len(body) > MAX_ARCHIVE:
            raise SubmissionError('GitHub comparison is too large')
        return Comparison.model_validate_json(body).status == 'ahead'

    def archive(self, repository_id: int, sha: str) -> bytes:
        try:
            with self.api(repository_id, f'tarball/{sha}'):
                raise SubmissionError('GitHub archive did not redirect')
        except HTTPError as error:
            with error:
                location = error.headers.get('Location', '')
                if error.code != 302:
                    raise SubmissionError(f'GitHub archive returned HTTP {error.code}') from None
        url = urlsplit(location)
        if url.scheme != 'https' or url.netloc != 'codeload.github.com' or url.path.rsplit('/', 1)[-1] != sha:
            raise SubmissionError('GitHub archive redirected outside the pinned source')
        # The GitHub API token is never forwarded. Private archives use a signed codeload URL.
        with self.http.open(Request(location), timeout=8) as reply:
            body = reply.read(MAX_ARCHIVE + 1)
        if len(body) > MAX_ARCHIVE or not body.startswith(b'\x1f\x8b'):
            raise SubmissionError('Expected a gzip archive of at most 8 MiB')
        return body


def utc_now() -> datetime:
    return datetime.now(UTC)


class Intake:
    def __init__(
        self, database: Path, config: Config, source: Source, clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database, self.config, self.source, self.clock = database, config, source, clock
        self.wake = Event()
        self.teams = {repo: team for team, repo in config.teams.items()}
        if len(self.teams) != len(config.teams):
            raise SubmissionError('Assign each repository to exactly one team')
        identity = json.dumps(config.model_dump(mode='json'), sort_keys=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS releases (
                    round TEXT NOT NULL, team TEXT NOT NULL, sha TEXT NOT NULL, archive BLOB NOT NULL,
                    digest TEXT NOT NULL, accepted_at TEXT NOT NULL, PRIMARY KEY (round, team)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY, round TEXT NOT NULL, team TEXT NOT NULL, sha TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(round, team, sha)
                );
                CREATE INDEX IF NOT EXISTS queued_deliveries ON deliveries(id) WHERE state = 'queued';
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (identity,))
            if db.execute('SELECT config FROM settings WHERE id = 1').fetchone()[0] != identity:
                raise SubmissionError('The database belongs to a different contest configuration')

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=3)

    def cutoff(self, round_name: str) -> datetime:
        if round_name not in self.config.rounds:
            raise SubmissionError('Unknown round')
        return self.config.rounds[round_name]

    def enqueue(self, push: Push) -> tuple[int, str]:
        if not push.ref.startswith(REF_PREFIX):
            return 200, 'ignored'
        round_name = push.ref.removeprefix(REF_PREFIX)
        team = self.teams.get(push.repository.id)
        cutoff = self.config.rounds.get(round_name)
        if team is None or cutoff is None:
            return 200, 'ignored'
        if push.forced or push.deleted or push.after == ZERO_SHA:
            return 409, 'rewrite_rejected'
        key = (round_name, team, push.after)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute(
                'SELECT state FROM deliveries WHERE round = ? AND team = ? AND sha = ?', key,
            ).fetchone()
            if prior and prior[0] != 'unavailable':
                return 200, 'duplicate'
            if prior:
                # Recover the same eligible SHA, even after the round stops taking new pushes.
                db.execute("UPDATE deliveries SET state = 'queued' WHERE round = ? AND team = ? AND sha = ?", key)
            else:
                # The committed delivery, not the subsequent archive fetch, establishes eligibility.
                if self.clock() >= cutoff:
                    return 409, 'closed'
                count = db.execute('SELECT count(*) FROM deliveries WHERE round = ? AND team = ?', key[:2]).fetchone()[0]
                if count >= 128:
                    return 429, 'submission_limit'
                db.execute('''
                    INSERT INTO deliveries (round, team, sha, payload, state) VALUES (?, ?, ?, ?, 'queued')
                ''', (*key, push.model_dump_json()))
        self.wake.set()
        return 202, 'queued'

    def process_next(self) -> bool:
        with closing(self.connect()) as db:
            item = db.execute("""
                SELECT id, round, team, payload FROM deliveries WHERE state = 'queued' ORDER BY id LIMIT 1
            """).fetchone()
            if item is None:
                return False
            delivery_id, round_name, team, payload = item
            try:
                state = self.pin(round_name, team, Push.model_validate_json(payload))
            except (OSError, ValidationError, SubmissionError, sqlite3.Error) as error:
                # Upstream HTTP and validation errors can contain credentials or input.
                LOG.warning('Submission %s unavailable: %s', delivery_id, type(error).__name__)
                state = 'unavailable'
            with db:
                db.execute('UPDATE deliveries SET state = ? WHERE id = ?', (state, delivery_id))
        return True

    def run_worker(self, stop: Event) -> None:
        while not stop.is_set():
            self.wake.clear()
            try:
                while not stop.is_set() and self.process_next():
                    pass
            except sqlite3.Error as error:
                LOG.warning('Submission queue unavailable: %s', type(error).__name__)
                stop.wait(1)
                continue
            if not stop.is_set():
                self.wake.wait()

    def pin(self, round_name: str, team: str, push: Push) -> str:
        key = (round_name, team)
        head = 'SELECT sha FROM releases WHERE round = ? AND team = ?'
        with closing(self.connect()) as db:
            prior = db.execute(head, key).fetchone()
            if prior == (push.after,):
                return 'accepted'  # The worker may have stopped after storing the archive.
            # A signed non-forced push advances 'before'. If we missed a push or
            # received events out of order, ask GitHub about actual ancestry.
            if prior and prior[0] != push.before and not self.source.ahead(push.repository.id, prior[0], push.after):
                return 'stale'
            archive = self.source.archive(push.repository.id, push.after)
            digest = sha256(archive).hexdigest()
            with db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute(head, key).fetchone() != prior:
                    raise SubmissionError('Another worker changed the release')
                accepted_at = self.clock()
                db.execute('''
                    INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(round, team) DO UPDATE SET sha=excluded.sha, archive=excluded.archive,
                        digest=excluded.digest, accepted_at=excluded.accepted_at
                ''', (*key, push.after, archive, digest, accepted_at.isoformat()))
        return 'accepted'

    def status(self, round_name: str) -> dict:
        cutoff = self.cutoff(round_name)
        open_for_pushes = self.clock() < cutoff
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            releases = {
                team: {'status': 'accepted', 'commit': commit, 'sha256': digest, 'accepted_at': accepted_at}
                for team, commit, digest, accepted_at in db.execute(
                    'SELECT team, sha, digest, accepted_at FROM releases WHERE round = ?', (round_name,),
                )
            }
            latest_push = {
                team: {'commit': commit, 'status': state}
                for team, commit, state in db.execute('''
                    SELECT team, sha, state FROM deliveries WHERE id IN (
                        SELECT max(id) FROM deliveries WHERE round = ? GROUP BY team
                    )
                ''', (round_name,))
            }
            unresolved: dict[str, list[dict[str, str]]] = {}
            for team, commit, state in db.execute('''
                SELECT team, sha, state FROM deliveries
                WHERE round = ? AND state IN ('queued', 'unavailable') ORDER BY id
            ''', (round_name,)):
                unresolved.setdefault(team, []).append({'commit': commit, 'status': state})
        if open_for_pushes:
            round_status = 'open'
        elif any(push['status'] == 'queued' for pushes in unresolved.values() for push in pushes):
            round_status = 'settling'
        else:
            round_status = 'blocked' if unresolved else 'closed'
        return {
            'schema': 1, 'round': round_name, 'cutoff': cutoff.isoformat(),
            'status': round_status,
            'teams': [
                {
                    'team': team, **releases.get(team, {'status': 'not_submitted'}),
                    'latest_push': latest_push.get(team),
                    'unresolved': unresolved.get(team, []),
                }
                for team in sorted(self.config.teams)
            ],
        }

    def export(self, round_name: str, team: str, output: Path) -> dict:
        cutoff = self.cutoff(round_name)
        with closing(self.connect()) as db, db:
            # Serialize with a receipt that acquired the write lock just before cutoff.
            db.execute('BEGIN IMMEDIATE')
            if self.clock() < cutoff:
                raise SubmissionError('The round is still open')
            pending = db.execute('''
                SELECT sha, state FROM deliveries
                WHERE round = ? AND team = ? AND state IN ('queued', 'unavailable') ORDER BY id LIMIT 1
            ''', (round_name, team)).fetchone()
            if pending:
                commit, state = pending
                raise SubmissionError(f'Submission {commit} is {state}; no frozen export yet')
            row = db.execute(
                'SELECT sha, archive, digest FROM releases WHERE round = ? AND team = ?', (round_name, team),
            ).fetchone()
        if row is None:
            raise SubmissionError('No accepted submission for this team and round')
        commit, archive, digest = row
        if sha256(archive).hexdigest() != digest:
            raise SubmissionError('Stored archive checksum does not match')
        # Publish only a complete file, without replacing an existing output.
        with NamedTemporaryFile(dir=output.parent) as temporary:
            temporary.write(archive)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.link(temporary.name, output)
        return {'schema': 1, 'round': round_name, 'team': team, 'commit': commit, 'sha256': digest, 'path': str(output)}


def make_server(port: int, intake: Intake, secret: bytes) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, format, *args) -> None:
            pass

        def respond(self, code: int, status: str, **fields: str) -> None:
            body = json.dumps({'status': status, **fields}).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != WEBHOOK:
                self.respond(404, 'not_found')
                return
            length = self.headers.get_all('Content-Length', [])
            if (
                self.headers.get('Transfer-Encoding') is not None or len(length) != 1
                or not re.fullmatch(r'[0-9]{1,7}', length[0]) or not 0 < int(length[0]) <= MAX_PUSH
            ):
                self.respond(400, 'invalid_length')
                return
            body = self.rfile.read(int(length[0]))
            signatures = self.headers.get_all('X-Hub-Signature-256', [])
            if (
                len(signatures) != 1 or not re.fullmatch(r'sha256=[0-9a-f]{64}', signatures[0])
                or not hmac.compare_digest(signatures[0][7:], hmac.digest(secret, body, 'sha256').hex())
            ):
                self.respond(401, 'invalid_signature')
                return
            if self.headers.get_all('X-GitHub-Event') != ['push']:
                self.respond(200, 'ignored')
                return
            try:
                push = Push.model_validate_json(body)
            except ValidationError:
                self.respond(400, 'invalid_push')
                return
            try:
                code, status = intake.enqueue(push)
            except sqlite3.Error as error:
                LOG.warning('Submission queue unavailable: %s', type(error).__name__)
                self.respond(503, 'unavailable')
                return
            self.respond(code, status, commit=push.after)

    return HTTPServer(('127.0.0.1', port), Handler)


def stop(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='Contest configuration JSON')
    parser.add_argument('--db', type=Path, required=True, help='SQLite ledger on a private persistent volume')
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve', help='Receive signed GitHub push webhooks on loopback')
    serve.add_argument('--port', type=int, default=8782)
    status = commands.add_parser('status', help='Show accepted and frozen commits as JSON')
    status.add_argument('round')
    export = commands.add_parser('export', help='Export one frozen source archive, without extracting it')
    export.add_argument('round')
    export.add_argument('team')
    export.add_argument('output', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = Config.model_validate_json(args.config.read_bytes())
        intake = Intake(args.db, config, GitHub(os.environ.get('GITHUB_READ_TOKEN')))
        if args.command == 'serve':
            secret = os.environ.get('GITHUB_WEBHOOK_SECRET', '')
            if len(secret.encode()) < 32:
                raise SubmissionError('Set GITHUB_WEBHOOK_SECRET to the shared secret (at least 32 bytes)')
            signal.signal(signal.SIGTERM, stop)
            with make_server(args.port, intake, secret.encode()) as server:
                stopping = Event()
                worker = Thread(target=intake.run_worker, args=(stopping,))
                worker.start()
                try:
                    print(f'Listening on http://127.0.0.1:{server.server_port}{WEBHOOK}', file=sys.stderr, flush=True)
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    stopping.set()
                    intake.wake.set()
                    worker.join()
        else:
            result = intake.status(args.round) if args.command == 'status' else intake.export(
                args.round, args.team, args.output,
            )
            print(json.dumps(result, indent=2))
    except (OSError, ValidationError, SubmissionError, sqlite3.Error) as error:
        print(f'Submissions: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
