# BLACK FRIDAY architecture

BLACK FRIDAY has two sides:

- **The team's side** is a small, constrained cloud. A team declares instances,
  a database, a cache and a load balancer, then scales them within the rules.
- **Our side** is real AWS Lambda. It turns each declared instance into a tiny
  disposable MicroVM, judges the shop, and scales the judging to every team.

The teams scale the shop. We scale the judging.

Each section says what is **built and run** and what is **proposed**. Commands
for the built parts are in [`processes/`](processes/README.md); the game rules
are in [`game.md`](game.md).

```mermaid
flowchart TB
  subgraph T["The team's side: a small, constrained cloud (proposed)"]
    TPL["infra/template.yaml<br/>EC2 · Auto Scaling · RDS · ElastiCache · ALB"]
    APP["app/ (their shop)"]
  end
  subgraph O["Our side: real Lambda"]
    WEB["Web Function<br/>arena · registration · leaderboard"]
    DUR["Durable Function<br/>rounds · cutoffs · fan-out"]
    EVT["Event Functions<br/>one judge per team × week"]
    VM["MicroVMs<br/>each 'EC2 instance' = one tiny VM"]
    VRDS["Virtual RDS and cache<br/>shared state outside the VM"]
  end
  TPL -->|git push| WEB --> DUR --> EVT
  EVT -->|"run-instances → run_microvm"| VM
  APP -.runs inside.-> VM
  VM <-->|"named database calls"| VRDS
  EVT -->|"referee: orders and payments"| WEB
```

## Scalable but constrained

| | Scalable | Constrained, and who enforces it |
|:--|:--|:--|
| **Team side** | More instances, Auto Scaling, a bigger database, a cache | Allowed resource types only (template checker). Fixed instance sizes. Every resource costs game money (judge's price list). |
| **Each tiny VM** | Boots in seconds; one VM per instance | Fixed memory and CPU. Destroyed after 120 s. No AWS role. Inbound only from the judge with a short-lived token. Outbound internet must be off before running outside code. |
| **Our side** | Durable fans Event judges out in parallel | Caps on concurrent judges, attempts and round length. A separate AWS account for team code. |

The constraints make the game fair (everyone gets the same tiny cloud), safe
(strangers' code is walled in) and cheap (we pay only while a VM runs).

## Our side today: built and run

Everything runs in Sydney (`ap-southeast-2`) on Lambda, S3, DynamoDB and SSM; it
was built and measured in Oregon (`us-west-2`) first. There is no real EC2, RDS,
load balancer, VPC or NAT gateway.

Two deployments with the same shape and no shared resources (selected by
`BLACK_FRIDAY_DEPLOYMENT`): the **round**, a public game for an audience, and the
**rehearsal**, its frozen IAM-only predecessor kept as a fallback (tag
`rehearsal-fallback-2026-11-24b`). The round:

```mermaid
flowchart LR
  Ph["Players' phones<br/>(join, pick, scoreboard)"] -->|"public HTTPS"| W
  P["Presenter<br/>/host: passphrase sign-in on any device<br/>(or operate.py present, adds the key)"] -->|"HTTPS + cookie or key"| W
  W["Web Function<br/>black-friday-round<br/>(created by operate.py)"]
  K[("SSM<br/>presenter key + passphrase")] -.- W
  subgraph R["Stack BlackFridayRound"]
    D["Durable Function<br/>black-friday-round-workflow"]
    E["Event Function<br/>black-friday-round-evaluator"]
    M["MicroVM image<br/>black-friday-round-shop"]
    T[("DynamoDB<br/>round state and teams")]
  end
  subgraph A["Stack BlackFridayRoundArtifacts"]
    S3[("S3 bucket<br/>packages · checkpoints · receipts")]
  end
  W --> T
  W -->|"start / continue (presenter only)"| D
  D -->|"Launch, then one job per team"| E
  E -->|"run_microvm, 120 s"| V["MicroVM per team<br/>shop + database"]
  M -.image.- V
  E <--> S3
  V -->|"one receipt upload"| S3
```

| Resource | Kind | Job | Limits |
|:--|:--|:--|:--|
| `black-friday-round` ([`operate.py`](../demo/aws/operate.py), [`web.mjs`](../demo/aws/web.mjs), roster rules in [`roster.mjs`](../demo/aws/roster.mjs)) | Web Function, Node.js 24 | Player page, presenter page, API | Public endpoint (`ApplicationManaged`); presenter routes need a sign-in cookie or the SSM key; 10 failed sign-ins per 10 minutes; 2 KiB bodies; at most 10 environments, 100 requests/s, 15 s timeout; request logs kept 7 days; HSTS; a 270 KB hero image cached for an hour |
| `black-friday-round-workflow` ([`workflow.py`](../demo/aws/workflow.py)) | Durable Function | Launch → wait for Close → one checkpointed step draws the bots and every team's roll → one migration per option in use → wait → one shopper check per option in use → scoreboard | 48 h execution, 23 h callbacks, 7-day history; at most 3 migration and 3 check MicroVMs for any number of teams |
| `black-friday-round-evaluator` ([`evaluator.py`](../demo/aws/evaluator.py)) | Event Function | Starts MicroVMs, drives the shop, checks orders and payments, scores the architecture, saves results | 512 MB, 300 s, at most 10 at once |
| `black-friday-round-shop` | MicroVM image | PHP shop with SQLite or PostgreSQL 17 | ARM64, 1 GiB; each VM at most 120 s, no role |
| Round table | DynamoDB, on demand | Round state, teams and picks | A safety cap of 200 audience teams + 2 house teams; about 160 KB at the cap, against a 400 KB item limit |
| Artifacts ([`artifacts.yaml`](../demo/aws/artifacts.yaml)) | S3, versioned | Code packages, checkpoints between stages, payment receipts | Objects expire after 7 days; old versions after 1 day |

The rehearsal is the same shape with names `BlackFridayRehearsal`,
`black-friday-arena`, `black-friday-evaluator` (at most 4 at once) and an
IAM-only endpoint that `present` signs with SigV4.

The launch-wave proof ([`launch-wave.yaml`](../demo/aws/launch-wave.yaml))
creates a temporary stack with its own Durable Function and Event judges (512 MB,
at most 8 at once), runs, and deletes it. Those judges run the trusted shop and
referee inside the function, with no MicroVM. That stack produced the measured
147 s → 22.6 s result ([`processes/launch-wave.md`](processes/launch-wave.md)).

Managed Instances is not deployed and was not measured.

## The tiny VM: built and run

There are two images.

**Rehearsal image** ([`Microvm.Dockerfile`](../demo/aws/Microvm.Dockerfile)):
Debian trixie with PHP 8.4, PostgreSQL 17 and Python 3, running as `postgres`.

```mermaid
flowchart LR
  J[Event judge] -->|"port 8080 only, 3-minute token"| G[vm_runtime.py gateway :8080]
  G -->|"allow-listed shop routes"| P["php -S 127.0.0.1:8081 shop.php"]
  P --> D[(SQLite or PostgreSQL 17 in /state)]
  P -->|charge| Pay["payment relay 127.0.0.1:8082"]
  Pay -->|"one-use pre-signed S3 PUT"| S3[(S3 receipt)]
```

- The gateway ([`vm_runtime.py`](../demo/aws/vm_runtime.py)) serves `/ready`,
  a single-use `/control/setup` (launch, migrate or compare; keep or drop data),
  `/control/artifact` (SQLite file or PostgreSQL dump), and a fixed list of shop
  routes. Everything else is 404.
- The shop holds no payment credential. The relay accepts only the checkout the
  judge expects and writes one receipt through a link that expires with the VM.
- Each stage gets a fresh VM. Only the exported artifact carries data forward.

**Submission image** ([`Submission.Dockerfile`](../demo/aws/Submission.Dockerfile)):
PHP 8.4 CLI as `nobody`, serving the reviewed fixture's `/ready` and
`/quote?quantity=N`. A frozen Git commit of that fixture ran in a real Oregon
MicroVM and earned 82 sales × $25 = $2,050.

**Outbound internet is on** in both images (`INTERNET_EGRESS`). That is
acceptable for reviewed code and must change before running anyone else's; see
[`processes/running-outside-code.md`](processes/running-outside-code.md).

### Why a MicroVM for the team's shop

- A separate kernel per shop, so hostile code stays in its own VM.
- No execution role, so no AWS credentials; the judge alone can reach port 8080.
- Any language and any OS packages, including the team's own database process.
- A fresh VM per stage: nothing survives to influence the next round or another team.
- The same size and a hard time limit for everyone.
- Real execution, so a slow query is slow and a crash loses the sale.
- Billed only while running, unlike an EC2 instance per team.

## Fake AWS for teams: built and run

Two fake EC2 APIs accept the stock `aws` CLI on a loopback endpoint. Neither
creates a real EC2 instance.

| | Local simulator ([`simulator.py`](../demo/simulator.py)) | Pseudo-EC2 bridge ([`pseudo_ec2.py`](../demo/aws/pseudo_ec2.py)) |
|:--|:--|:--|
| An "instance" is | A PHP shop process in one local container | A real Lambda MicroVM |
| AWS account | Not needed | Needed |
| Operations | 12 (below) | `describe-images`, `run-instances` (one `t4g.micro`), `describe-instances`, `terminate-instances` |
| Use | Practising infrastructure changes | Showing `run-instances` boot an isolated VM from one reviewed commit |

Local simulator operations:

| Service | Operations | Limits |
|:--|:--|:--|
| EC2 | `run-instances`, `describe-instances`, `terminate-instances` | `ami-00000000000000001`, `t4g.micro`, at most 4 running; client-token idempotency; the `BlackFridayDatabase` tag attaches a database |
| RDS | `create-db-instance`, `describe-db-instances`, `delete-db-instance` | `db.t4g.micro` PostgreSQL, at most 4, on one local PostgreSQL server; delete needs `--skip-final-snapshot` and is refused while in use |
| Load balancer | `create-load-balancer`, `create-target-group`, `create-listener`, `register-targets`, `deregister-targets`, `describe-target-health` | One load balancer on `127.0.0.1:8781`; checks `/health` and alternates healthy targets |

An instance without the database tag keeps its data in its own SQLite file, which
is lost on termination. Commands: [`processes/local-aws-simulator.md`](processes/local-aws-simulator.md).

## Virtual RDS: built and run

[`virtual_rds.py`](../demo/virtual_rds.py) is an in-memory store that makes the
database a deliberate, repeatable bottleneck:

- A read costs 1 unit and a commit 2. Small handles 80 units/s, large 160. A
  request that would wait more than 1 s is rejected with HTTP 429.
- `conditional_commit` checks and writes in one step, so orders are exactly once.
- A fault can drop the reply to a successful commit, testing whether a retry
  charges twice. The referee counts payments separately.
- Shared storage survives shop replacement; the `local` tier dies with the shop.
- The same seed gives the same result anywhere, so the leaderboard is fair.
- No server to start, so one Event judge scores a design in about 5 s.

It models capacity and timing; it does not measure a real database. Only the
trusted Python test shop uses it today.

## The arena

**Today (built and run):** one Web Function ([`web.mjs`](../demo/aws/web.mjs),
rules in [`game.mjs`](../demo/aws/game.mjs)) serves two pages.

- **Player page** ([`player.html`](../demo/aws/player.html), public): join a
  team, tap one of three options ([`options.json`](../demo/aws/options.json):
  Lift & shift 5%, Scale up 10% and +$40, Refactor 30%), watch the round, see the
  scoreboard: the top 10 and the viewer's own row, with "+N more teams". A team
  token kept on the phone guards its pick. Other teams' picks are hidden until
  the round closes. `/api/state` stays small: before Close it carries only each
  team's id, name, house flag and picked flag, and after the shoppers it carries
  the board instead of the roster (about 16 KB raw, 2.6 KB gzipped, for 200 teams).
- **Presenter page** ([`host.html`](../demo/aws/host.html)): the page is public
  but shows only a passphrase form until sign-in
  ([`host_auth.mjs`](../demo/aws/host_auth.mjs)). The passphrase is Rick's own
  (SSM SecureString, set with `operate.py passphrase`); the session cookie is an
  HMAC keyed by the SSM presenter key over the current passphrase, so changing
  either signs every device out. After sign-in: QR and public URL, team list,
  execution ARN, three buttons, Open/Launch → Close and deploy every release →
  Send the shoppers back, each resuming the paused Durable round. Each action is
  bound to the current run, so a double-click cannot advance it twice. Only
  these actions start compute. `operate.py present` (presenter key header)
  remains as a second way in.

Routes and rules: [AWS round and rehearsal](processes/aws-rehearsal.md#what-runs).
The frozen rehearsal serves the older single presenter page,
[`demo/index.html`](../demo/index.html), which also runs locally from
[`arena.py`](../demo/arena.py).

**Proposed:** Git-registered teams, submissions and a multi-round leaderboard.

```mermaid
sequenceDiagram
  participant T as Team (GitHub)
  participant G as GitHub App
  participant W as Web Function (arena)
  participant D as Durable (round)
  participant E as Event judges
  T->>G: install app on repo
  G->>W: installation webhook → team registered
  T->>G: git push origin HEAD:submit/round-1
  G->>W: signed push webhook → exact SHA queued
  W-->>T: GitHub check: plan and "received"
  D->>D: wait for cutoff, freeze one SHA per team
  D->>E: fan out teams, weeks in order per team
  E-->>D: verified sales, cost, profit
  D->>W: publish round results
  W-->>T: leaderboard and GitHub check "scored"
```

1. **Register:** create a repo from a starter template and install the BLACK
   FRIDAY GitHub App on it. The installation gives the repository's numeric ID
   and the installing user. An organiser approves team names before they appear.
2. **Practise:** a push to any other branch gets a one-scenario score as a GitHub
   check, rate limited per team.
3. **Submit:** the last push to `submit/round-N` received before the cutoff
   counts. [`submissions.py`](../demo/submissions.py) already implements
   signed webhooks, exact SHAs, receipt-time cutoff and frozen export; it would
   take teams from app installations instead of a config file.
4. **Leaderboard:** public, read-only: profit per round and in total, verified
   sales, lost carts, duplicate charges, and each team's drawn architecture.

A Web Function endpoint can be public (`ApplicationManaged`, used by the round),
so the webhook and leaderboard need no separate function.

## What teams submit: proposed

```
my-shop/
  app/                 # their shop: listens on :8080, implements the Shop API
  infra/template.yaml  # the architecture, in a CloudFormation subset
```

```yaml
Resources:
  Db:
    Type: AWS::RDS::DBInstance
    Properties: {Engine: postgres, DBInstanceClass: db.t4g.large, AllocatedStorage: 20}
  Cache:
    Type: AWS::ElastiCache::CacheCluster
    Properties: {Engine: redis, CacheNodeType: cache.t4g.micro, NumCacheNodes: 1}
  ShopA:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: t4g.small
      ImageId: blackfriday-app
      Tags: [{Key: DATABASE_URL, Value: !GetAtt Db.Endpoint.Address}]
  Targets:
    Type: AWS::ElasticLoadBalancingV2::TargetGroup
    Properties: {Port: 8080, HealthCheckPath: /health, Targets: [{Id: !Ref ShopA}]}
```

| Template resource | What we run | Game cost |
|:--|:--|:--|
| `AWS::EC2::Instance` (`t4g.micro/small/large`) | One MicroVM running `app/`; size sets memory and CPU | Per instance size |
| `AWS::AutoScaling::AutoScalingGroup` + launch template | The judge adds and removes MicroVMs during the week | Per running instance |
| `AWS::RDS::DBInstance` | A Virtual RDS store; class sets capacity | Per tier |
| `AWS::ElastiCache::CacheCluster` | A cache service outside the VM | Per node |
| `AWS::ElasticLoadBalancingV2::*` | Our load balancer | Fixed |
| `AWS::EC2::VPC`, `Subnet`, `SecurityGroup` | Accepted and ignored | Free |
| IAM, Lambda, custom resources, `Transform`, macros | Rejected at push | — |

Template functions: `Ref`, `GetAtt`, `Sub`, `Join`, `Parameters`. Database and
cache addresses reach the app as environment variables.

**CDK:** teams may use CDK locally but commit the synthesized template. The judge
never runs `cdk synth`, because that executes the team's code.

**Phases:**

1. **Template only.** Teams push `infra/template.yaml`; our reference shop runs
   on their architecture. No team code runs.
2. **Template and app.** Teams also push `app/`, once the hosted-runner controls
   and a database API exist (below).

## Managing state: proposed

There are three kinds of state. Only the first uses real CloudFormation.

| State | What it is | Where it lives | When it changes |
|:--|:--|:--|:--|
| **Our infrastructure** | Functions, MicroVM image, bucket, tables | Real CloudFormation stacks | When an organiser deploys |
| **Team's declared infrastructure** | Frozen template, plus one record per resource: type, settings fingerprint, our ID | S3 (template and plan, never overwritten) and DynamoDB (current pointer) | Once per round, at the cutoff |
| **Team's data and business** | Database contents, carts, orders, payments, returning customers, cumulative profit | S3 (one saved copy per week, named by checksum) and DynamoDB (index and scores) | After every simulated week |

**Resources are records; VMs exist only while running.** A team's two
`t4g.small` instances are two records. During an evaluation they become two
MicroVMs for at most 120 s, then disappear. We pay nothing while teams build.

### The plan: a mini CloudFormation

On every push:

1. **Check** allowed types and properties (`cfn-lint` plus our allowlist).
2. **Resolve** `Ref`, `GetAtt` and `Sub` into a resource graph.
3. **Compare** with the last frozen round and label each change:

| Change | Rule | Effect |
|:--|:--|:--|
| Instance size | Modify | New VM size next week |
| Auto Scaling min/max | Modify | Applies immediately |
| `DBInstanceClass` | Modify, brief downtime window | Capacity changes; requests fail during the window |
| `Engine`, or the database's logical ID | **Replace** | New, empty database unless the release migrates the data |
| Cache added or removed | Add / Remove | The cache always starts empty |
| Instance replaced | Replace | Anything stored on the instance is lost |

4. **Report** the plan as a GitHub check on the commit:

```
BLACK FRIDAY plan for submit/round-2 @ 3f9c1e2
  + Cache   ElastiCache cache.t4g.micro        +$12/week
  ~ Db      db.t4g.small → db.t4g.large        +$30/week, modify
  - ShopB   EC2 t4g.small                      −$8/week
  ! Db      Engine postgres → aurora: REPLACE  data will NOT carry over without a migration
```

Prices in this example are illustrative; the price list is not set.

### A round

Each week depends on the previous week's state, and with the
[switchers pool](design.md#the-switchers-pool) on every team's results
from that week. So the weeks run in order, and within each week the teams run in
parallel. Durable fans the teams out, waits for all of them, allocates the
switchers in a checkpointed step, then starts the next week.

```mermaid
flowchart LR
  C[Cutoff: freeze SHA and template] --> P[Apply plan: new declared state]
  P --> F5{{Week 5: one judge per team, in parallel}}
  F5 --> S5[Allocate switchers]
  S5 --> F6{{Week 6: one judge per team}} --> S6[Allocate switchers]
  S6 --> F7{{Week 7}} --> S7[Allocate switchers] --> F8{{Week 8}}
  F8 --> R[Round result and leaderboard]
```

Each team's week is one Event judge:

1. **Restore** last week's saved state: database contents, returning customers,
   the switchers allocated to this team, payment record.
2. **Boot** what the template declares: MicroVMs at the frozen commit, Virtual
   RDS loaded with the saved data, an empty cache, our load balancer.
3. **Play** the week's traffic; the referee checks every order and payment.
4. **Export** the database and destroy the VMs.
5. **Save** a new copy named by its checksum and record the score.

Durable saves progress after each week and after each allocation, so a failed
week 7 judge reruns from week 6 without changing anyone's switchers. A week
waits for its slowest team. If a team's judge still fails after its retry limit,
that team's week is scored as an outage (every checkout failed) and the round
moves on.

### Rules

- **Saved states are never overwritten.** The pointer moves only after the
  referee's result is recorded.
- **Reuse key:** commit, template checksum, scenario version, seed, starting state
  checksum, inbound switchers checksum and judge version. The same key gives the
  same result, so a retry reuses it rather than counting twice.
- **Practice runs** read official saved states and write only to a practice area.
- **One official timeline per team.** Extra seeds are for practice and robustness.

## Where the line sits

| Crosses between a team's VM and our side | Never crosses |
|:--|:--|
| HTTP from the judge to the shop, port 8080, short-lived token | AWS credentials (the VM has no role) |
| Named database calls to Virtual RDS (proposed) | The internet (once outbound access is off; proposed) |
| One payment receipt per checkout, through a one-use S3 link | Other teams' VMs and data |
| The exported database at the end of a stage | The referee's ledger and scores |

## Built vs to build

| Piece | Status |
|:--|:--|
| Our infrastructure as CloudFormation | Built |
| Durable round with pauses, Event judges, parallel fan-out | Built |
| MicroVM shop image, gateway, payment relay, checkpoints between stages | Built |
| Frozen commits from signed GitHub pushes | Built (`submissions.py`) |
| Local simulator and pseudo-EC2 bridge | Built |
| Virtual RDS with saved and restored state | Built (`virtual_rds.py run --checkpoint` / `resume`) |
| Template checker, resolver and plan | To build |
| Per-week loop and returning-customer state | To build |
| GitHub App registration, checks and public leaderboard | To build |
| Database API for team apps (named operations, hosted outside the VM) | To build; the hard part of phase 2 |
| Cache service outside the VM | To build |
| Hosted-runner controls: separate account, no outbound internet, isolated builds | To build before phase 2 |
