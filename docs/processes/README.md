# Processes

Runbooks for the BLACK FRIDAY demo. Commands run from the repository root unless
a file says otherwise; code is in [`demo/`](../../demo/). Background:
[game.md](../game.md) and [design.md](../design.md).

| Runbook | What it covers | Needs AWS |
|:--|:--|:--|
| [local-play.md](local-play.md) | The local two-migration demo and the Virtual RDS scoring round | No |
| [aws-rehearsal.md](aws-rehearsal.md) | Build, run and tear down the Sydney rehearsal | Yes |
| [launch-wave.md](launch-wave.md) | Fan out one game-release wave across Event Lambda workers | Yes |
| [git-submissions.md](git-submissions.md) | Submit a release with Git; run the intake receiver | No |
| [microvm-submission.md](microvm-submission.md) | Run one reviewed frozen commit in a MicroVM; stock AWS Emulator proofs | Yes |
| [local-aws-simulator.md](local-aws-simulator.md) | Drive the local virtual AWS simulator with the stock AWS CLI | No |
| [running-outside-code.md](running-outside-code.md) | The safety boundary: what exists, what a hosted runner needs | n/a |

## AWS prerequisites

Every AWS runbook assumes:

```sh
export BLACK_FRIDAY_AWS_ACCOUNT=<12-digit account id>   # the scripts refuse to run in any other account
export BLACK_FRIDAY_AWS_PROFILE=<AWS CLI profile>       # an SSO or other profile for that account
aws sso login --profile "$BLACK_FRIDAY_AWS_PROFILE"
```

The Region is fixed at Sydney (`ap-southeast-2`). Use a sandbox account you are
content to run rehearsal stacks in. Measurements recorded in these runbooks were
taken in Oregon (`us-west-2`) before the move on 23 November 2026.
