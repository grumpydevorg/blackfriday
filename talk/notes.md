# What Is Going On With Lambda?

**Five Lambdas for the Agent Era — Rick Morgans**  
AWS User Group Adelaide, Wednesday 25 November 2026, 5.30pm, CORE Innovation Hub, Adelaide.

## Run sheet

The simulation is described, not run live. Open `index.html` locally; no server or network is needed. Keep the sibling `assets/` directory with it. Use ←/→, Space, PageUp/PageDown, Home/End; F requests fullscreen where the browser supports it. A URL hash such as `#9` opens that slide.

Slides 8–15 carry a status tag. **Built and run** (green) means we have run it in Oregon. **Proposed** (amber) means designed, not built. Where a slide mixes both, each item is tagged inline. Detail for the design is in [`docs/architecture.md`](../docs/architecture.md).

| Slide | Topic | Status | Minutes | Elapsed |
|---|---|---|---:|---:|
| 1 | Title and promise | | 2 | 2 |
| 2 | 2014 rules versus 2026 | | 3 | 5 |
| 3 | Five choices | | 2.5 | 7.5 |
| 4 | The game | | 2.5 | 10 |
| 5 | Profit and the referee | | 2 | 12 |
| 6 | Maya's checkout | | 2 | 14 |
| 7 | Two healthy migrations | | 2.5 | 16.5 |
| 8 | Two sides of the infrastructure | mixed | 2.5 | 19 |
| 9 | The team's tiny cloud | Proposed | 3 | 22 |
| 10 | One instance = one tiny MicroVM | Built and run | 3 | 25 |
| 11 | Our side scales the judging | Built and run | 2.5 | 27.5 |
| 12 | Resources are records | Proposed | 2.5 | 30 |
| 13 | Where the line sits | mixed | 2 | 32 |
| 14 | Managed Instances | not measured | 1.5 | 33.5 |
| 15 | The arena | mixed | 2 | 35.5 |
| 16 | Fine print | | 1.5 | 37 |
| 17 | Code and December meetup | | 1 | 38 |
| **Total** | | | **38** | |

## Speaker notes

### 1. What Is Going On With Lambda? — 2 minutes

Lambda now covers several different workload choices, so tonight we give each one a job in BLACK FRIDAY. This is a game about running an online shop through a seasonal rush, and we describe the simulation rather than run it live. The glowing shop is concept art, not evidence of a deployed tournament.

### 2. Every 2014 rule has been broken — 3 minutes

Lambda's November 2014 preview was Node.js only, with a 60-second limit, 128 MB to 1 GB of memory and 100 ms billing; five minutes came in October 2015 and 15 minutes in 2018 [S1–S3]. Read the crossed-out rules as “no longer a universal description of the Lambda brand”, not “classic functions no longer work”: Durable adds checkpoints and waiting, Web Functions serves concurrent HTTP requests, and Managed Instances puts Event Function compute on an AWS-managed EC2 pool in your account [S3–S6]. The 90-minute Managed Instances timeout arrived in September 2026, but does not apply to synchronous calls [S6]. The pricing row is a change of capacity model, not a claim that every Lambda option has stopped charging per request: we have not measured Managed Instances pricing or savings.

### 3. Five Lambda choices — 2.5 minutes

Choose by workload and trust boundary, not as five interchangeable runtimes. Web Functions serves the arena, a Durable workflow runs the round and waits for submissions to close, and an Event evaluator judges one frozen release against one scenario. MicroVMs run the shop apart from the judge, while Managed Instances is an alternative compute option for those same trusted Event judges when load is steady. Web Functions and the Event Functions name arrived in October 2026; MicroVMs in June [S4–S5].

### 4. BLACK FRIDAY — 2.5 minutes

Every team inherits the same shop and sells the same $25 Cloud Mug, with $10 contribution per sale before technology costs. The four rounds cover 13 simulated weeks: Launch, weeks 1–4; Growth, 5–8; Black Friday, 9–10; Christmas, 11–13. Teams freeze one release each round and decide what to scale, cache and how to migrate their data; the score is cumulative net profit after the modelled AWS bill. The artwork illustrates the progression, not a measured architecture.

### 5. Profit wins, not servers — 2 minutes

The independent referee checks every checkout for a correct stored order and exactly one payment; a happy response from the shop is not enough. Customers remember lost carts and slow pages, so those failures cut future sales even when `/health` stays green. The bill is modelled as part of the game, so a cheap, well-tuned shop can beat an expensive one.

### 6. Maya's cart — 2 minutes

Walk through one checkout: Maya adds a Cloud Mug to her cart, pays $25 and expects a stored order to exist. The referee checks the order and payment independently rather than trusting the checkout response. Then she comes back: she has a saved cart, and expects the shop to remember it after the next release. Keep the cart and the completed order distinct.

### 7. Healthy is not correct — 2.5 minutes

In the Oregon rehearsal, two Growth migrations started from one Launch checkpoint and both passed `/health`, but only one kept returning customer Maya's saved cart. Ask the room which release a health monitor would call successful before revealing the cart row. This is a migration-correctness example, not a measured four-round tournament.

### 8. Two sides of the infrastructure — 2.5 minutes (mixed)

Make the split plain: a team's side is a small, constrained cloud — an `infra/template.yaml` declaring EC2, Auto Scaling, RDS, ElastiCache and an ALB, plus their `app/` code. That whole side is **Proposed**: the template format and its plan are designed, not built. Our side is real Lambda: a Web Function (the arena), a Durable Function (rounds, cutoffs, fan-out), Event Functions (one judge per team × week) and MicroVMs, where each “EC2 instance” is a tiny disposable VM. The presenter page, Durable workflow, Event judges and MicroVMs are **built and run**; registration, the leaderboard and Virtual RDS (shared state held outside the VM) are **Proposed**. Land the line: teams scale the shop, we scale the judging.

### 9. The team's tiny cloud — 3 minutes (Proposed)

This slide is a proposal, so say so up front. Teams would declare resources in a CloudFormation subset: allowed types only, with IAM, Lambda, custom resources and Transform rejected. A push would produce a plan as a GitHub check showing the weekly cost of each change, and flagging a replacement, such as changing the engine from postgres to Aurora, as data that will not carry over without a migration. CDK is fine locally, but teams commit the synthesized template; we never run `cdk synth`. The green “measured” box is real and separate: in the Oregon launch-wave run, an extra replica sold the same as small (1,558) while large and cached sold 2,216, because the storage tier was the bottleneck. It is one observation, not a tournament result.

### 10. One instance = one tiny MicroVM — 3 minutes (Built and run)

This is built and run. Inside each MicroVM a gateway listens on :8080 and hands requests to the app, which stores state in `/state` as SQLite or PostgreSQL; a payment relay writes a one-use S3 receipt. The limits: 120 seconds, then destroyed; no AWS role; inbound only from the judge, on port 8080, with a three-minute token; fixed 1 GiB ARM64; a fresh VM per stage, with only the exported database carried forward. Be direct about the amber warning: outbound internet is still on, and it must be off before we run anyone else's code. What has actually run is a reviewed PHP shop at a frozen Git commit in a real Oregon MicroVM, earning 82 sales × $25 = $2,050. No outside or audience code has been run, and a separate AWS account, externally enforced egress controls and independent scoring would also be needed first.

### 11. Our side scales the judging — 2.5 minutes (Built and run)

A Durable workflow fans out Event judges: four shop designs × eight seeded scenarios is 32 evaluations. With one worker the Launch-wave pass took 147.12 seconds; with eight it took 22.61 seconds, 6.5× faster with identical scores, across 62,336 real loopback HTTP requests per pass. It is one Oregon observation on 22 November 2026, not cold-start controlled and excluding stack setup and teardown. A later repeat ran 96 on-demand evaluations at an estimated worker charge of US$0.0029. The tournament sentence at the bottom is **Proposed**: every team plays a week in parallel, then Durable waits for all of them and hands out the switchers before the next week starts.

### 12. Resources are records — 2.5 minutes (Proposed)

Everything on this slide is a design, not a build. Three layers: our infrastructure is real CloudFormation stacks; a team's declared infrastructure is a frozen template plus per-resource records in S3 and DynamoDB, changing once per round; a team's data and business — database contents, carts, orders, payments, returning customers, profit — gets a new saved copy every simulated week and is never overwritten. A VM exists only while it plays a week: restore, boot, play, export, destroy, save. Because the same commit, template, scenario, seed, starting state and judge version give the same result, retries never double count.

### 13. Where the line sits — 2 minutes (mixed)

Compare what may cross the boundary with what must not. Crossing: HTTP from the judge to the shop, payment receipts from the VM to S3 through a one-use link, and scores from the judge to the arena are built. Named database calls from the VM to Virtual RDS are **Proposed**. Never crossing: AWS credentials (the VM has no role), other teams' VMs and the referee's ledger; the internet is on the list only once egress is switched off, which is **Proposed** — today outbound internet is still on.

### 14. Managed Instances — 1.5 minutes (not measured)

Managed Instances is an Event Function compute option on an AWS-managed EC2 pool in your account. The case is the same trusted judges running steadily rather than in bursts, and it is described only. Lambda rejected the pool configuration we attempted, so there are no timings, no costs and no savings claim. Do not carry the on-demand measurement from slide 11 across to this slide.

### 15. The arena — 2 minutes (mixed)

Today is **built and run**: a presenter page on a Web Function walks Launch → Deploy → Shopper returns, with the Durable workflow pausing for a click. Next is **Proposed**: installing the BLACK FRIDAY GitHub App on your repo would be registration, a push to `submit/round-N` would be an entry, and a public leaderboard would show the results. The dashboard is concept art; its teams, numbers and services are invented. The Git submission path has been tested with a disposable GitHub repository, but accepting audience code is not built.

### 16. Fine print for Adelaide — 1.5 minutes

Web Functions is Node.js only and is available in N. Virginia, Ohio, Oregon, Tokyo and Ireland, not Sydney; memory is still billed while waiting [S4, S7]. For MicroVMs, suspended time counts towards the eight-hour limit and snapshot storage is billed [S5, S7]. Managed Instances' 90-minute timeout does not apply to synchronous calls [S6, S7]. Check data residency before sending agent work offshore, and recheck region support and pricing before building.

### 17. Get the code — 1 minute

The repo is `https://github.com/grumpydevorg/blackfriday`, under the MIT licence, with the game rules, design and runbooks alongside the code. The QR code is an inline SVG generated with Segno, so it needs no network service to render. Invite questions, then leave the slide up for the next meetup: re:Invent 2026 Recap, Wednesday 16 December 2026, 5.30pm at CORE Innovation Hub.

## Sources for history and fine print

These are the public source links recorded in the talk's research outline. The 2026 region, runtime, billing and limit facts on slides 2, 3 and 16 come from that outline and the supplied talk brief; they were not independently re-verified while preparing this deck. The outline's Sources section does not attach a specific primary pricing/limits page to every line of its fine-print list, so [S7] identifies that provenance rather than inventing a citation.

- **S1 — AWS, 2014 launch:** [Run Code in the Cloud](https://aws.amazon.com/blogs/aws/run-code-cloud/). Preview date, initial triggers, runtime, memory, 60-second limit and billing granularity.
- **S2 — AWS, October 2015 update:** [Python, VPC, increased function duration, scheduling and more](https://aws.amazon.com/blogs/aws/aws-lambda-update-python-vpc-increased-function-duration-scheduling-and-more/). Five-minute timeout.
- **S3 — AWS:** [Lambda turns ten](https://aws.amazon.com/blogs/aws/aws-lambda-turns-ten-the-first-decade-of-serverless-innovation). Historical context; the outline's Part 1 records the 2018 15-minute limit. Additional reference: [Lambda timeline](https://hidekazu-konishi.com/entry/aws_history_and_timeline_amazon_lambda.html).
- **S4 — AWS, October 2026:** [Web Functions](https://aws.amazon.com/about-aws/whats-new/2026/10/aws-lambda-web-functions/). Announcement; the outline provides the Node.js and region list.
- **S5 — AWS, June 2026:** [MicroVMs](https://aws.amazon.com/about-aws/whats-new/2026/06/aws-lambda-microvms/). Announcement; the outline records the suspended-time and snapshot-storage caveats.
- **S6 — AWS, September 2026:** [Managed Instances 90-minute functions](https://aws.amazon.com/about-aws/whats-new/2026/09/aws-lambda-90-minute-function/). The outline records the synchronous-call exclusion.
- **S7 — Supplied research outline:** “What Is Going On With Lambda? Talk outline”, Part 1 §2–5 and Part 4 §15. Source for the specific fine-print wording.

## Claim boundaries to keep on stage

All artwork is concept art; do not read its sample prices, rankings or numbers as measurements. The one/eight-worker comparison is one Oregon Launch-wave observation, not a controlled cold-start study, proof of production scale or a shipped tournament. Managed Instances was not measured; no savings claim is supported. Everything tagged **Proposed** is a design, not something we have run. Accepting arbitrary audience code safely is not built: outbound internet from MicroVMs is still on.
