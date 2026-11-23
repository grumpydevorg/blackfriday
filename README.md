# BLACK FRIDAY

**Everyone inherits the same online shop and has to get it through the holiday
rush. Most profit wins, not most servers.**

BLACK FRIDAY is a cloud architecture game, built for the AWS User Group
Adelaide talk *What Is Going On With Lambda? Five Lambdas for the Agent Era*
(25 November 2026). Teams run one shop through four rounds and 13 simulated
weeks. They choose what to scale, what to cache and how to migrate their data.
A separate referee checks every checkout, customers remember bad days, and the
score is profit after the AWS bill.

The teams scale their shops. The organisers scale the judging, and that is
where the five kinds of Lambda come in:

| Lambda | In BLACK FRIDAY |
|:--|:--|
| Web Functions | Serve the arena |
| Durable Functions | Run the round, wait for submissions to close, fan out the judging |
| Event Functions | Judge one release against one scenario |
| MicroVMs | Run a team's shop code, separately from the judge |
| Managed Instances | Optional capacity for the same judges when the work is steady (described, not measured) |

## Status

This is a working prototype, not a hosted tournament. What has been run, and
with what limits, is recorded in [`docs/game.md`](docs/game.md#what-exists-today-and-what-is-proposed).
No outside or audience code has been run; [what that would
need](docs/processes/running-outside-code.md) is written down.

## What is here

| Path | Contents |
|:--|:--|
| [`docs/game.md`](docs/game.md) | The game on one page |
| [`docs/architecture.md`](docs/architecture.md) | Both sides of the infrastructure: what is built, what is proposed, how state is managed |
| [`docs/design.md`](docs/design.md) | The full design specification |
| [`docs/processes/`](docs/processes/) | How to run each part: locally, in AWS, Git submissions, MicroVMs |
| [`talk/`](talk/) | Slides (`index.html`, opens offline) and speaker notes |
| [`demo/`](demo/) | The shop, the referee, the simulators and the AWS templates |
| [`assets/`](assets/) | Concept art (ChatGPT-generated, illustrative only) |

## Try it

Score five shop designs on the same customer trace, with no AWS account and no
Docker:

```sh
cd demo
uv run --no-project python virtual_rds.py compare
```

Run the tests:

```sh
cd demo
uv run --no-project --with pytest --with boto3==1.44.9 --with pydantic==2.13.4 pytest -q
```

The local arena (Docker), the AWS rehearsal and the Lambda fan-out are in
[`docs/processes/`](docs/processes/). The AWS scripts only run against the
account named in `BLACK_FRIDAY_AWS_ACCOUNT`, in Sydney (`ap-southeast-2`), and
delete what they create; read the process before running one.

## Licence

Code and documentation: [MIT](LICENSE). Artwork in `assets/`: CC BY 4.0
([details](assets/README.md)).
