# Launch-wave rehearsal

Rehearse a game-release wave on Lambda: fan one set of shop evaluations out to
Event Lambda workers and compare one worker with many.

The release shape is inspired by the
[GTA Online launch](https://www.rockstargames.com/newswire/article/k49a58878okoak/some-more-details-on-grand-theft-auto-online.html):
preload activity, a concentrated unlock rush, and recovery. Request volumes,
timing and failures are synthetic. No requests go to Rockstar, and this does
not model the game download or CDN traffic. (If you hear "as we scale this MMOG
to the size of GTA 6", that is a joke. Nothing here is GTA-scale.)

## Run it

From `demo/`, with `BLACK_FRIDAY_AWS_ACCOUNT` and `BLACK_FRIDAY_AWS_PROFILE` set
and a valid SSO session (see [AWS prerequisites](README.md#aws-prerequisites)):

```sh
cd demo
uv run aws/launch_wave_proof.py proof --cohorts 8 --workers 8 --compare-serial
```

`--cohorts` and `--workers` each accept 1–8 (default 8). `--compare-serial` runs
the exact same jobs first with a one-worker limit. For a smaller stage run, use
`--cohorts 1 --workers 4` without the serial pass. `uv run aws/launch_wave_proof.py
package <output.zip>` writes the Lambda ZIP without contacting AWS.

Each cohort uses the same seed across four plans: small, extra shop replica,
large, and cached. A version-pinned Durable Function fans out one Event Lambda
per plan and cohort. Each worker runs the trusted shop over loopback HTTP with
its own in-memory Virtual RDS and payment referee. The operator checks that the
completed jobs, phase counts and scores match across the serial and parallel
executions.

Each evaluation issues 1,948 HTTP requests, including verification reads: 288
during preload, 1,441 at unlock, and 219 during recovery. It checks a commit
whose reply was lost, replaces a shop, then verifies that replaying checkout
twice recovers the same order and records just one payment.

The JSON report contains each Event Lambda request ID, start and end time, real
loopback HTTP count, and per-phase game results. Use Durable execution wall time
and measured worker overlap for **platform scale**; use modelled storage units,
rejections and game profit for **shop design**. Actual requests do work, but the
one-second storage windows are modelled, not measurements of public traffic, RDS
throughput or any real game launch.

Redirect stdout to `report.json` to keep the evidence; `jq '.scaled | del(.jobs)'
report.json` shows a compact score and concurrency summary.

## Limits and cleanup

The operator creates a private, temporary stack in Sydney; it does not update
the rehearsal. Limits: 1–8 cohorts, up to eight workers, 30 seconds per worker,
and a five-minute Durable execution ceiling. There is no public URL, database
instance, or contestant code. The operator deletes the execution history, stack
and exact temporary package version on exit.

## Recorded observation: Oregon, 22 Nov 2026

Serial first, parallel second, using `release-v1`, eight cohorts (seeds 42–49)
and four plans. Both passes completed 32 evaluations and 62,336 loopback HTTP
requests with identical game results. Durable execution wall time was
**147.12 s** at one observed worker and **22.61 s** at eight (**6.5×** faster).
These times exclude stack creation and teardown. This is one observation, not a
cold-start-controlled benchmark or a performance guarantee.

Verified sales per design, summed over the eight seeds (identical in both
passes): 1,558 each for the small and extra-replica designs, 2,216 each for the
large and cached designs. The extra replica did not help because the storage
tier was the bottleneck.

## On-demand vs Managed Instances

Managed Instances would run the same trusted Event scoring workers on a
dedicated EC2 pool in your account. On-demand charges for requests and execution
time; Managed also charges for capacity while it is idle. A fair comparison needs
throughput, p95 admission-to-worker-start delay, and cost per verified
evaluation including idle capacity. A short burst does not establish the
break-even point for sustained judging, and steady work may favour Managed while
bursty work favours on-demand. **This is not measured.** The one attempt to
create an ARM64 capacity-provider pool was rejected by Lambda, so no Managed
evaluations or cost comparison exist. The only on-demand figure is from a later
Oregon repeat of 96 evaluations (three runs of the same 32 jobs): 20.6–24.3 s of
execution wall time each, and an estimated worker charge of **US$0.0029**,
excluding Durable orchestration, log data and taxes. Describe Managed Instances
on stage; do not run it.
