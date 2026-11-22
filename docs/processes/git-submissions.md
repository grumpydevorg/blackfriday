# Git submissions

A team submits a release by pushing to a submission branch. This runbook covers
the team's push and the organiser's intake receiver. The intake never extracts,
builds, executes or scores a submission.

## Submit (team)

In the team's registered GitHub repository:

```sh
git push origin HEAD:refs/heads/submit/round-1
```

A normal push to `main` is not a submission. A new fast-forward push is
eligible if the receiver durably queues it before cutoff. Configure GitHub
branch rules to block deletion and force pushes on `submit/round-*`; intake also
rejects these events. Duplicate events do not enqueue another job. A delayed
push cannot move the accepted commit backward.

## Organiser setup

Run the [submission intake](../../demo/submissions.py) on a dedicated host
without AWS or presenter credentials. Put `contest.json` and the SQLite ledger
on a private persistent volume. The config maps team names to numeric GitHub
repository IDs and round names to timezone-aware UTC cutoffs; all teams and
rounds are fixed for that ledger. Example:

```json
{"teams":{"demo":1296269},"rounds":{"round-1":"2026-11-25T08:00:00Z"}}
```

Use your own fixture repository ID and cutoff. Supply `GITHUB_WEBHOOK_SECRET`
(at least 32 bytes) and, for private repos, `GITHUB_READ_TOKEN` with read-only
Contents access to the registered repos. Load them via SOPS (or your secret
manager), not CLI arguments. Run one receiver per ledger from `demo/`:

```sh
cd demo
sops exec-env ./submissions.sops.env \
  'uv run submissions.py --config /srv/black-friday/contest.json --db /srv/black-friday/intake.sqlite serve'
```

`serve` listens on loopback port 8782 (`--port` to change). Expose **only**
`POST /github/push` via a rate-limited HTTPS reverse proxy to
`127.0.0.1:8782`; buffer the raw request and cap it at 2 MiB. Register that URL
as a JSON GitHub webhook for push events. The receiver verifies the signature
and queues the exact SHA in SQLite before sending `202`. A separate worker
fetches and stores the pinned archive (at most 8 MiB compressed). Eligibility is
fixed at receipt, so a fetch that finishes after cutoff does not disqualify a
queued push. If a delivery never reached the queue, redeliver it before cutoff.
If a queued archive is `unavailable`, redeliver the same event even after
cutoff; that cannot introduce a new SHA.

## Status and export

```sh
uv run submissions.py --config /srv/black-friday/contest.json --db /srv/black-friday/intake.sqlite status round-1
uv run submissions.py --config /srv/black-friday/contest.json --db /srv/black-friday/intake.sqlite \
  export round-1 demo /srv/black-friday/round-1-demo.tar.gz > /srv/black-friday/round-1-demo.json
```

`status` shows the accepted SHA, archive digest, latest distinct push, and
unresolved pushes. After cutoff, the round is `settling` while work is queued or
`blocked` if a fetch needs recovery. `export` refuses an unsettled team rather
than silently returning its older commit. For a settled team, it delivers the
frozen bytes and digest without overwriting an existing file.

On stage, show **push → queued → accepted SHA → frozen archive** with a
disposable fixture repository.

## Observed

A real test of this flow was run with a disposable GitHub repository. No team
or audience repository has been used.

The frozen export feeds [the MicroVM fixture run](microvm-submission.md).
