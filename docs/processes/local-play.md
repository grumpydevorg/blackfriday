# Local play

Two offline ways to play. Neither needs AWS credentials.

## Run the local demo

From `demo/`, with Docker Compose:

```sh
cd demo
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8766/> and advance three times:

1. **Run Launch shop.** PHP/SQLite sells a mug and saves Maya's next cart. Open
   **Checkpoint and payment evidence** to show a deliberately lost payment
   response, the retry, and one verified charge and order. The PHP process stops.
   For a visible restart, run `docker compose restart arena` and reload. The
   rehearsal ID and saved checkpoint remain the same.
2. **Deploy both migrations.** Each reads the same SQLite file into a separate
   PostgreSQL schema. Both report healthy and return Maya's earlier order.
   Pause here before revealing the cart result.
3. **Send the shopper back.** Release A preserved the cart: Maya checks out.
   Release B skipped the carts table: she leaves. The same 42% retention draw
   gives different next-week outcomes. With $2 of modelled Growth cost each,
   the two releases earn **+$8** and **−$2** in Growth, respectively.

Use **New rehearsal** after the comparison to repeat with a fresh shop.
`docker compose down` stops the stack without deleting saved state. Set
`BLACK_FRIDAY_PORT` before starting if port 8766 is in use. The first image build
needs internet access; once the images are built, `docker compose up -d --wait`
runs locally without fetching dependencies.

The time and database operations are real; the costs and retention draw are
game inputs. This is a local two-round rehearsal, not a MicroVM deployment or
the proposed Git-submitted four-round tournament.

## Play the Virtual RDS scoring round

From `demo/`, using Python 3.11 or newer:

```sh
cd demo
python3 virtual_rds.py compare
python3 virtual_rds.py run --storage small --replicas 1 --cache --checkpoint round.json
python3 virtual_rds.py resume round.json
```

`compare` forks the same seed (`--seed`, 42 by default) into local storage,
small shared storage, two shop replicas on that same small tier, large shared
storage, and a small tier with an app-side cache. `run` scores one chosen
architecture (`--storage` from the listed tiers, `--replicas` 1 or 2, `--cache`).
The round sends 80 reads and 20 checkout attempts to real loopback HTTP servers.
It sends them serially in a seeded order but assigns them the same modelled
arrival time. This is a capacity model, not a concurrent benchmark: a read costs
one unit, a conditional checkout commit costs two, and an operation that can't
complete within one simulated second is rejected without changing the store.
The same warmup and read trace are used for every architecture.

Small and large shared tiers handle 80 and 160 units/s and cost $1 and $4 per
modelled round, respectively. Instance-local storage has its own 160-unit/s
budget per replica and no separate storage charge. Each shop replica costs $1.
A verified sale contributes $10. All prices are game inputs; the CLI reports
simulated storage delay and measured HTTP latency separately.

After the wave the store commits one additional order but loses the reply. The
driver replaces the shop fleet, reads that order through a fresh shop, and
retries twice with the same idempotency key. The referee checks the record
and payment independently before crediting any sale; instance-local orders are
lost when their shops are replaced. Without `--checkpoint`, no app data is
saved. With it, `resume` loads the app records into a fresh store and reads
the retry order again; this does not resume a live process or payment ledger.

No PostgreSQL, Docker, AWS credentials or cloud services are required. This is
one controlled scoring round with a trusted reference shop, not a SQL benchmark,
a hosted contestant runner, or the proposed Git-submitted four-round tournament.
