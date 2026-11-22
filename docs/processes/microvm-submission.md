# MicroVM submission

Run one frozen Git commit in a real Lambda MicroVM. This is a manual proof for a
**reviewed fixture only**, not a contestant runner. See
[running-outside-code.md](running-outside-code.md) for the safety boundary.

Prerequisite: a frozen export from [git-submissions.md](git-submissions.md), and
the account and profile from [AWS prerequisites](README.md#aws-prerequisites).
The [AWS rehearsal](aws-rehearsal.md) stack must already be deployed: this runner
reuses its build role and private bucket and does not redeploy it.

## Run the reviewed Git fixture in Lambda

In the fixture repository, put the organiser's
[reviewed PHP file](../../demo/aws/submission.php) at the root as
`submission.php`, unchanged. Push to the submission branch and export it as in
the Git runbook. On the AWS operator machine:

```sh
cd demo
uv run aws/submission_vm.py /srv/black-friday/round-1-demo.json /srv/black-friday/round-1-demo.tar.gz
```

The runner verifies the archive checksum and matches the PHP bytes against the
reviewed copy before contacting AWS. It packages only that file and the
organiser-owned Dockerfile. With `--package-only image.zip`, it stops there
without contacting AWS. The live command builds a separate temporary image,
checks its identity and absence of an execution role, and verifies a
quantity-dependent Cloud Mug quote from the PHP endpoint. It terminates the
MicroVM and removes the temporary image and S3 object version.

**Observed:** the first live run in Oregon took about three minutes end to end,
including image build and cleanup. A reviewed PHP fixture at a frozen commit ran
in a real Oregon MicroVM and earned 82 sales × $25 = $2,050. Run it before the
talk and show the result on stage rather than waiting for a fresh image build.

The MicroVM has public network egress but receives no execution role, GitHub
token, or payment capability.

## Launch that fixture as virtual EC2 (stock AWS Emulator)

Build the pinned AWS Emulator image first (next section), then run the AWS CLI
proof against the frozen fixture:

```sh
cd demo
uv run aws/pseudo_ec2.py /srv/black-friday/round-1-demo.json \
  /srv/black-friday/round-1-demo.tar.gz
```

Options: `--emulator-image` (default is the pinned image), `--serve`, and
`--port` (loopback port; 0 chooses a free one).

The organiser prepares the approved Lambda image, registers a custom AMI in
stock AWS Emulator, and binds that AMI to the approved image version. The local
controller accepts only that AMI, `t4g.micro`, and one instance per session.
`RunInstances` creates a real **emulator** instance record, starts a roleless
Lambda MicroVM, waits for real app readiness, and registers the virtual target.
The local bridge, not Emulator's native ALB data plane, carries quote traffic.
`TerminateInstances` disables routing first and waits for both lifecycles to
terminate. Before launch and after termination the bridge returns `503`.

For a manual run, add `--serve --port 8783` to the command. Wait for the ready
line, then run these from another terminal:

```sh
pseudo() {
  AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_EC2_METADATA_DISABLED=true aws --no-sign-request --no-cli-pager \
    --region us-west-2 --endpoint-url http://127.0.0.1:8783 --cli-read-timeout 125 ec2 "$@"
}
ami=$(pseudo describe-images --query 'Images[0].ImageId' --output text)
instance=$(pseudo run-instances --image-id "$ami" --instance-type t4g.micro --count 1 \
  --client-token reviewed-fixture --query 'Instances[0].InstanceId' --output text)
curl -fsS 'http://127.0.0.1:8783/quote?quantity=3'
pseudo terminate-instances --instance-ids "$instance"
curl -sS -w '\nHTTP %{http_code}\n' 'http://127.0.0.1:8783/quote?quantity=3'
```

Close the server with Ctrl-C to remove the temporary Lambda image, S3 object
version, and stock Emulator container. The MicroVM has a 120-second maximum
lifetime. Keep the unsigned endpoint on the trusted presenter's loopback
interface. This is one reviewed fixture, not a general EC2 API, a hosted
contestant service, or permission to execute untrusted submissions.

## Try stock AWS Emulator with a real MicroVM

This proof is separate from the [local simulator](local-aws-simulator.md) and the
Oregon presenter. It needs Docker, `uv`, and an active AWS session for the
account and profile above:

```sh
cd demo
docker build -f aws/Emulator.Dockerfile -t black-friday-aws-emulator:1.0.556 .
uv run aws/emulator_shop.py
```

`emulator_shop.py` takes `--image` to name a different local Docker image. The
Dockerfile pins the official arm64 package; on x86-64 add
`--build-arg RPM_ARCH=x86_64`. The script starts AWS Emulator on a private
loopback port with generated test credentials and telemetry off. Its EC2 and
ELBv2 APIs create a virtual instance, ALB, listener and target group. A local
bridge uses the emulator's target registration to decide whether to forward shop
HTTP requests. The emulator's native ALB listener does **not** forward to AWS;
the bridge takes that hop using the real MicroVM's port-scoped token.

The script uses the already deployed Oregon image for the PHP/SQLite Launch shop
in one roleless MicroVM. It makes the checkout lose its first payment reply,
then retries with the same key. It requires one write-once S3 object, the paid
order and saved cart to survive a PHP restart, and HTTP 503 after
deregistration. It observes the real MicroVM and virtual instance terminate,
then stops its emulator container. The test receipt expires under the existing
S3 lifecycle.

Limits: no emulator fork or stack redeployment. This proof does not cover
virtual RDS, private VPC egress, state across MicroVM replacement, or a hosted
emulator. The deployed image's PostgreSQL `initdb` hit its 12-second subprocess
limit during the hybrid probe; the same seed worked in Docker. The hybrid proof
uses the existing SQLite Launch path. One earlier Launch attempt returned HTTP
500 during VM setup; the successful run does not establish stage reliability.
