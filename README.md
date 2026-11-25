# BLACK FRIDAY

**Everyone inherits the same online shop and has to get it through the holiday
rush. Most profit wins, not most servers.**

BLACK FRIDAY is a cloud architecture game, built for the AWS User Group
Adelaide talk *What Is Going On With Lambda? Five Lambdas for the Agent Era*
(25 November 2026). Every player runs one shop through four rounds and 13
simulated weeks: Launch, Growth, Black Friday and Christmas. Each round they pick
one of three options, such as a server size, a careful or a fast database
migration, autoscaling or a queue. Customers remember bad days, and the score is
profit after the modelled AWS bill.

The players scale their shops. The organisers run the game on Lambda, and that
is where the five kinds of Lambda come in:

| Lambda | In BLACK FRIDAY |
|:--|:--|
| Web Functions | Serve the player and presenter pages and the API; a second streams the presenter's live log |
| Durable Functions | Run the game: the five real stages (Launch, two migrations, two checks), then wait for each round to close, plan, score and publish |
| Event Functions | Run one real stage: Launch, a migration or a check |
| MicroVMs | Run the shop for each real stage, separately from the judge |
| Managed Instances | Priced in Christmas's `commit` option; modelled, not measured |

## Status

This is a working prototype, not a hosted tournament. The four-round game can
be played live in AWS (the `fourround` deployment, the only one now present): up
to 100 audience players join on their phones, with or without a name, and pick
one of three options per round, each with their own dice roll, while the presenter
runs the rounds. A House player and labelled bots fill the board. Only Launch
(one real MicroVM sale) and the two growth migrations (careful and fast, each
checked in its own MicroVM) are real; they run once after Start, before Round 1
opens. Demand, capacity, costs, Black Friday and Christmas are modelled, and
Christmas `commit` is Managed Instances-like pricing that is modelled, not
measured. The earlier one-round game and the rehearsal were destroyed on
25 November 2026 and can be redeployed from their tags
([how](docs/processes/aws-rehearsal.md#the-frozen-deployments)). What has been
run, and with what limits, is recorded in
[`docs/game.md`](docs/game.md#what-exists-today-and-what-is-proposed).
Players only pick; no outside or audience code has been run, and [what that
would need](docs/processes/running-outside-code.md) is written down.

## What is here

| Path | Contents |
|:--|:--|
| [`docs/game.md`](docs/game.md) | The game on one page: rules, economics, balance |
| [`docs/architecture.md`](docs/architecture.md) | Both sides of the infrastructure: what is built, what is proposed, how state is managed |
| [`docs/design.md`](docs/design.md) | The full design specification |
| [`docs/processes/`](docs/processes/) | How to run each part: locally, in AWS, Git submissions, MicroVMs |
| [`demo/aws/rounds.json`](demo/aws/rounds.json), [`model.py`](demo/aws/model.py) | The four rounds' options and the model that scores them |
| [`demo/`](demo/) | The shop, the referee, the simulators and the AWS templates |
| [`assets/`](assets/) | Concept art (ChatGPT-generated, illustrative only) |

The talk deck, with Arran Peterson's slides on each Lambda, is published at
<https://aws-ug-2026-11-25.talk.grumpydev.org/talk/>.

## Try it

Score five shop designs on the same customer trace, with no AWS account and no
Docker:

```sh
cd demo
uv run --no-project python virtual_rds.py compare
```

Check that every option of the four-round model is worth picking (4,000 random
worlds, a few seconds):

```sh
python3 demo/aws/model.py balance
```

Run the tests:

```sh
cd demo
uv run --no-project --with pytest --with boto3==1.44.9 --with pydantic==2.13.4 pytest -q
```

The local arena (Docker), the AWS game and rehearsal, and the Lambda fan-out are in
[`docs/processes/`](docs/processes/). The AWS scripts only run against the
account named in `BLACK_FRIDAY_AWS_ACCOUNT`, in Sydney (`ap-southeast-2`), and
delete what they create; read the process before running one.

## Licence

Code and documentation: [MIT](LICENSE). Artwork in `assets/`: CC BY 4.0
([details](assets/README.md)).
