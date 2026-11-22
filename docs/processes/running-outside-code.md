# Running outside code: the safety boundary

Nothing in this repository runs code from the audience or any other outside
party. This page states what exists, what a hosted runner would need, and what is
not claimed.

## What exists

- Trusted rehearsal code runs in disposable Lambda MicroVMs. The image has no
  execution role but does have internet egress for a short-lived, object-scoped
  payment PUT ([AWS rehearsal](aws-rehearsal.md)).
- Git intake queues the exact pushed SHA, pins and hashes the archive, and
  exports frozen bytes. It never extracts, builds, executes or scores
  ([Git submissions](git-submissions.md)).
- One **reviewed** PHP fixture, matched byte for byte against an
  organiser-owned copy, ran in a real Oregon MicroVM with no execution role,
  GitHub token or payment capability ([MicroVM submission](microvm-submission.md)).
- The local simulator and stock-Emulator proofs run trusted PHP only.
- Caps on the rehearsal: 120-second MicroVM lifetime, evaluator reserved
  concurrency 4, 48-hour Durable ceiling, no EC2, RDS or NAT.

## What a hosted runner would need (not built)

1. A **separate AWS account** for contestant builds and runs, with no access to
   organiser data, presenter credentials or other teams.
2. **Isolated build and run MicroVMs**, separate for building the artifact and
   for serving traffic.
3. **Externally enforced egress controls.** The current MicroVM has public
   network egress; a VM boundary does not restrict where it connects.
4. **Independent scoring.** The referee, payment ledger and retention state sit
   outside anything a contestant controls, so a team cannot write its own score.
5. Per-team quotas, lifecycle limits and a way to reset or destroy an
   environment without touching the referee ledger.

## What is not claimed

- No audience or outside code has been run.
- The MicroVM boundary alone is not treated as sufficient isolation.
- No contestant runner, hosted emulator or tournament is deployed.
- No claim about concurrent players, production scale, or Managed Instances
  savings.
