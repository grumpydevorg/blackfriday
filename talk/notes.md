# What Is Going On With Lambda?

**Five Lambdas for the Agent Era — Rick Morgans**  
AWS User Group Adelaide, Wednesday 25 November 2026, 5.30pm, CORE Innovation Hub, Adelaide.

## Run sheet

The simulation is described, not run live. Open `index.html` locally; no server or network is needed. Keep the sibling `assets/` directory with it. Use ←/→, Space, PageUp/PageDown, Home/End; F requests fullscreen where the browser supports it. A URL hash such as `#7` opens that slide.

| Slide | Topic | Minutes | Elapsed |
|---|---|---:|---:|
| 1 | Title and promise | 2 | 2 |
| 2 | 2014 rules versus 2026 | 4 | 6 |
| 3 | Five choices | 3 | 9 |
| 4 | The game | 3 | 12 |
| 5 | Profit and the referee | 2.5 | 14.5 |
| 6 | Maya's checkout | 2.5 | 17 |
| 7 | Two healthy migrations | 3 | 20 |
| 8 | Teams scale the shop | 2.5 | 22.5 |
| 9 | We scale the judging | 3.5 | 26 |
| 10 | MicroVM boundary | 3 | 29 |
| 11 | Managed Instances | 2 | 31 |
| 12 | Arena and takeaway | 1 | 32 |
| 13 | Fine print | 2 | 34 |
| 14 | Code and December meetup | 1 | 35 |
| **Total** | | **35** | |

## Speaker notes

### 1. What Is Going On With Lambda? — 2 minutes

Lambda now covers several different workload choices, so tonight we will give each one a job in BLACK FRIDAY. This is a game about running an online shop through a seasonal rush, and we will describe the simulation rather than run it live. The glowing shop is concept art, not evidence of a deployed tournament.

### 2. Every 2014 rule has been broken — 4 minutes

Lambda's November 2014 preview was Node.js only, with a 60-second limit, 128 MB to 1 GB of memory and 100 ms billing; five minutes came in October 2015 and 15 minutes in 2018 [S1–S3]. Read the crossed-out rules as “no longer a universal description of the Lambda brand”, not “classic functions no longer work”: Durable adds checkpoints and waiting, Web Functions serves concurrent HTTP requests, and Managed Instances puts Event Function compute on an AWS-managed EC2 pool in your account [S3–S6]. The 90-minute Managed Instances timeout arrived in September 2026, but does not apply to synchronous calls [S6]. The pricing row is deliberately a change of capacity model, not a claim that every Lambda option has stopped charging for requests: we have not measured Managed Instances pricing or savings.

### 3. Five Lambda choices — 3 minutes

Use workload and trust boundary to choose, rather than treating these as five interchangeable runtimes. Web Functions serves the arena, a Durable workflow runs the round and waits for submissions to close, and an Event evaluator judges one frozen release against one scenario. MicroVMs run the shop apart from the judge, while Managed Instances is an alternative compute option for those same trusted Event judges when the load is steady, not a sixth service or an isolated home for outside code. Web Functions and the Event Functions name arrived in October 2026; MicroVMs arrived in June [S4–S5].

### 4. BLACK FRIDAY — 3 minutes

Every team inherits the same shop and sells the same $25 Cloud Mug, with $10 contribution per sale before technology costs. The four rounds cover 13 simulated weeks: Launch, weeks 1–4; Growth, 5–8; Black Friday, 9–10; Christmas, 11–13. Teams freeze one release each round and decide what to scale, cache and how to migrate their data; the score is cumulative net profit after the modelled AWS bill. The artwork illustrates the progression, not a measured architecture or workload.

### 5. Profit wins, not servers — 2.5 minutes

The independent referee checks every checkout against a correct stored order and exactly one payment; a happy response from the shop is not enough. Customers remember lost carts and slow pages, so those failures reduce future sales even when `/health` stays green. The bill is modelled as part of the game, so a cheap, well-tuned shop can beat an expensive one rather than losing automatically to whoever bought the most servers.

### 6. Maya's cart — 2.5 minutes

Walk the room through one checkout: Maya adds a Cloud Mug to her cart, pays $25 and expects her stored order to exist. The referee checks the order and payment independently rather than trusting the checkout response. Now move forward to her return: Maya has a saved cart, and expects the shop to remember it after the next release. Keep the cart and the completed order distinct — this is a customer journey, not a claim that checking out always leaves the same cart active.

### 7. Healthy is not correct — 3 minutes

In the Oregon rehearsal, two Growth migrations started from one Launch checkpoint and both passed `/health`, but only one kept returning Maya's saved cart. Pause and ask which release the health monitor would declare successful before pointing to the cart row. The cart-preserving release lets her continue; the other loses that opportunity and affects later customer behaviour in the game. This is a migration-correctness example, not a measured four-round tournament.

### 8. Teams scale the shop — 2.5 minutes

The measured Launch wave compared four shop designs across eight seeded scenarios in Oregon on 22 November 2026. Small and extra-replica each earned 1,558 sales; large and cached each earned 2,216, because the storage tier was the bottleneck and the extra replica did not remove it. These are the measured sales beside the picture; the artwork itself is still only concept art. More replicas did not fix the shop.

### 9. We scale the judging — 3.5 minutes

Now change axes: organisers judge frozen release × scenario, so four designs and eight seeded scenarios give 32 evaluations. The Launch-wave pass made 62,336 real loopback HTTP requests and took 147.12 seconds with one worker versus 22.61 seconds with eight, 6.5× faster with identical scores. This was one observation, not cold-start controlled, and excludes stack setup and teardown; it is not an internet-load or production-throughput benchmark. A later repeat ran 96 on-demand evaluations with an estimated worker charge of US$0.0029, which is not the whole stack bill. More Lambda workers let us judge all four designs faster.

### 10. MicroVMs — 3 minutes

A real Oregon MicroVM ran one reviewed PHP fixture at a frozen commit and earned 82 sales × $25 = $2,050 of game revenue, not profit. The rehearsal used disposable roleless MicroVMs capped at 120 seconds, with the Event evaluator separate from shop code; it also used S3 checkpoints and DynamoDB run state. No audience or outside code has been run, and running it would require a separate AWS account, isolated build and run MicroVMs, externally enforced egress controls and independent scoring. That outside-code system has not been built: a MicroVM on its own is not the full safety design.

### 11. Managed Instances — 2 minutes

Managed Instances is an Event Function compute option on an AWS-managed EC2 pool in your account, so the candidate workload here is the same trusted judge running steadily rather than in bursts. It is described only: Lambda rejected the pool configuration we attempted, so there are no timings or savings to present. Do not put the on-demand measurement from the previous slides into the Managed Instances column.

### 12. The arena — 1 minute

The arena picture is concept art: its teams, numbers, prices and services are not results. Say the separation once more: “The teams decide how to scale the shop. We decide how to scale the judging.” The useful takeaway is not the size of the dashboard — more replicas did not fix the shop, but more Lambda workers let us judge all four designs faster.

### 13. Fine print for Adelaide — 2 minutes

Web Functions is Node.js only and is available in N. Virginia, Ohio, Oregon, Tokyo and Ireland, not Sydney; memory is still billed while waiting [S4, S7]. For MicroVMs, suspended time counts towards the eight-hour limit and snapshot storage is billed [S5, S7]. Managed Instances' 90-minute timeout does not apply to synchronous calls [S6, S7]. Check data residency before sending agent work offshore, and recheck current region support and pricing before a deployment rather than treating these slides as a service contract.

### 14. Get the code — 1 minute

The repo is `https://github.com/grumpydevorg/blackfriday`, under the MIT licence, with the game rules, design and runbooks alongside the code. The QR code points to that exact address; it is an inline SVG generated with Segno, so it needs no network service to render. Invite questions, then leave the slide up for the next meetup: re:Invent 2026 Recap, Wednesday 16 December 2026, 5.30pm at CORE Innovation Hub.

## Sources for history and fine print

These are the public source links recorded in the talk's research outline. The 2026 region, runtime, billing and limit facts on slides 2, 3 and 13 come from that outline and the supplied talk brief; they were not independently re-verified while preparing this deck. In particular, the outline's Sources section does not attach a specific primary pricing/limits page to every line of its fine-print list, so [S7] identifies that provenance rather than inventing a citation.

- **S1 — AWS, 2014 launch:** [Run Code in the Cloud](https://aws.amazon.com/blogs/aws/run-code-cloud/). Preview date, initial triggers, runtime, memory, 60-second limit and billing granularity.
- **S2 — AWS, October 2015 update:** [Python, VPC, increased function duration, scheduling and more](https://aws.amazon.com/blogs/aws/aws-lambda-update-python-vpc-increased-function-duration-scheduling-and-more/). Five-minute timeout.
- **S3 — AWS:** [Lambda turns ten](https://aws.amazon.com/blogs/aws/aws-lambda-turns-ten-the-first-decade-of-serverless-innovation). Historical context; the research outline's Part 1 records the 2018 15-minute limit. Additional outline reference: [Lambda timeline](https://hidekazu-konishi.com/entry/aws_history_and_timeline_amazon_lambda.html).
- **S4 — AWS, October 2026:** [Web Functions](https://aws.amazon.com/about-aws/whats-new/2026/10/aws-lambda-web-functions/). Web Functions announcement; outline Part 1 and Part 4 §15 provide the Node.js and region list.
- **S5 — AWS, June 2026:** [MicroVMs](https://aws.amazon.com/about-aws/whats-new/2026/06/aws-lambda-microvms/). MicroVM announcement; outline Part 4 §15 records the suspended-time and snapshot-storage caveats.
- **S6 — AWS, September 2026:** [Managed Instances 90-minute functions](https://aws.amazon.com/about-aws/whats-new/2026/09/aws-lambda-90-minute-function/). Outline Part 4 §15 records the synchronous-call exclusion.
- **S7 — Supplied research outline:** “What Is Going On With Lambda? Talk outline”, Part 1 §2–5 and Part 4 §15. Source for the specific fine-print wording; no private-repo link or personal correspondence is needed to use this public deck.

## Claim boundaries to keep on stage

All artwork is concept art; do not read its sample prices, rankings or numbers as measurements. The one/eight-worker comparison is one Oregon Launch-wave observation, not a controlled cold-start study, proof of production scale or a shipped tournament. Managed Instances was not measured; no savings claim is supported. The signed GitHub submission path was tested with a disposable repository, but accepting arbitrary audience code safely is not built.
