# BLACK FRIDAY: the game on one page

*One shop. Four deployments. Thirteen weeks. Survive Black Friday.*

Every team inherits the same online shop. Same product, same customers, same
market. Teams decide how to scale the shop; the organisers decide how to scale
the judging. The winner has the highest profit after the AWS bill, not the most
AWS services.

Full rules: [design.md](design.md). Runnable steps: [processes](processes/README.md).

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
  parallel. Every evaluation is independent, so more workers judge faster.

## Five Lambda choices

| Choice | In BLACK FRIDAY |
|:--|:--|
| Web Functions | Serve the arena |
| Durable Functions | Run the round, wait for submissions to close, fan out judging |
| Event Functions | Judge one release against one scenario |
| MicroVMs | Run team shop code separately from the judge |
| Managed Instances | Alternative capacity for the same trusted judges when work is steady (described only, not measured) |

## What exists today, and what is proposed

Exists and has been run:

- An Oregon (`us-west-2`) rehearsal: Web arena, version-pinned Durable workflow,
  Event evaluator, disposable roleless MicroVMs. From one Launch checkpoint, two
  Growth migrations both passed `/health`; only one kept a saved cart.
- Git submission intake: push to `submit/round-1`, signed webhook, exact SHA
  queued, archive pinned and hashed, frozen export.
- One reviewed PHP fixture at a frozen commit ran in a real MicroVM and earned
  82 sales × $25 = $2,050. No audience or outside code has been run.
- Launch-wave fan-out (22 Nov 2026): 32 evaluations, one worker 147.12 s, eight
  workers 22.61 s (6.5×), identical scores. One observation, not a benchmark.
- A local Virtual RDS scoring round and a local virtual AWS simulator.

Proposed, not built: the four-round scored tournament, a hosted contestant
runner, running untrusted code (needs a separate AWS account, isolated
build/run MicroVMs, externally enforced egress controls and independent
scoring). Managed Instances is **not measured**: the attempted pool
configuration was rejected by Lambda.
