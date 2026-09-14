# OpenTelemetry Demo → ClickStack on AWS EKS

A standing instance of the OpenTelemetry demo application on an AWS EKS cluster,
shipping traces, logs, metrics and session replay to a managed ClickStack
service on ClickHouse Cloud. It is the cloud counterpart of the laptop workshop
in `../202609-offsite-workshop/observability-workshop/`: same ClickHouse service, same collector wiring,
same Helm values and feature-flag exercise, but reachable by anyone with the AWS
profile rather than tied to one Mac. Between demos the node group scales to
zero and only the EKS control plane is billed; `./demo.sh up` brings the whole
thing back in minutes.

Run `./demo.sh` with no subcommand to list what it can do.

## Architecture

```
 your Mac
 ├── ./demo.sh …            aws / tofu / kubectl / helm, authenticated with an
 │                          IAM Identity Center (SSO) profile
 └── kubectl port-forward   http://localhost:8080  ->  svc/frontend-proxy:8080
         │
         │  EKS public API endpoint (IAM-authenticated, TLS)
         ▼
 AWS us-east-1  ── default VPC 172.31.0.0/16, two default PUBLIC subnets, no NAT gateway
 │                (dedicated 10.20.0.0/16 VPC optional: use_default_vpc = false)
 │
 │  EKS control plane "otel-demo-eks" (Kubernetes 1.36, always on)
 │  managed node group "demo": 0 nodes idle, 2 × m7g.xlarge (Graviton) when up
 │
 │   ns otel-demo      opentelemetry-demo Helm chart 0.41.0 (~30 pods)
 │                       frontend  <- our image from ECR (ClickStack browser SDK)
 │                       gateway collector (DaemonSet)
 │                             │ OTLP/HTTP + `authorization: <OTLP_AUTH_TOKEN>`
 │   ns clickstack     clickstack-otel-collector 2.38.0
 │                             │ ClickHouse HTTPS interface (TLS, port 8443)
 └─────────────────────────────┴──> ClickHouse Cloud, database `otel`

 EventBridge Scheduler "otel-demo-eks-nightly-scale-down"
   every night at scale_down_hour: eks:UpdateNodegroupConfig desiredSize=0
```

Nothing in the cluster is reachable from the internet. The storefront, the
feature-flag UI and the load generator are served to your browser through the
port-forward; the only inbound path to the cluster is the EKS API endpoint,
which authenticates every request with IAM. The demo's bundled backends
(Jaeger, Prometheus, Grafana, OpenSearch) are turned off; ClickStack replaces
all four.

### Design decisions

| Decision | Why |
| --- | --- |
| **Private access, `kubectl port-forward` only** (no LoadBalancer, Ingress or NodePort) | Anyone with the AWS profile can open the tunnel, nobody else can reach the storefront at all, and there is no load balancer or public endpoint to secure or pay for. |
| **Public subnets, no NAT gateway** | ClickHouse Cloud accepts connections from any IP and nothing inbound reaches the nodes, so a NAT gateway (hourly plus per-GB) would be pure cost; nodes get public IPs (`map_public_ip_on_launch`) and egress directly through the internet gateway. |
| **Run in the account default VPC** (`use_default_vpc = true`) | The account's us-east-1 VPC quota is used up by other teams' VPCs, so the cluster uses the default VPC's public subnets instead of creating its own; that also means no NAT gateway to add and nothing network-side to delete on teardown. OpenTofu only *reads* the default VPC and subnets (data sources, no `aws_default_*` resources), so it can never modify or destroy them. Set `use_default_vpc = false` in an account with headroom for a dedicated 10.20.0.0/16 VPC. |
| **Graviton (arm64) nodes** | The session-replay frontend image is built on the Mac with `docker buildx`, and `linux/arm64` builds natively on Apple Silicon (no QEMU emulation); m7g is also cheaper than the x86 equivalent. |
| **Scale to zero instead of destroy** | The EKS control plane is the slow part of `apply`, so keeping it (about $73/month) turns "stand the demo up" into scaling a node group rather than rebuilding a cluster; `destroy` is still there for when the demo is really over. |
| **OpenTofu, remote state in S3** | The control plane is long-lived and costs money; state on one laptop could orphan it, and a second presenter could not `up`/`down` without it. The bucket is versioned so a clobbered state file can be restored. |
| **Nightly scale-to-zero via EventBridge Scheduler** | A forgotten demo costs at most one day of nodes. The schedule calls `eks:UpdateNodegroupConfig` directly (universal target), so there is no Lambda to maintain. |
| **Cost trims in the EKS module** | No control-plane CloudWatch logs, no customer-managed KMS key (`create_kms_key = false` together with `encryption_config = null`, which the module needs to skip the encryption block entirely; EKS still encrypts secrets with an AWS-owned key), no IRSA OIDC provider, no EBS CSI (nothing needs a PersistentVolume with the backends off). |

### Pinned versions

| Component | Version | Where |
| --- | --- | --- |
| Demo Helm chart `open-telemetry/opentelemetry-demo` | 0.41.0 | `CHART_VERSION` in `scripts/lib/common.sh` (read by `deploy` and `check.sh`) |
| Demo source (frontend image build) | commit `d6fd782e` | `DEMO_REF` in `scripts/lib/common.sh` |
| EKS Kubernetes | 1.36 | `kubernetes_version` in `tofu/variables.tf` |
| ClickStack collector `clickhouse/clickstack-otel-collector` | 2.38.0 | `k8s/clickstack-collector.yaml` |
| `terraform-aws-modules/eks/aws` | ~> 21.25 (needs `hashicorp/aws` >= 6.59) | `tofu/eks.tf`, `tofu/versions.tf`, `tofu/.terraform.lock.hcl` |
| `terraform-aws-modules/vpc/aws` | ~> 6.7 | `tofu/vpc.tf` |

The chart and the demo commit are pinned together: the session-replay patch is
generated against `d6fd782e`, and a chart bump can move the frontend image the
patch expects. Bump either, then re-check `git apply --check` (see
[Session replay](#session-replay)).

## Prerequisites

On the Mac:

| Tool | Notes |
| --- | --- |
| `aws` CLI v2 | With an IAM Identity Center profile: `aws configure sso --profile <name>`. The permission set needs to create EC2, EKS, ECR, IAM roles, S3 and EventBridge Scheduler resources, plus VPC when `use_default_vpc = false`; an administrator-style set is the practical answer. |
| OpenTofu >= 1.10 | `brew install opentofu`. `tofu` is the only IaC binary used; terraform and eksctl are not needed. |
| Docker with buildx | Docker Desktop or equivalent. `build-frontend` runs on the Mac and pushes to ECR; on Apple Silicon the `linux/arm64` build is native. |
| `kubectl` | Within one minor version of the cluster (1.36). |
| `helm` | Helm 4 works (the scripts know about its stricter `--wait`); Helm 3 does too. |
| `jq`, `curl`, `git`, `lsof` | `jq` is used for everything JSON; no Python is required. |

`brew install kubectl helm` covers the two most often missing; `./demo.sh init`
checks for all of them and names whatever is absent.

Two local files, both gitignored, both copied from their `.example`:

```sh
cp envvars.aws.example envvars.aws                 # AWS_PROFILE=<your sso profile>, AWS_REGION=us-east-1
cp envvars.clickhouse.example envvars.clickhouse   # the five ClickHouse / ClickStack values
```

- `envvars.aws` holds `AWS_PROFILE` and `AWS_REGION`. Every script sources it,
  exports both, and runs `aws sso login` for you when the session has expired
  (a browser opens). Alternatively export `AWS_PROFILE` in your shell; the file
  is optional if you do. Note that `init` needs `AWS_PROFILE` before it can do
  anything, so it can only *warn* about a missing `envvars.aws` when the
  variable came from your shell; otherwise it stops and tells you to create
  the file.
- `envvars.clickhouse` holds `CLICKHOUSE_ENDPOINT`, `CLICKHOUSE_USER`,
  `CLICKHOUSE_PASSWORD`, `HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE` and
  `OTLP_AUTH_TOKEN`. **Plain `KEY=VALUE` lines, no quotes, no `export`, no other
  keys**: `deploy` feeds the file verbatim to
  `kubectl create secret --from-env-file`, so quotes would become part of the
  values and an extra line would become an extra Secret key. The same file is
  also sourced by `deploy` and `verify`, which is why it must be valid shell too.

On the ClickHouse side you need a ClickHouse Cloud service with ClickStack, a
database (`otel`) and a user the collector can write with. The service the
laptop workshop uses works as-is; for a new one, run `sql/create-user.sql` in
the SQL console with a real password in place of `SECURE_PASSWORD`. The
collector creates the tables. `OTLP_AUTH_TOKEN` is any long random string: the
ClickStack collector requires it on every inbound OTLP request and the demo's
gateway collector sends it, so it never leaves the cluster.

## First-time runbook

Four commands, in order. Only the first two ask you anything.

```sh
./demo.sh init            # tools, SSO login, state bucket, tofu init, helm repo
./demo.sh apply           # EKS + node group + ECR + schedule in the default VPC; asks first
./demo.sh build-frontend  # build the session-replay frontend image on the Mac, push to ECR
./demo.sh deploy          # collector + demo chart + tunnel; prints http://localhost:8080
```

- `init` is idempotent and safe to re-run. It creates the state bucket
  `otel-demo-eks-tfstate-<account-id>` only if it does not exist (versioned,
  public access blocked), then runs `tofu init` against it. The bucket, key and
  region are passed as `-backend-config` flags, so nothing account-specific is
  in `tofu/backend.tf`.
- `apply` shows the plan and waits for `yes` (`--yes` skips the prompt). The
  EKS control plane is the slow part. It brings the node group up at
  `node_count` so the add-ons can reach `ACTIVE`, writes your kubeconfig
  (context `otel-demo-eks`), prints `tofu output` and the next steps. Copy
  `tofu/terraform.tfvars.example` to `tofu/terraform.tfvars` first if you want
  a different `scale_down_hour`/`scale_down_timezone`, instance type, node
  count, API CIDR allow-list, extra cluster admins, or a dedicated VPC instead
  of the account default one (`use_default_vpc = false`; decide before the
  first apply, since a cluster cannot move between VPCs and changing it later
  means `./demo.sh destroy` and a fresh apply). When you know your egress IP
  (`curl -s https://checkip.amazonaws.com`), set `endpoint_public_access_cidrs`
  to that `/32` in `tofu/terraform.tfvars`: the default admits any source
  address, IAM-authenticated. EKS control-plane logging is off
  (`enabled_log_types = []` in `tofu/eks.tf`, to avoid CloudWatch Logs
  charges); listing `"audit"` (and optionally `"api"`, `"authenticator"`) there
  turns it on, and setting `create_cloudwatch_log_group = true` beside it lets
  the module own the log group and its retention instead of EKS creating one
  that never expires.
- `build-frontend` clones the demo source at the pinned commit (blobless, into
  the gitignored `opentelemetry-demo/`), applies
  `patches/frontend-session-replay.patch`, logs Docker in to ECR and runs
  `docker buildx build --platform linux/arm64 … --push`. The first build
  downloads the npm tree and is slow; buildx caches it afterwards. `deploy`
  runs this step for you when the image is missing, so the explicit call is
  only there to keep the slow part out of `deploy`.
- `deploy` creates the two namespaces, the `clickstack-credentials` Secret
  (from `envvars.clickhouse`, through a pipe, never a rendered file on disk),
  the ClickStack collector, the `clickstack-otlp-token` Secret, then
  `helm upgrade --install` of the demo with the ECR image, and finally opens
  the tunnel. It refuses to run against zero nodes.

Then browse `http://localhost:8080` for a minute and confirm rows are landing:

```sh
./demo.sh verify
```

## Everyday cycle

```sh
./demo.sh up                    # scale the nodes back up, wait, deploy, open the tunnel
./demo.sh tunnel                # re-open the tunnel if it died (laptop sleep, SSO expiry)
./demo.sh flag paymentFailure 50%   # inject a fault, live in ~2 s
./demo.sh verify                # rows in the last 15 minutes, per-service spans, replay sessions
./demo.sh flag --reset          # restore every flag
./demo.sh down                  # uninstall, scale to 0; ClickHouse data is untouched
```

`up` is `ng_scale node_count` + wait for Ready nodes and CoreDNS + `deploy`.
`down` stops the tunnel, `helm uninstall`s the demo, deletes both namespaces
and scales the node group to 0; `down --keep` skips the uninstall so the
workloads stay in the API and reschedule when the nodes return. Either way the
control plane, the ECR image and the state stay, and telemetry already in
ClickHouse Cloud is never touched.

The tunnel is a `kubectl port-forward` to `svc/frontend-proxy` inside a small
restart loop (`.run/port-forward.pid`, `.run/port-forward.log`) that re-runs
`aws sso login` when the session has expired and relaunches the forward when
the pod or the API connection drops. `deploy` and `up` open it for you and
print the URLs even if the storefront has not answered within 20 seconds (the
loop keeps trying; `./demo.sh tunnel status` tells you when it is up). Once it
is:

| URL | What |
| --- | --- |
| `http://localhost:8080` | The Astronomy Shop storefront (session replay is recording) |
| `http://localhost:8080/feature/` | flagd UI, the same state `./demo.sh flag` edits |
| `http://localhost:8080/loadgen/` | Locust load generator; it runs continuously, so telemetry flows without clicking |

`./demo.sh status` shows where things stand in either state (identity, node
group size, nodes, pods by phase, Helm release, tunnel, whether ECR holds the
current frontend tag, and what the nightly schedule will do).

## Subcommands

`./demo.sh <name> [args]` runs `scripts/<name>.sh`; every script can also be run
directly. All of them log in to the SSO profile first, so any of them works
from a fresh shell.

| Subcommand | Purpose | Flags |
| --- | --- | --- |
| `init` | One-time host and account setup: check tools, SSO login, create the versioned state bucket, `tofu init` with the S3 backend, add the `open-telemetry` Helm repo, warn about missing `envvars.*`. Idempotent. | |
| `apply` | `tofu apply`: EKS cluster, node group, ECR repository, nightly schedule, in the account default VPC (or a dedicated VPC with `use_default_vpc = false`). Then kubeconfig, `kubectl get nodes`, `tofu output`. | `--yes` apply without the confirmation prompt |
| `build-frontend` | Clone the demo at `d6fd782e`, apply the session-replay patch, `docker buildx build --push` the frontend to ECR under the deterministic tag. Skips the build if the tag is already in ECR. | `--force` build and push even if the tag exists |
| `deploy` | Namespaces, Secrets from `envvars.clickhouse`, ClickStack collector, `helm upgrade --install` of the demo with the ECR image, then open the tunnel. Builds the image if ECR lacks it. Refuses on zero nodes. | |
| `up` | Scale the node group to `node_count`, wait for Ready nodes and CoreDNS, then `deploy`. | |
| `down` | Stop the tunnel, uninstall the demo and collector, scale the node group to 0. Control plane, image and state stay. | `--keep` scale to 0 but leave the workloads in the API |
| `tunnel` | (Re)start the local port-forward to `http://localhost:8080`, stop it, or report whether it is up. | `stop`, `status` (exit 0 when up, 1 otherwise); no argument restarts |
| `flag` | List, inspect or set flagd feature flags without a restart, or reset them all. | `<flag>`, `<flag> <variant>`, `--reset` |
| `verify` | Query ClickHouse Cloud for rows in the last 15 minutes (traces, logs, metrics, session replay) and show the collector pods and logs. | |
| `status` | Identity, node group size and status, nodes, pods by phase, Helm release, tunnel, ECR image, nightly schedule. Exits 0 whether idle or up. | |
| `nightly` | Enable or disable the nightly scale-to-zero schedule without a `tofu apply`. | `on`, `off` |
| `destroy` | Full teardown: `down`, then `tofu destroy` of everything. Optionally delete the state bucket. | `--purge-state` empty and delete the state bucket after the destroy succeeds |

`scripts/check.sh` is not a subcommand: it is the repo's offline lint/validate
step (`bash -n` on every script, shellcheck when installed, `tofu fmt`/`init
-backend=false`/`validate`, a parse of the collector manifest and a
`helm template` of the chart with the committed values). It needs no AWS
credentials or cluster; run it after editing anything.

## Repository layout

| Path | Purpose |
| --- | --- |
| `demo.sh` | Dispatcher: `./demo.sh <name>` → `scripts/<name>.sh`. |
| `scripts/*.sh` | One script per subcommand (table above). |
| `scripts/lib/common.sh` | `log`/`die`/`need`, the pinned constants, `load_clickhouse_env`, `frontend_tag`. |
| `scripts/lib/aws.sh` | `aws_login` (SSO), `tf_out` (OpenTofu outputs), node-group scaling. |
| `scripts/lib/k8s.sh` | `kubeconfig`, `wait_nodes_ready`, the tunnel loop. |
| `scripts/check.sh` | Offline verification (see above). |
| `tofu/` | OpenTofu: network (`vpc.tf`: default-VPC lookup, or the dedicated-VPC module behind `use_default_vpc = false`), EKS + node group (`eks.tf`), ECR (`ecr.tf`), nightly schedule (`scheduler.tf`), S3 backend with no account values (`backend.tf`), variables, outputs, `terraform.tfvars.example`, committed lock file. |
| `k8s/demo-values.yaml` | Static Helm values for the demo chart: backends off, gateway collector → ClickStack with the token from a Secret, frontend env for session replay. The ECR image arrives via `--set`. |
| `k8s/clickstack-collector.yaml` | Namespace, Deployment and Service for the ClickStack collector; credentials come from the Secret `deploy` creates. |
| `patches/frontend-session-replay.patch` | Adds the ClickStack browser SDK to the demo frontend (identical to the workshop's). |
| `sql/create-user.sql` | Creates the `clickstack` ClickHouse user with grants on `otel`. |
| `envvars.*.example` | Templates for the two gitignored local files. |
| `.run/` | Tunnel pidfile and log (gitignored). |
| `opentelemetry-demo/` | Demo source checkout made by `build-frontend` (gitignored). |

## Costs

Everything is on-demand in us-east-1; list prices at the time of writing.

| State | What is billed | About |
| --- | --- | --- |
| **Idle** (node group at 0) | EKS control plane at $0.10/hour | **~$73/month** |
| **Up** (2 × m7g.xlarge) | Control plane $0.10/hour + two nodes at about $0.33/hour | **~$0.43/hour**, so roughly $10/day if forgotten |
| Always | S3 state, ECR image storage (lifecycle policy keeps the 5 most recent), the two nodes' public IPv4 addresses and 50 GiB gp3 root volumes while up | Cents |

There is no NAT gateway, no load balancer, no CloudWatch control-plane logs and
no KMS key, by design.

The nightly schedule bounds the "up" cost: EventBridge Scheduler scales the node
group to 0 every night at `scale_down_hour` in `scale_down_timezone` (defaults
23:00 `America/New_York`), so a demo nobody took down costs at most one day of
nodes. Two ways to change that:

- **For one evening**: `./demo.sh nightly off` before a demo that runs past the
  hour, `./demo.sh nightly on` afterwards. This flips the schedule's state in
  AWS without touching OpenTofu; the next `./demo.sh apply` puts it back to
  whatever `nightly_scale_down_enabled` says (default `true`), which is the
  intended cost-guard behaviour.
- **Permanently**: set `scale_down_hour` and `scale_down_timezone` (and, if you
  really want, `nightly_scale_down_enabled = false`) in `tofu/terraform.tfvars`
  and run `./demo.sh apply`.

Cheaper nodes: `instance_type = "m7g.large"` in `tofu/terraform.tfvars` halves
the node cost; the demo still fits on two of them, with a slower start. If you
go smaller than that, uncomment the `chatbot`/`agent`/`mcp`/`telemetry-docs`
trims in `k8s/demo-values.yaml` (about 1.6 GiB of requests and four pods you may
never show).

When the demo is over for good, `./demo.sh destroy` removes everything
(`--purge-state` also deletes the state bucket). `down` is the nightly state;
`destroy` is the end.

## Feature flags

The demo ships flagd flags that inject faults: failed payments, memory leaks,
slow images, Kafka backpressure. Flipping one and watching the blast radius
appear in ClickStack is the core demo exercise.

```sh
./demo.sh flag                         # list every flag + current variant + valid variants
./demo.sh flag paymentUnreachable      # show one flag
./demo.sh flag paymentUnreachable on   # set it: live in ~2 s, no restart
./demo.sh flag paymentFailure 50%      # some flags take non-boolean variants
./demo.sh flag --reset                 # restart flagd, restore all chart defaults
```

An unknown flag or variant is rejected before anything is written, and the
script reads the file back after two seconds to confirm the write took. The
web UI at `http://localhost:8080/feature/` edits the same state.

**Do not edit `cm/flagd-config` to change a flag.** flagd never reads that
ConfigMap directly: an init container copies it once into an emptyDir shared
with the `flagd-ui` container, and flagd fsnotify-watches that copy. Patching
the ConfigMap changes nothing until the pod restarts, and the restart discards
every live toggle. `./demo.sh flag` writes to the watched copy instead (via
`kubectl exec` into `flagd-ui`, since the `flagd` container is distroless),
which is why its changes take effect without a rollout; `--reset` is that
restart, on purpose.

## Session replay

Session replay is always on. The frontend runs our own image, built from the
demo source at commit `d6fd782e` with `patches/frontend-session-replay.patch`
applied, which adds `@hyperdx/browser` to `src/frontend/package.json` and wires
it into `_app.tsx`, `_document.tsx` and `FrontendTracer.ts`.
`k8s/demo-values.yaml` sets `PUBLIC_HYPERDX_ENABLED=true`, and the SDK posts to
`window.location.origin + /otlp-http`, which through the port-forward is the
demo's own gateway collector.

That image is a prerequisite rather than an option: the upstream `frontend`
image has no browser SDK in its bundle and there is no runtime hook to inject
one, so the SDK has to be in `package.json` and compiled by `next build`.
`PUBLIC_HYPERDX_ENABLED` only activates code that must already be there.

**The tag is deterministic**: `<DEMO_REF>-<first 8 hex digits of sha256(patch)>`,
with the committed patch that is `d6fd782e-c9b5ca3e`. `build-frontend` and `deploy` both compute it
(`frontend_tag` in `scripts/lib/common.sh`), so "is that tag in ECR" is the
whole up-to-date check. No state is needed, a second presenter's `deploy` finds
the image the first one built, and a changed patch produces a new tag that
`deploy` builds automatically. To print it:

```sh
bash -c '. scripts/lib/common.sh && frontend_tag'
```

Replay events land in `otel.hyperdx_sessions`; `./demo.sh verify` counts them
and the distinct `rum.sessionId`s from the last 15 minutes.

### Changing the frontend or the patch

`build-frontend` leaves the patched checkout in `opentelemetry-demo/` (detached
at `d6fd782e`). Edit under `opentelemetry-demo/src/frontend/`, then regenerate
the patch from the working tree and rebuild:

```sh
git -C opentelemetry-demo diff > patches/frontend-session-replay.patch
./demo.sh build-frontend --force
./demo.sh deploy          # rolls the frontend onto the new image
```

`--force` builds and pushes regardless of what ECR holds. You need it whenever
the tag would *not* change (edits to files the patch does not cover, or a
rebuild of the same sources), and it is harmless when the tag does change.
`imageOverride.pullPolicy: Always` in the values makes sure a re-pushed image
under an existing tag is actually pulled; ECR tags are `MUTABLE` for the same
reason.

To reuse a checkout you already have instead of cloning a new one, point
`DEMO_DIR` at it (`DEMO_DIR=../202609-offsite-workshop/observability-workshop/opentelemetry-demo
./demo.sh build-frontend`); a checkout without the SDK wiring gets the patch
applied. To move to a newer demo commit, change `DEMO_REF` in
`scripts/lib/common.sh`, remove `opentelemetry-demo/` and run
`./demo.sh build-frontend`: it clones fresh at the new ref, runs
`git apply --check` on the patch first, and stops with a clear message if the
pin and the patch have drifted apart (fix the patch against the new checkout,
regenerate it as above, and build again).

## Recovery

### The tunnel died

`./demo.sh tunnel` (stop whatever is left, start a new one). If
`http://localhost:8080` answers with a 503 from envoy the tunnel is fine and
the frontend is still starting. If port 8080 is held by something else,
`lsof -iTCP:8080 -sTCP:LISTEN` names it.

### `tf_out` says "run ./demo.sh apply first"

Every script reads the cluster name and friends from OpenTofu outputs in the S3
backend. On a fresh clone or a new laptop that means `./demo.sh init` has not
been run yet (it configures the backend); an expired SSO session gives the same
message with tofu's own reason attached, and `./demo.sh init` or any subcommand
will re-login. If a `tofu apply` was interrupted, re-run `./demo.sh apply`; it
only changes what differs.

### The node group is stuck `UPDATING`

Usually the nightly schedule fired moments ago. `ng_scale` waits for it; if you
are in a hurry, `aws eks wait nodegroup-active --cluster-name otel-demo-eks
--nodegroup-name demo` and try again.

### The state file is lost or corrupted

First try to get it back. `init` turned versioning on, so every previous state
is still in the bucket:

```sh
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
BUCKET=otel-demo-eks-tfstate-$(aws sts get-caller-identity --query Account --output text)
KEY=otel-demo-eks/terraform.tfstate

aws s3api list-object-versions --bucket "$BUCKET" --prefix "$KEY" \
  --query 'Versions[].{VersionId:VersionId,LastModified:LastModified,Size:Size,IsLatest:IsLatest}' --output table
aws s3api copy-object --bucket "$BUCKET" --key "$KEY" \
  --copy-source "$BUCKET/$KEY?versionId=<a good VersionId>"
./demo.sh init && tofu -chdir=tofu plan      # should show no (or only expected) changes
```

If the bucket itself is gone, or no version is usable, the resources are
orphaned and have to be removed by hand before `./demo.sh init && ./demo.sh
apply` can start over. Every resource has a deterministic name and carries the
tag `Project=otel-demo-eks`, so an inventory is one call:

```sh
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=otel-demo-eks \
  --query 'ResourceTagMappingList[].ResourceARN' --output text
```

Then delete in this order (each `delete` is asynchronous; the `wait`s matter).
The block is bash: run `bash` first if your shell is zsh, which does not split
`$SGS` and `$rules` into words.

```sh
CLUSTER=otel-demo-eks

# 1. The nightly schedule and its role
aws scheduler delete-schedule --name otel-demo-eks-nightly-scale-down
aws iam delete-role-policy --role-name otel-demo-eks-scheduler --policy-name scale-demo-nodegroup
aws iam delete-role --role-name otel-demo-eks-scheduler

# 2. Node group, then the cluster (add-ons and access entries go with it)
aws eks delete-nodegroup --cluster-name "$CLUSTER" --nodegroup-name demo
aws eks wait nodegroup-deleted --cluster-name "$CLUSTER" --nodegroup-name demo
aws eks delete-cluster --name "$CLUSTER"
aws eks wait cluster-deleted --name "$CLUSTER"

# 3. The frontend image repository (--force deletes the images with it)
aws ecr delete-repository --force --repository-name otel-demo-frontend

# 4. The IAM roles the EKS module created (name prefixes otel-demo-eks-cluster-
#    and demo-eks-node-group-; check with `aws iam list-role-tags` if in doubt)
for r in $(aws iam list-roles \
    --query "Roles[?starts_with(RoleName,'otel-demo-eks-cluster-') || starts_with(RoleName,'demo-eks-node-group-')].RoleName" \
    --output text); do
  for p in $(aws iam list-attached-role-policies --role-name "$r" --query 'AttachedPolicies[].PolicyArn' --output text); do
    aws iam detach-role-policy --role-name "$r" --policy-arn "$p"
  done
  aws iam delete-role --role-name "$r"
done

# 5. The node group's launch template (name prefix demo-)
for lt in $(aws ec2 describe-launch-templates --filters Name=tag:Project,Values=otel-demo-eks \
    --query 'LaunchTemplates[].LaunchTemplateId' --output text); do
  aws ec2 delete-launch-template --launch-template-id "$lt"
done

# 6. The security groups the EKS module created: the additional cluster SG and
#    the node SG (named otel-demo-eks-*) and the EKS-owned cluster SG (tagged
#    kubernetes.io/cluster/otel-demo-eks). They reference each other, so revoke
#    ingress rules first. With use_default_vpc = true (the default) this is ALL
#    the network cleanup: the demo ran in the account default VPC, which
#    OpenTofu never created. NEVER delete the default VPC, its subnets, its
#    internet gateway, its route table or its default security group; they are
#    shared with everything else in the account.
SGS=$( {
  aws ec2 describe-security-groups --filters "Name=tag-key,Values=kubernetes.io/cluster/$CLUSTER" \
    --query "SecurityGroups[?GroupName!='default'].GroupId" --output text
  aws ec2 describe-security-groups --filters "Name=group-name,Values=$CLUSTER-*" \
    --query "SecurityGroups[?GroupName!='default'].GroupId" --output text
} | tr '\t' '\n' | sort -u)
for sg in $SGS; do
  rules=$(aws ec2 describe-security-group-rules --filters Name=group-id,Values="$sg" \
    --query "SecurityGroupRules[?IsEgress==\`false\`].SecurityGroupRuleId" --output text)
  [ -n "$rules" ] && aws ec2 revoke-security-group-ingress --group-id "$sg" --security-group-rule-ids $rules
done
for sg in $SGS; do aws ec2 delete-security-group --group-id "$sg"; done

# 7. ONLY if the cluster was applied with use_default_vpc = false: the dedicated
#    VPC (tag Name=otel-demo-eks, never the default one) and what the vpc module
#    put in it. The lookup excludes the default VPC explicitly and the `if`
#    stops when nothing matches, so this block cannot fall through to deleting
#    the account default VPC.
VPC_ID=$(aws ec2 describe-vpcs --filters Name=tag:Name,Values=otel-demo-eks Name=is-default,Values=false \
  --query 'Vpcs[0].VpcId' --output text)
if [ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ]; then
  for s in $(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID" --query 'Subnets[].SubnetId' --output text); do
    aws ec2 delete-subnet --subnet-id "$s"
  done
  IGW=$(aws ec2 describe-internet-gateways --filters Name=attachment.vpc-id,Values="$VPC_ID" \
    --query 'InternetGateways[0].InternetGatewayId' --output text)
  aws ec2 detach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC_ID"
  aws ec2 delete-internet-gateway --internet-gateway-id "$IGW"
  for rt in $(aws ec2 describe-route-tables --filters Name=vpc-id,Values="$VPC_ID" \
      --query "RouteTables[?Associations[0].Main!=\`true\`].RouteTableId" --output text); do
    aws ec2 delete-route-table --route-table-id "$rt"
  done
  aws ec2 delete-vpc --vpc-id "$VPC_ID"
else
  echo "no dedicated VPC tagged Name=otel-demo-eks; nothing more to delete (default VPC left alone)"
fi
```

A `DependencyViolation` on a security group means a rule somewhere still
references it (revoke that rule) or an ENI is still attached (EKS removes its
ENIs a few minutes after the cluster is gone; wait and retry). Re-run the
tagging inventory at the end; it should come back empty. The state bucket is
not tagged and is not created by OpenTofu; delete it with
`./demo.sh destroy --purge-state`'s logic by hand if you want it gone
(`aws s3api list-object-versions` → `delete-objects` → `delete-bucket`), or
keep it for the next `./demo.sh init`.

## Timings

Measured on 2026-09-09 in us-east-1 with the defaults (two `m7g.xlarge`
Graviton nodes, account default VPC, EKS 1.36). The full record with commands,
exit codes and sanitized output is in [LIVE-VALIDATION.md](LIVE-VALIDATION.md);
the UTC timestamps below are the ones recorded there.

| Step | Started | Finished | Duration | Notes |
| --- | --- | --- | --- | --- |
| `./demo.sh init` | 15:56:47 | 15:56:51 | 4 s | State bucket, `tofu init`, Helm repo |
| `./demo.sh apply` (first) | 16:22:57 | 16:38:12 | 15 min 15 s | EKS control plane 10 min 8 s, node group 1 min 28 s, add-ons about 2 min; includes a 79 s stop to fix the scheduler input (see the validation record) |
| `./demo.sh build-frontend` | 16:24:12 | 16:31:23 | 7 min 11 s | Cold buildx cache, native linux/arm64; ran alongside `apply` |
| `./demo.sh deploy` | 16:38:40 | 16:41:00 | 2 min 20 s | Collector schema seed plus first image pulls |
| `./demo.sh down` | 16:44:56 | 16:46:29 | 1 min 33 s | `helm uninstall`, namespaces, node group to 0 |
| `./demo.sh up` | 16:54:06 | 16:56:33 | 2 min 27 s | Scale to 2, nodes Ready in about 60 s, CoreDNS, full deploy, tunnel |

Telemetry was queryable in ClickHouse Cloud within three minutes of the
storefront opening (`./demo.sh verify` at 16:44:15 showed 14 858 spans, 6 053
log rows, 47 073 metric rows and 24 replay sessions in the preceding 15
minutes). From idle to a browsable storefront is therefore under three minutes,
and the first apply is the only step that takes a quarter of an hour. EC2
instances linger for a few minutes after `down` returns (the node group update
is accepted before the instances finish terminating); this is invisible to `up`,
which waits for exactly `node_count` nodes whose status is `Ready`.
