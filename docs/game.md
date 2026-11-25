# BLACK FRIDAY: the game on one page

*One shop. Four rounds. Thirteen weeks. Survive Black Friday.*

Every player inherits the same online shop. Players decide how to scale it; the
organisers decide how to scale the judging. The winner has the highest profit
after the AWS bill, not the most AWS services.

This page is the game as built. The longer specification (a Git-submitted shop,
the switchers pool, a referee over many scenarios) is in [design.md](design.md).
How it is built: [architecture.md](architecture.md). Runnable steps:
[processes](processes/README.md).

## The game as built

- **One phone is one player is one shop.** The product is one $25 "Cloud Mug".
  Each verified sale contributes $10 before technology costs. No pricing,
  advertising or inventory decisions, and nobody uploads code.
- **Four rounds, three options each**, 13 simulated weeks in all. Every round has
  a forecast and one shared random market draw, the same for every player.
  Players pick one option before the presenter closes the round.
- **Joining.** Open the public page and join with an optional name; a blank name
  becomes a generated `Adjective Noun` (for example "Spiky Lambda"), unique
  ignoring case. A name is 1–20 printable characters. Tapping an option before
  joining joins and picks in one go. Names can change until the Launch round
  closes. Joining stays open until the Growth round closes, so a late joiner
  starts from the shop an all-default player would have by then, including its
  profit so far. At most 100 audience players ("The game is full").
- **Picking.** A pick counts only for the round that is open, and can be changed
  until the presenter closes it. Nobody picks for a round in advance. A player
  who does not pick gets the first option of the round (the default). The
  presenter closing the round freezes every pick at that instant.
- **Everyone is on the board.** The house player ("House (does nothing)")
  plays every round's default. Bots (Bot Lambo, Bot Turbo, Bot Kevin, Bot
  Monolith, Bot Spike) fill the board until there are six players counting the
  audience and the house; they are labelled, their names are reserved, and they
  draw a random allowed option each round.
- **Customers remember.** Customers who got a fast checkout come back more often
  than ones who waited or failed, and customers whose cart was lost or who were
  charged twice rarely return. This carries from round to round.

| Round | Weeks | Market draw | A (the default) | B | C |
|:--|:--|:--|:--|:--|:--|
| 1 Launch | 1–4 | demand 80–120% of forecast | **Small server** | Medium server | Large server |
| 2 Growth | 5–8 | demand 80–120% | **Stay on the server's disk** | Careful migration | Fast migration (dice: 38%) |
| 3 Black Friday | 9–10 | spike 8×–15× | **Ride it out** | Autoscale (dice: 35%) | Queue checkouts (dice: 21%) |
| 4 Christmas | 11–13 | demand 70–120% | **Keep what you have** | Scale down | Commit to steady capacity |

`autoscale` needs a shared database, so it is refused unless the player picked
Careful or Fast in Growth: the server answers 409 with the reason, phones grey
the button out, bots never draw it without the requirement, and a default never
lands on it. The options, their text, risks and requirements are in
[`rounds.json`](../demo/aws/rounds.json), the one table read by the phones, the
web function, the workflow and the model.

Each player also rolls once per round, 0–99, and a risky option "hits" when the
roll is below its risk. Everyone gets a small personal demand noise of −10% to
+10% so ties are rare. Rolls, noise, the market and the bots' picks are all drawn
in one checkpointed Durable step per round, so a replay never draws again.

## What the presenter does

Sign in on `/host` (or run `operate.py present`), then:

1. **Start** (only before the game starts): the real part runs, then Round 1
   opens.
2. **Close round N**: freezes the picks, scores the round, publishes its results
   and opens the next round. After Round 4 the game is final.
3. **Reset**: throws every player and score away and returns to a fresh lobby.
   Works in any phase; the page asks first unless the game is waiting or final.
4. **Remove**: takes a player off the roster (a squatted or rude name); that
   phone can join again while joining is open. The presenter page offers it
   only while joining is open.

The runbook is [AWS game and rehearsal](processes/aws-rehearsal.md).

## Real and modelled

Real, once after **Start** and before Round 1 opens, in disposable MicroVMs:

- **Launch:** the unchanged PHP/SQLite shop sells one mug and leaves a saved cart
  (one MicroVM).
- **Both growth migrations**, each checked afterwards in its own MicroVM:
  *careful* copies every table to PostgreSQL (carts preserved), *fast* omits the
  carts table. The check asks whether the saved cart survived.

No MicroVM runs after that. If a real step fails, the workflow uses the
`recorded_evidence` in `rounds.json` (careful keeps the cart, fast loses it) and
carries on; only the failing option is recorded (if Launch fails, both). Every
result row carries an `evidence` badge, `real` or `recorded` for the careful and
fast rows and `modelled` for everything else, and an `outcome` (`held`, `hit` or
`lost_carts`) that decides its colour. The presenter page shows the evidence,
including Launch's.

Everything else is modelled in [`model.py`](../demo/aws/model.py): demand,
capacity, costs, Black Friday, Christmas and the dice. The prices are in
cents per week in `rounds.json`, not an AWS bill. Christmas `commit` is priced like
Managed Instances (cheaper per unit of capacity, paid even when quiet); that
price is **modelled, not measured**. Lambda rejected the attempted Managed
Instances pool configuration.

## Economics

Profit per round = $10 × verified sales − cost − refunds. A sale is verified when
the customer was served fast or slow; a customer who is not served is a failure
that earns nothing. The score is cumulative profit over the four rounds. Ranking
is by cumulative profit; equal profit shares a rank, then name, then id.

- **Demand** = new arrivals × the market draw × (100 + noise)% + returning
  customers. New arrivals per week: Launch 40, Growth 56, Black Friday 2 (× the
  8×–15× spike), Christmas 53.
- **Serving.** Capacity is customers per week × weeks. Demand up to capacity is
  served fast; a band of 14% of capacity above it is served slowly (still a
  sale); the rest fail.
- **Returning customers**, as a share of last round's customers: 75% of fast, 50%
  of slow, 20% of failed. Only 5% of customers whose cart or history was lost, or
  who were double-charged, return.
- **Weeks per round:** 4, 4, 2, 3.

| Server | Customers/week | Cost per week |
|:--|--:|--:|
| Small | 60 | $315.00 |
| Medium | 80 | $420.00 |
| Large | 104 | $480.13 |

| Round | Option | Effect |
|:--|:--|:--|
| Growth | Stay | No cost. From here the database on the disk caps the shop at 89 customers per week, whatever its server size. |
| Growth | Careful | $140.89 once, +$22.90 per week for the shared database. The shop then runs at 120% of its server's capacity and cost. Carts are lost only if the real check shows they were. |
| Growth | Fast | $38.40 once, +$1.86 per week. Same 120% as Careful. Carts are lost when the dice hit (roll under 38) and the real check shows the omitted carts table lost the cart. A lost cart counts the previous round's customers as lost. |
| Black Friday | Ride | Keeps the capacity it has. |
| Black Friday | Autoscale | Capacity follows demand; cost is $4.51 per added unit of capacity per week, **billed twice**: once as the ramp and once in the new running cost. So the real price is about double the listed one when it scales on time, and between 1× and 2× when the dice hit. `rounds.json` is tuned to this: billing it once makes the balance check fail (autoscale on the best path in 93% of worlds). Under 35 it scales late: the first 60% of the period runs at the old capacity. |
| Black Friday | Queue | $11.85 per week plus $3.62 per queued checkout, rising with queue depth. Nobody fails; queued checkouts are slow sales. Under 21 retries double-charge 36% of them and each is refunded $36. |
| Christmas | Keep | Black Friday capacity. |
| Christmas | Down | Capacity capped at 90 per week, cost scaled to match. |
| Christmas | Commit | Capacity 103 per week at $5.39 per unit per week for the three weeks, paid whether or not it is used, plus the shared database's weekly fee if the player has one. Modelled, not measured. |

Every shop also pays its server and database cost for every week of every round.
In Christmas, a shop that lost its carts in Growth has 40% of its previous
customers counted as lost, whichever option it picks. A shop that loses carts or
double-charges customers loses future sales as well as the refund, so a cheap
fast migration that goes wrong can cost more than it saved.

### Balance

`python demo/aws/model.py balance` plays every legal path through the four
rounds in 4,000 random worlds (a fixed seed) and checks that no option is
pointless: every option is on the best path in at least 10% of worlds; no path
is beaten by another in every world; Black Friday explains at most 50% of the
difference between paths; and the all-default shop (small, stay, ride, keep)
makes money and ranks between the 25th and 75th percentile. It assumes the real
check showed careful kept its carts and fast lost them. Current result:

```
balance: 4000 worlds x 72 legal paths (autoscale needs a shared database), seed 20261125, fast migration cart_kept=False
  on the best path launch      small  57.5%  medium  25.4%  large  17.1%
  on the best path growth      stay  23.1%  careful  17.1%  fast  59.7%
  on the best path blackfriday ride  18.5%  autoscale  24.2%  queue  57.3%
  on the best path christmas   keep  26.2%  down  18.2%  commit  55.6%
  paths beaten by another path in every world: 0
  share of between-path variance: launch 10.3%  growth 67.1%  blackfriday 21.0%  christmas  1.6%
  all-default small/stay/ride/keep: mean $3,642, mean path percentile 38
  mean total over paths: best $4,135, worst $2,857
BALANCE PASS
```

Growth carries 67.1% of the variance between paths; that is where the real
migrations sit.

## Two scales

- **Players scale their shops:** server size, shared database, autoscale, queue,
  committed capacity.
- **Organisers scale the judging.** In the built game the judging is small: one
  real Launch, two real migrations and two real checks. The fan-out of one
  evaluation per release and scenario is the proposed tournament
  ([design.md](design.md)) and was measured separately in the launch wave below.

## Five Lambda choices

The five kinds of Lambda and what each does in BLACK FRIDAY are in the
[README](../README.md#black-friday); Managed Instances is priced in Christmas's
`commit` option, modelled, not measured.

## What exists today, and what is proposed

Exists and has been run:

- **The four-round game** (above) in Sydney as the `fourround` deployment, the
  only deployment now present: a public player page and a keyed presenter page
  on one Web Function (`black-friday-four`), a version-pinned Durable game, and
  the real Launch and growth stages in MicroVMs. Up to 100 audience players, the
  house and bots. Go/no-go numbers for this game:
  [AWS game and rehearsal](processes/aws-rehearsal.md#gono-go-measurements).
  The build presented at the talk is tagged `fourround-talk-2026-11-25`.
- **The live log**, a second, presenter-only Web Function
  (`black-friday-four-logs`) that streams the game's Durable history, timeline
  and CloudWatch lines to a viewer with Story and Follow modes; read-only, and
  separate from the game. See [architecture.md](architecture.md#the-logs-function).
- **The one-round game** it replaced, the `round` deployment, frozen at tag
  `round-fallback-2026-11-25`: up to 200 teams picked one of three migration
  paths in a single Growth round. It was measured in Sydney on 24 Nov 2026
  (Launch 9–11 s, Close 10–14 s, shoppers about 11 s, 40 teams a full round, 200
  joins in 7.1 s). Those numbers belong to that game, not the four-round one.
  Destroyed on 25 Nov 2026, artifacts bucket kept; redeploy it from the tag.
- An AWS rehearsal (Oregon, then Sydney since 23 Nov 2026), frozen at tag
  `rehearsal-fallback-2026-11-24b`: from one Launch checkpoint, two Growth
  migrations both passed `/health`; only one kept a saved cart. Destroyed on
  25 Nov 2026, artifacts bucket kept; redeploy it from the tag.
- Git submission intake: push to `submit/round-1`, signed webhook, exact SHA
  queued, archive pinned and hashed, frozen export.
- One reviewed PHP fixture at a frozen commit ran in a real MicroVM and earned
  82 sales × $25 = $2,050. No audience or outside code has been run.
- Launch-wave fan-out (22 Nov 2026): 32 evaluations, one worker 147.12 s, eight
  workers 22.61 s (6.5×), identical scores. One observation, not a benchmark.
- A local Virtual RDS scoring round and a local virtual AWS simulator.

Proposed, not built: the full tournament with switchers and a Git-submitted
shop, a hosted contestant runner, running untrusted code (needs a separate AWS
account, isolated build/run MicroVMs, externally enforced egress controls and
independent scoring). Managed Instances is **not measured**.
