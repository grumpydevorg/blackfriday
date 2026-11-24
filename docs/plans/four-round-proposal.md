# BLACK FRIDAY — four-round MVP proposal (to build today, talk is tonight 25 Nov 2026)

Repo: /Users/rick/Git/personal/blackfriday. Live code: demo/aws/ (game.mjs, web.mjs, roster.mjs, workflow.py, scoring.py, evaluator.py, vm_runtime.py, round.js, player.html, host.html, options.json, rehearsal.yaml, operate.py, tests test_growth.py/test_workflow.py/test_roster.py/test_host_auth.py). Model: demo/virtual_rds.py. Shop: demo/shop.php, demo/migrate.php (has 'preserve' and 'omit' modes). Runbook: docs/processes/aws-rehearsal.md. Docs README.md, docs/game.md (stale: still describes A/B release picks, 8 teams).

## Today's deployed game (commit 4d3a27d)
One Growth round. Up to 200 teams on phones (one phone = one "team": name + token). Three options (Lift & shift 5% risk, Scale up +$40 10%, Refactor 30%) in options.json. Presenter /host buttons: Launch → Close & deploy every release → Send shoppers back. Durable workflow: Launch MicroVM, callback (Close, frozen roster), checkpointed step draws bots (fill to 6) + per-team roll, real PostgreSQL migration per option in use (≤3 MicroVMs), callback, compare per option (≤3 MicroVMs), publish ranked board. Measured in Sydney 24 Nov: Launch 9–11 s, Close 10–14 s, shoppers ~11 s; 40 teams full round OK; 200 joins 7.1 s. Phones poll every 2 s; state cached 2 s per environment. Fallback: frozen rehearsal deployment tag rehearsal-fallback-2026-11-24b + local demo.

## Proposed game
- One device = one player = one shop. Opening the page joins; name optional any time; anyone still nameless when presenter presses Close gets an auto-generated unique name ("Spiky Lambda"), drawn in the same checkpointed step as dice. Bots fill to 6. House teams dropped.
- Four rounds, three fixed buttons each. Option A always the safe default with no dice; no pick or late join = A for missed rounds (so late joiners replay deterministically).
- Randomness: a shared market draw per round (same for everyone) + personal dice on risky options.

| Round | Market draw | A (safe default) | B | C |
|:--|:--|:--|:--|:--|
| 1 Launch | demand ±20% | Small server | Medium server | Large server |
| 2 Growth | demand ±20% | Stay on server's disk | Careful migration to shared DB (+$, small risk) | Fast migration (cheap, 30% lose carts) |
| 3 Black Friday | spike 8×–15× | Ride it out | Autoscale (dice: slow to react; fails if R2 left you on disk) | Queue checkouts (never fails, slower; dice: duplicate charges) |
| 4 Christmas | steady; customers check order history | Keep what you have | Scale down | Committed steady capacity (Managed Instances-like; modelled pricing, NOT measured — Lambda rejected the pool config) |

## Model
Per-player state {server, data: disk|shared, spike, steady, customers_returning, history_lost, duplicate_charges}. One pure function per round:
play(state, pick, market, roll) -> (state', result): capacity from choices, demand from market; fast sales up to capacity, overflow slow then failed; profit = $10 × verified sales − rate-card cost − refunds; returning = 75% fast + 50% slow + 20% failed (5% if history lost). Customer counts, not individuals (ms for 200 players). rounds.json replaces options.json (forecast text, options, rate card, risks), read by phones, web and scoring. Balance check: Monte Carlo over 81 paths × draws; each option of each round must be best in ≥~20% of worlds.

## State machine
Lobby (Reset; Launch MicroVM runs while people join) → Start → Open(r) → Close (freeze picks; draw names/bots/market/dice; score) → Revealed(r) → Next → Preparing (next round's real work) → Open(r+1) … → Final after R4 → Reset → Lobby. Presenter buttons: Start, Close round, Next round, Reset. Join/name open until R4 Close; picks only in Open.

## Under the hood
- Web Function (game.mjs, web.mjs): POST /api/join (no name), /api/name, /api/pick {round, option}; presenter /api/start, /api/close, /api/next, /api/reset; remove /api/launch, /api/migrate, /api/compare.
- Durable workflow.py loop:
  for r in 1..4: run real work if any (R1 Launch; R2: both careful and fast migrations once each, regardless of picks); publish Open(r) and wait Close callback (picks frozen in one atomic write); step plan(r) names/bots/market/dice (checkpointed); step score(r) pure; publish Revealed(r); if r<4 wait Next callback. Publish Final. 8 callbacks, each ≤23 h.
- Event Function + MicroVMs mostly unchanged: Launch real (in lobby); R2 migrations real, run while presenter talks over R1 results; fast-migration roll<30% gets the real 'omit' run's result (lost cart); widen vm_runtime/evaluator kind check from preserve-only to preserve|omit. R3/R4 modelled only, no MicroVMs; board labels results "real" vs "modelled".
- Phones/presenter (round.js, html): per round forecast card, 3 buttons, your result (sales/slow/failed/cost/dice/customers returning), cumulative board top 10 + own row.

## Speed (today measured; proposed estimated, not measured)
Launch 9–11 s → hidden in lobby. R2 migrations 10–14 s + 11 s shoppers → hidden behind R1 reveal. Close→phones ~10–14 s → ~2 s + ≤2 s poll. Per round on stage ~45 s picking + talk.

## Build order
1. Tag current deployed round round-fallback-2026-11-25.
2. rounds.json → model → balance sim.
3. Durable loop + tests (fake Durable context in test_workflow.py).
4. Web routes + pages.
5. Deploy Sydney; full four-round game with ~40 simulated phones + 1 real phone; record Close→reveal; check scores against model.
6. Update README, game.md, aws-rehearsal.md, architecture.md.

Risks: DynamoDB 400 KB item and Durable checkpoint/callback payload limits at 200 players × 4 rounds; same-day rewrite of workflow, routes and pages; fallback = redeploy tag.
Defaults: drop houses; late joiners get A; names lock after R1 Close; no countdown; need a hard go/no-go time.
