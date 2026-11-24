# BLACK FRIDAY: the game on one page

*One shop. Four deployments. Thirteen weeks. Survive Black Friday.*

Every team inherits the same online shop. Same product, same customers, same
market. Teams decide how to scale the shop; the organisers decide how to scale
the judging. The winner has the highest profit after the AWS bill, not the most
AWS services.

Full rules: [design.md](design.md). How it is built: [architecture.md](architecture.md).
Runnable steps: [processes](processes/README.md).

## Rules

- **Product:** one $25 "Cloud Mug". Each verified sale contributes $10 before
  technology costs. No pricing, advertising or inventory decisions.
- **Starting shop:** a working PHP/SQLite shop with sessions, carts, orders and
  a synchronous checkout. It is correct under light load and has real limits.
- **Fixed API:** `GET /product`, `POST /cart`, `GET /cart/{id}`,
  `POST /checkout` (with an idempotency key), `GET /orders/{id}`, `GET /health`.
  Any language, framework or storage engine behind it.
- **One frozen release per round.** Teams choose what to scale, cache and how to
  migrate data. Code and architecture stay fixed while trading.
- **An independent referee** checks every checkout: a correctly stored order and
  exactly one payment. HTTP success alone is not a sale.
- **Customers remember.** Lost carts and slow pages cut future sales, even when
  `/health` is green. A bad migration can destroy business already built.
- **Switchers.** Every team starts with the same customers. A customer you fail
  moves next week to the other shops, in proportion to their verified fast
  checkouts. A rival's outage is your Black Friday.

## Four rounds, 13 simulated weeks

| Round | Weeks | The challenge |
|:--|:--|:--|
| 1 Launch | 1–4 | Establish a reliable, low-cost baseline. |
| 2 Growth | 5–8 | More traffic and returning customers; migrate data without losing carts. |
| 3 Black Friday | 9–10 | Abrupt spikes and retries; scale, shed or queue without duplicate payments. |
| 4 Christmas | 11–13 | Sustained demand, repeat customers, history checks and final reconciliation. |

## Scoring

Weekly profit = $10 × verified sales − modelled AWS cost − verified transaction
losses. Score is the sum over weeks 1–13. A cheap, tuned shop can beat an
expensive one. Next-week return chance: 75% after a fast verified checkout, 50%
after a slow one, 20% after a failure, 5% after lost history.

## Two scales

- **Teams scale their shops:** instance size, replicas, storage tier, caches.
- **Organisers scale the judging:** frozen release × scenario, fanned out in
  parallel. Within a week every evaluation is independent, so more workers
  judge faster; with the switchers pool, all teams finish a week before the
  next one starts.

## Five Lambda choices

| Choice | In BLACK FRIDAY |
|:--|:--|
| Web Functions | Serve the arena |
| Durable Functions | Run the round, wait for submissions to close, fan out judging |
| Event Functions | Judge one release against one scenario |
| MicroVMs | Run team shop code separately from the judge |
| Managed Instances | Alternative capacity for the same trusted judges when work is steady (described only, not measured) |

## The Growth round: built and run

One cycle of the game, for an audience: up to eight teams join on their phones
and play one Growth week from the same Launch shop, alongside two house teams.
Players **pick** a reviewed release and an architecture; nobody uploads code.

| Pick | Options | Modelled cost |
|:--|:--|:--|
| Release | **A** migrates every table; **B** migrates customers, products and orders only (faster, drops carts) | A $2, B $1 |
| Storage | instance-local, small shared, large shared | $0, $1, $4 |
| Replicas | 1 or 2 | $1 each |
| Cache | on or off | free |

Team profit, in cents:

```
900                                   Launch: one verified sale ($10) − $1 Launch cost, same for every team
+ architecture profit                 virtual_rds.py: $10 × verified sales − storage − replicas, same customer trace (seed 42)
+ 1000 × Maya's sales                 her checkout now (only if her cart survived), plus one next-week sale
                                      if the shared draw (42) is below her return chance: 75% fast, 50% slow, 5% without her cart
− migration cost                      A 200, B 100
```

Release A is $1 dearer and keeps Maya's $20. Instance-local storage loses its
orders when the shop is replaced, so it sells nothing; a cache or large storage
beats the house teams, which play small storage, one replica, no cache. The
runbook is [AWS round and rehearsal](processes/aws-rehearsal.md).

## What exists today, and what is proposed

Exists and has been run:

- The Growth round (above) in Sydney: a public player page and a keyed
  presenter page on one Web Function, a version-pinned Durable round, one Event
  judgement and one disposable roleless MicroVM per team. Players pick; only
  organiser code runs.
- An AWS rehearsal (Oregon, then Sydney since 23 Nov 2026): Web arena,
  version-pinned Durable workflow, Event evaluator, disposable roleless
  MicroVMs. From one Launch checkpoint, two Growth migrations both passed
  `/health`; only one kept a saved cart.
- Git submission intake: push to `submit/round-1`, signed webhook, exact SHA
  queued, archive pinned and hashed, frozen export.
- One reviewed PHP fixture at a frozen commit ran in a real MicroVM and earned
  82 sales × $25 = $2,050. No audience or outside code has been run.
- Launch-wave fan-out (22 Nov 2026): 32 evaluations, one worker 147.12 s, eight
  workers 22.61 s (6.5×), identical scores. One observation, not a benchmark.
- A local Virtual RDS scoring round and a local virtual AWS simulator.

Proposed, not built: the four-round scored tournament with switchers, a hosted contestant
runner, running untrusted code (needs a separate AWS account, isolated
build/run MicroVMs, externally enforced egress controls and independent
scoring). Managed Instances is **not measured**: the attempted pool
configuration was rejected by Lambda.
