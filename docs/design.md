# BLACK FRIDAY — design specification v0.1

**An AWS cloud architecture competition**

*One shop. Four deployments. Thirteen weeks. Survive Black Friday.*

**Status:** sections 1–15 describe the intended competition, not a deployed
tournament. What is runnable today: the AWS rehearsal, the manual Git-to-MicroVM
fixture proof, and the local Virtual RDS scoring round. See the
[game on one page](game.md) for the short version and the
[process runbooks](processes/README.md) for steps and recorded observations.

## 1. Game overview

BLACK FRIDAY is a competitive cloud engineering game for AWS User Groups,
workshops and software engineering events. Each contestant inherits the same
legacy PHP shop. They deploy it into a restricted AWS-compatible environment,
evolve its architecture, and operate it through the last quarter of the retail
year. Real application code runs in isolated AWS Lambda MicroVMs; the game
models what the contestant’s AWS resources would do and cost.

Everyone starts with the same product, prices and underlying market. The winner
has the highest cumulative net profit, not the most AWS services. A tuned
monolith can beat a distributed architecture. Customers remember what happened,
and a bad migration can destroy some of the business a contestant already built.

## 2. Competition format

| Round | Simulated period | Demand and engineering challenge |
|:--|:--|:--|
| **1 — Launch** | October, weeks 1–4 | Lift and shift or modernize early. Modest concurrent demand exposes inefficient queries and synchronous checkout. Establish a reliable, low-cost baseline. |
| **2 — Growth** | November, weeks 5–8 | More traffic, returning customers and database contention. Improve throughput without losing carts, orders or sessions during migration. |
| **3 — Black Friday** | Weeks 9–10 | Abrupt spikes, sustained peaks, retries and controlled infrastructure degradation. Scale, shed load or queue work without duplicating payments. No emergency releases. |
| **4 — Christmas** | Weeks 11–13 | Sustained holiday demand, repeat customers, order-history checks and final reconciliation. Preserve history and operate economically. |

Each round is **Forecast → Build → Test → Deploy → Trade → Review**. Publish
expected demand, constraints and upcoming events before the build window.
Development and practice benchmarks are repeatable within equal quotas.
At the start of the round a contestant can submit **one immutable production
release**, or retain their current release. Code and declared architecture stay
fixed during trading; declared autoscaling policies and application behaviour
can respond automatically. Review reports weekly profit, customer retention,
performance and failures. Score all 13 weeks, carrying business and application
state forward through every round.

## 3. Starting shop

- **Product:** the Cloud Mug, $25 selling price, $10 contribution per successful
  sale before technology costs, unlimited inventory. No advertising, pricing,
  product selection or supply-chain decisions.
- **Application:** working Apache/PHP, SQLite, server-side sessions, a product
  page, shopping cart, customer and order records, and synchronous checkout.
  Correct under light load, with genuine scalability limits. The published
  starter already implements the Shop API and simulated payment contract, so
  one-click **Lift & Shift** requires no source changes.
- **Business:** identical customer records, saved carts and existing orders.
  Returning customers appear in Launch; preserving history matters from day one.

## 4. Fixed Shop API

| Endpoint | Purpose |
|:--|:--|
| `GET /product` | Retrieve the fixed product |
| `POST /cart` | Create or update a customer’s cart |
| `GET /cart/{id}` | Retrieve a saved cart |
| `POST /checkout` | Complete a purchase with an idempotency key |
| `GET /orders/{id}` | Retrieve an existing order |
| `GET /health` | Report readiness |

Publish schemas and the rules for stable customer identities, request IDs, order
IDs and checkout retries. Contestants can change any language, framework or
storage engine behind this contract. HTTP success alone is not a sale: checkout
needs a correctly persisted order and exactly one effective payment.

## 5. Real execution and simulated AWS

**Execution:** each virtual EC2 instance maps to a real Lambda MicroVM with
independently enforced CPU, memory and execution limits. Actual HTTP requests
and application code do real work. Scored Virtual RDS operations perform real
state changes against a modeled store, not an AWS or PostgreSQL server. Report
modeled storage capacity and delay separately from observed application
performance; do not claim that either reproduces real EC2 or RDS throughput.

**Contestant cloud:** route supported AWS CLI and SDK operations to simulator
endpoints with virtual credentials. Contestants receive no real AWS
account-management permissions. The intentionally small catalogue:

| Virtual resource | Game capability |
|:--|:--|
| EC2 | Instance size, replica count and application deployment |
| Application Load Balancer | Routing, target groups and health checks |
| Virtual RDS | Shared records, conditional commits, modeled capacity and failures |
| SQS | Asynchronous work, batching, visibility timeouts and retries |
| Lambda Web Functions | Alternative HTTP application architecture |
| Lambda Event Functions | Event processing and background workers |
| Lambda Durable Functions | Long-running, recoverable workflows |
| Lambda Managed Instances | An alternate capacity model for sustained Event Function work |
| CloudWatch | Metrics, logs, alarms and traces |
| S3 | Deployment artifacts and persistent objects |

Accepted workloads execute real code. Resource behaviour, scaling rules and
billing are explicitly modeled where they are not the actual AWS service.

The [local simulator](processes/local-aws-simulator.md) exercises the
EC2, ALB and RDS control surface with the trusted shop. It does not implement
the MicroVM isolation, quotas or billing described for the competition.

## 6. Architectural freedom and limits

Contestants can change source code and language, release and migration scripts,
instance sizes and replica counts, routing, record layout and access patterns,
session persistence, caching, queues and workers, Lambda configuration,
concurrency, batching, failure handling, and allowed capacity allocations.
Everyone has the same resource ceilings and catalogue. Larger or additional
instances, databases, queues and reserved compute have costs. Where supported, compare a constrained
Managed Instances capacity pool against ordinary on-demand event execution.
There are no points for adopting a service.

## 7. Customer behaviour

Synthetic customers have stable identities and a limited schedule of purchase
opportunities. They browse, save or retrieve carts, attempt purchases and come
back to inspect orders. Each experience changes the chance of a return next
week:

| Outcome | Next-week return probability |
|:--|--:|
| Verified fast checkout, at most 2 s | 75% |
| Verified slow checkout, over 2 s and at most 5 s | 50% |
| Failed checkout or customer timeout | 20% |
| Lost history or serious integrity failure | 5% |

These are game parameters, not consumer research. A frustrated customer can
return. Combine fixed new arrivals with retained customers. Every team starts
from an identical copy of the same customers and receives the same new arrivals
each week. Share scenario seeds, purchase opportunities and per-customer,
per-week retention draws across contestants, so one team's outcomes do not
perturb another team's random stream. The **realised** traffic can diverge:
better retention earns future sales opportunities and creates additional load.

### The switchers pool (proposed)

A customer who does not return after a failed checkout, a timeout or lost
history does not simply disappear: they become a **switcher**. A customer who
does not return after a successful checkout has no grievance and leaves the
game.

At the end of each week, the referee pools every team's switchers and hands
them out for the next week:

- **Share:** each team receives switchers in proportion to its verified fast
  checkouts that week. A team with none receives none.
- **Never back to the shop they left:** a switcher is only offered to other teams.
- **Deterministic:** switchers are ordered by stable customer ID and divided by
  largest remainder, with ties broken by the week's published seed, so the same
  week's results always produce the same allocation.
- **New to the receiving shop:** a switcher arrives with no cart or order
  history there, then follows the same return rules as any other customer.

So one team's outage becomes another team's extra traffic, often during the
same spike that caused it. The cost is independence: a team's official score
now depends on the other teams in the field. Each team's own starting customers
and new arrivals stay identical, so the base remains fair; only the switchers
differ. A replay of one team re-runs that team against the switchers it actually
received, from the saved allocation, and never changes the official scores.

Each simulated week becomes a synchronisation point: every team's week must be
judged before switchers can be allocated for the next one (see
[architecture](architecture.md#a-round)). None of this is built: the referee,
Virtual RDS and the judges hold no cross-team state today.

## 8. Business Referee

A trusted service owns customer identities and purchase intentions, simulated
payment outcomes, verified sales, shopping experiences, history references,
retention decisions and financial results. Persist its state in a transactional
store with immutable event history independent of sandbox lifetimes. The
contestant owns their own data, sessions, orders and queues; the referee does not
repair them. If a migration loses history, the referee still knows what a later
customer should be able to retrieve.

Expose a simulated payment endpoint that records charges independently of app
responses. Authorize payment against the issued purchase intention and enforce
idempotency for that intention, including after a retry or worker restart.
Credit it at most once, only when a matching persisted order and a successful
payment are verified. Exercise the case where payment succeeds, the response is
lost and checkout is retried. Check persistence independently of the initial
response, including reads after an environment restart.

## 9. Deployments, migrations and rollback

A production release contains an application artifact, infrastructure declaration
and any migration procedures. Preserve the previous state, provision the new
architecture, run the migration, check readiness, switch traffic, and record
downtime and correctness before and during scored trading.

Players own the migration: instance-local to shared storage, one server to
replicas, synchronous to asynchronous checkout, a language rewrite, or new
Event workers. Passing health checks does not prove carts or orders survived.
Loss of history, downtime and failed transactions affect business outcomes; no
player can reset the business for another attempt.

Before cutover, rollback can restore the pre-deployment snapshot. After the new
release takes customer traffic, restore the previous compatible release only if
post-cutover transactions are preserved or reconciled. A rollback consumes
deployment time, is not a second release, and never erases the referee ledger.

## 10. Scoring and clocks

**Weekly profit = $10 × eligible verified sales − modeled infrastructure cost
− independently verified transaction losses not already accounted for.**
**Quarterly profit is the sum over weeks 1–13.**

Credit no sale twice. Incorrect orders, duplicate payments and unverified
transactions earn no revenue. Refunds or incident costs cannot be double-counted
as both excluded contribution and a separate loss. Downtime, abandonment and
poor retention primarily reduce current or future sales, not arbitrary points.

Compress the shopping calendar and the gaps between load waves. Measure HTTP
latency on the wall clock; enforce modeled storage deadlines independently.
Charge modeled resources over simulated business time using a published rate card.
Keep measured timings, modeled delays, and modeled charges separate.

Report verified sales, conversion, p50/p95/p99 latency, errors and timeouts,
retention, migration integrity, CPU and memory utilization, queue depth and
processing delay where applicable, infrastructure spend, and cost per verified
order. The public leaderboard ranks by cumulative quarterly profit.

## 11. Official workload and fairness

Run real HTTP traffic against frozen releases in isolated environments with
equivalent limits. Use the same scenario definitions, comparable starting
populations and reproducible random inputs; hide official variations and
referee internals from contestants. Practice traces are public; official traces
are revealed after scoring. The referee checks externally observable outcomes
and independently verifies order and payment effects. Calibrate timing and
repeat equivalent runs where host noise is significant. Any comparison or repeat
starts from the same checkpoint on a separate evaluation fork; it never rewrites
official business history. With the switchers pool, the switchers a team
receives are the one input that differs between teams; a repeat uses the saved
allocation for that week, never a fresh one.

## 12. Player experience

Provide a browser workspace with source, terminal, real language runtimes,
virtual AWS CLI and SDK configuration, architecture editor, deploy and migrate
controls, database inspection, logs, metrics, traces, practice runs and release
history. Any development tools are allowed within the workspace; AI is optional.

- **Beginner:** lift and shift, adjust compute sizes, tune the app and use guided
  architecture recommendations.
- **Intermediate:** migrate to PostgreSQL, add replicas and load balancing,
  share sessions, and introduce asynchronous workers where available.
- **Advanced:** rewrite the app, automate migrations and deployment, and tune
  distributed workloads. The same public contract and scoring apply at every level.

## 13. Arena, presentation and visual references

Show the current round and traffic wave, contestant architecture, verified
sales, latency, errors, utilization, virtual spend, weekly and cumulative profit,
retention, and active incidents. A presenter can reveal traffic spikes and
incidents, inspect deployment changes, and compare migration strategies under
equivalent conditions. Postmortems connect measured bottlenecks and changes in
business outcomes; replays compare releases without changing official scores.

| Asset | Intended use |
|:--|:--|
| [Cloud Surge](../assets/cloud-surge.png) | Key art for the opening slide |
| [Four rounds](../assets/four-rounds.png) | Quarter and demand-wave illustration; the depicted architectures are examples, not requirements |
| [Architecture Showdown](../assets/architecture-showdown.png) | Side-by-side visual for the contest |
| [Arena dashboard](../assets/arena-dashboard.png) | Spectator layout reference, not benchmark data |
| [UI asset kit](../assets/ui-asset-kit.png) | Reference sheet for logos, colours, controls, statuses, metrics and diagrams |

The artwork is illustrative. Its sample scores, prices and service choices do
not override the rules or represent measured results.

## 14. Real execution lifecycle

Provision contestant infrastructure only for development, practice and official
evaluation. For each scored round, restore saved application data and the release
manifest; start the required MicroVMs and managed-service implementations; deploy
and migrate; measure readiness and resource baselines; generate traffic; record
real measurements and verified outcomes; persist application state, applicable
queue state and referee results; then terminate the environments. The next round
starts from durable data, not from a previous MicroVM’s memory. No VM needs to
stay alive for the full simulated quarter or while players prepare releases.

## 15. First playable version

Deliver all four scored rounds, one fixed product and price, a PHP starter shop,
virtual EC2 backed by real MicroVMs, a virtual ALB and Virtual RDS, checkpointed
application data and migrations, the four customer behaviours, deterministic
load generation, an independent Business Referee, modeled infrastructure billing,
a profit leaderboard, and basic logs
and performance diagnostics. Contestant-facing SQS and virtual Lambda execution
modes can come later without changing the competition rules.

Acceptance:

1. The unchanged starter deploys and earns verified sales.
2. Scored requests execute real contestant code in isolated MicroVMs.
3. Increasing load exposes a genuine application or resource limit.
4. Changing infrastructure or code measurably affects throughput, correctness
   or cost.
5. A successful database migration preserves customers, carts and orders.
6. A faulty migration causes observable customer and revenue consequences even
   when `/health` still passes.
7. Identical releases on equivalent inputs, including the same inbound
   switchers, produce comparable results.
8. All four rounds complete with a verified 13-week profit score.
9. The postmortem links changes in score to observable architectural outcomes.


## 16. The five Lambda choices on the tournament side

The rehearsal deploys Web, Event and Durable Functions plus MicroVMs. Managed
Instances is described, not deployed or measured. None is a required contestant
architecture. AWS describes [three compute primitives](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html):
Event Functions, Web Functions and MicroVMs. Durable execution and Managed
Instances answer separate questions about Event Functions.

| Choice | Tournament role | Trade-off to explain |
|:--|:--|:--|
| [Event Functions](https://docs.aws.amazon.com/lambda/latest/dg/lambda-functions-chapter.html) | Dispatch independent shop evaluations and process bounded results | Scale out on demand; make retries safe rather than relying on the process that ran last time |
| [Web Functions](https://docs.aws.amazon.com/lambda/latest/dg/web-functions.html) | Serve the arena and spectator API with concurrent requests | A normal HTTP server is useful here; it is not the authoritative state store, and memory still costs |
| [Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html) | Checkpoint tournament progress and wait for the next release window | No worker runs overnight just to wait; replay requires retry-safe side effects |
| [MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html) | Isolate actual contestant application and migration code | VM boundaries do not replace restricted networking, external scoring, lifecycle limits or persistent app data |
| [Managed Instances](https://docs.aws.amazon.com/lambda/latest/dg/lambda-managed-instances.html) | Alternate capacity for the same sustained trusted Event workload | Would need throughput, queue delay and cost per verified run, including idle capacity. If on-demand wins, keep it. Not measured |

## 17. Design principle

Correctness before throughput; persisted state is both an asset and a migration
liability; scaling moves bottlenecks; complexity costs money. Win by operating
the most profitable reliable shop, not by selecting a preferred AWS architecture.
