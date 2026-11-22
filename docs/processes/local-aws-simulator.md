# Local virtual AWS simulator

This is separate from the [AWS rehearsal](aws-rehearsal.md) and the offline
[two-migration demo](local-play.md). Start the simulator and use the stock AWS
CLI against its loopback endpoint. No AWS credentials or account are used. The
first image build may fetch dependencies.

From `demo/`:

```sh
cd demo
docker compose -f compose.simulator.yaml up --build -d --wait
sim() { aws --endpoint-url http://127.0.0.1:8780 --region us-west-2 --no-sign-request --no-cli-pager "$@"; }
sim rds create-db-instance --db-instance-identifier shop --db-instance-class db.t4g.micro --engine postgres
db=$(sim rds describe-db-instances --db-instance-identifier shop \
  --query 'DBInstances[0].DBName' --output text)
docker compose -f compose.simulator.yaml exec -T postgres \
  psql -h postgres -p 5432 -U demo -d "$db" -c 'SELECT id, name FROM customers;'

launch_shop() {
  sim ec2 run-instances --image-id ami-00000000000000001 --instance-type t4g.micro --count 1 \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=BlackFridayDatabase,Value=shop}]' \
    --query 'Instances[0].InstanceId' --output text
}
first=$(launch_shop)
alb=$(sim elbv2 create-load-balancer --name front --query 'LoadBalancers[0].LoadBalancerArn' --output text)
tg=$(sim elbv2 create-target-group --name shop --protocol HTTP --port 80 --health-check-path /health \
  --query 'TargetGroups[0].TargetGroupArn' --output text)
sim elbv2 create-listener --load-balancer-arn "$alb" --protocol HTTP --port 80 \
  --default-actions "Type=forward,TargetGroupArn=$tg"
sim elbv2 register-targets --target-group-arn "$tg" --targets "Id=$first"
sim elbv2 describe-target-health --target-group-arn "$tg"

shop=http://127.0.0.1:8781
curl -fsS -H 'X-Customer-ID: maya' -H 'Content-Type: application/json' \
  --data '{"cart_id":"cart-saved","sku":"cloud-mug"}' "$shop/cart"
curl -fsS -H 'X-Customer-ID: maya' -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: growth-mug' --data '{"cart_id":"cart-saved"}' "$shop/checkout"

sim ec2 terminate-instances --instance-ids "$first"
curl -sS -i "$shop/health" # HTTP 503: no healthy target
second=$(launch_shop)
sim elbv2 register-targets --target-group-arn "$tg" --targets "Id=$second"
sim elbv2 deregister-targets --target-group-arn "$tg" --targets "Id=$first"
curl -fsS -i -H 'X-Customer-ID: maya' "$shop/cart/cart-saved"
curl -fsS -i -H 'X-Customer-ID: maya' "$shop/orders/order-growth-mug"

sim ec2 terminate-instances --instance-ids "$second"
sim rds delete-db-instance --db-instance-identifier shop --skip-final-snapshot
```

## What it does

The ALB actually probes `/health` and round-robins across healthy PHP processes.
`X-Black-Friday-Instance` identifies the target that answered. `CreateDBInstance`
allocates and seeds a separate database on the local PostgreSQL server.
`DescribeDBInstances` reports its native SQL endpoint (`postgres:5432` on the
Compose network), database name, and user. The trusted demo network uses
passwordless PostgreSQL access and does not publish port 5432 to the host.
Virtual shops share the selected database across replacements. Omit
`BlackFridayDatabase` for a shop with private SQLite; terminating that instance
discards its data. The separate payment ledger uses the fixed Maya scenario above.

## Limits

The simulator models one HTTP listener, up to four running `t4g.micro` shops, and
up to four `db.t4g.micro` databases on one PostgreSQL server. Deletion requires
`--skip-final-snapshot`, refuses while a shop uses the database, and drops the
database before returning. No snapshots, backups, or database capacity modelling.
The supported Query operations are those shown above plus `ec2 describe-instances`.
There is no VPC, IAM, SQS, or Lambda emulation. Virtual EC2 runs trusted PHP
subprocesses in one local container, not MicroVMs or a multi-tenant sandbox.
Don't expose the control or HTTP ports beyond loopback. It does not implement the
isolation, quotas or billing described for the competition in the
[design specification](../design.md).

## Reset

Run `docker compose -f compose.simulator.yaml down`, then the `up` command above.
`down` discards the PostgreSQL server's `tmpfs` data. Don't restart only
`simulator`: its in-memory RDS registry is lost while the database server keeps
the old databases, leaving them unreachable through the RDS API until the full
reset. Nothing is provisioned or billed on AWS. Use `BLACK_FRIDAY_SIM_API_PORT`
and `BLACK_FRIDAY_SIM_SHOP_PORT` to change the published ports and update the two
URLs accordingly.
