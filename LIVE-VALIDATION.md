# Live validation record

A full bring-up of this repository on a real AWS account and a real ClickHouse
Cloud service, run on 2026-09-09 from an Apple Silicon Mac. It is the evidence
that the scripts do what the README says they do; the README `## Timings`
section is derived from the timestamps below.

How to read it:

- Every step gives the UTC timestamp when it started and finished, the exact
  command, its exit status and the relevant part of its output.
- Output is sanitized. The AWS account id is `<account-id>`; the caller's IAM
  role is shown as its IAM Identity Center permission-set name,
  `SolutionArchitect`; the ECR registry host is `<ecr-registry>`; the ClickHouse
  Cloud endpoint, user, password and OTLP token never appear. The state bucket
  is `otel-demo-eks-tfstate-<account-id>`.
- Two `apply` runs appear on purpose: the first one failed on an EC2 quota and
  is recorded as context for the design change it caused.

Environment: aws CLI 2.36, OpenTofu 1.12, Docker 29 with buildx (native
linux/arm64), kubectl 1.37, helm 4.2.4, jq. `envvars.aws` set `AWS_PROFILE`
to a profile authenticated with `aws login` (an IAM Identity Center session,
not an `aws configure sso` profile; see [Fixes applied](#fixes-applied)) and
`AWS_REGION=us-east-1`. `envvars.clickhouse` held the five keys for the
`clickstack` user and `otel` database created by `sql/create-user.sql`.

## 1. `./demo.sh init`

Run once, on the first attempt (the second attempt resumed from its result; the
S3 backend it initialised stayed valid across the task-8 change to `tofu/`).

- Started: `2026-09-09T15:56:47Z`
- Finished: `2026-09-09T15:56:51Z`
- Command: `./demo.sh init`
- Exit status: `0`

```text
==> authenticated as
|  Account|  <account-id>
|  Arn    |  <assumed-role: SolutionArchitect permission set>/<user>
==> creating state bucket s3://otel-demo-eks-tfstate-<account-id> in us-east-1
==> enabling versioning and blocking public access on s3://otel-demo-eks-tfstate-<account-id>
==> tofu init (backend s3://otel-demo-eks-tfstate-<account-id>/otel-demo-eks/terraform.tfstate)
Initializing the backend...
Successfully configured the backend "s3"! OpenTofu will automatically
use this backend unless the backend configuration changes.
Initializing modules...
Initializing provider plugins...
- Using previously-installed hashicorp/aws v6.63.0
OpenTofu has been successfully initialized!
==> helm repo open-telemetry
==> init complete
Next: ./demo.sh apply
```

## 2. `./demo.sh apply --yes` (first attempt, failed)

Recorded as context. At this point `tofu/vpc.tf` still created a dedicated VPC
unconditionally, and the account's us-east-1 VPC quota (5) was already used by
VPCs belonging to other people.

- Started: `2026-09-09T15:57:35Z`
- Finished: `2026-09-09T15:57:41Z`
- Command: `./demo.sh apply --yes`
- Exit status: `1`

```text
==> tofu apply
Plan: 46 to add, 0 to change, 0 to destroy.
aws_ecr_repository.frontend: Creating...
aws_iam_role.scheduler: Creating...
module.eks.aws_iam_role.this[0]: Creating...
module.vpc.aws_vpc.this[0]: Creating...
aws_iam_role.scheduler: Creation complete after 1s [id=otel-demo-eks-scheduler]
module.eks.aws_iam_role.this[0]: Creation complete after 1s [id=otel-demo-eks-cluster-<suffix>]
aws_ecr_repository.frontend: Creation complete after 1s [id=otel-demo-frontend]
module.eks.aws_iam_role_policy_attachment.this["AmazonEKSClusterPolicy"]: Creation complete after 0s
aws_ecr_lifecycle_policy.frontend: Creation complete after 0s [id=otel-demo-frontend]
│ Error: creating EC2 VPC: operation error EC2: CreateVpc, https response error
│ StatusCode: 400, api error VpcLimitExceeded: The maximum number of VPCs has been reached.
│   with module.vpc.aws_vpc.this[0],
```

Only free resources were left in state: the ECR repository and its lifecycle
policy, the scheduler IAM role, and the EKS cluster IAM role with its policy
attachment. The user chose to run the demo in the account default VPC instead
of raising the quota; task 8 added the `use_default_vpc` toggle (default
`true`), and the second attempt resumed from this state.

## 3. `./demo.sh apply --yes` (resumed, default VPC)

Resumed from the state above with `use_default_vpc = true`. The plan created
30 resources: the cluster and node security groups and rules, the EKS cluster,
the cluster-creator access entry, the `vpc-cni`, `kube-proxy` and `coredns`
add-ons, the node IAM role, launch template and the `demo` managed node group,
and the scheduler's inline policy and schedule. `module.vpc` was absent from
the plan (count 0), and no `aws_vpc`, `aws_subnet` or default-VPC resource was
created.

- Started: `2026-09-09T16:22:57Z`
- Finished: `2026-09-09T16:36:31Z`
- Command: `./demo.sh apply --yes`
- Exit status: `1` (29 of 30 resources created; the last one failed, see below)

```text
==> tofu apply
Plan: 30 to add, 0 to change, 0 to destroy.
module.eks.aws_security_group.node[0]: Creation complete after 2s
module.eks.aws_security_group.cluster[0]: Creation complete after 2s
module.eks.aws_eks_cluster.this[0]: Creation complete after 10m8s [id=otel-demo-eks]
module.eks.aws_eks_access_entry.this["cluster_creator"]: Creation complete after 1s [id=otel-demo-eks:<role: SolutionArchitect permission set>]
module.eks.aws_eks_access_policy_association.this["cluster_creator_admin"]: Creation complete after 0s
module.eks.aws_eks_addon.before_compute["vpc-cni"]: Creation complete after 35s [id=otel-demo-eks:vpc-cni]
module.eks.module.eks_managed_node_group["demo"].aws_launch_template.this[0]: Creation complete after 6s
module.eks.module.eks_managed_node_group["demo"].aws_eks_node_group.this[0]: Creation complete after 1m28s [id=otel-demo-eks:demo]
aws_iam_role_policy.scheduler_scale: Creation complete after 1s [id=otel-demo-eks-scheduler:scale-demo-nodegroup]
module.eks.aws_eks_addon.this["kube-proxy"]: Creation complete after 25s [id=otel-demo-eks:kube-proxy]
module.eks.aws_eks_addon.this["coredns"]: Creation complete after 56s [id=otel-demo-eks:coredns]
│ Error: creating EventBridge Scheduler Schedule (otel-demo-eks-nightly-scale-down):
│ operation error Scheduler: CreateSchedule, https response error StatusCode: 400,
│ ValidationException: Invalid RequestJson provided. Reason Request payload is
│ missing the following field(s): ClusterName, NodegroupName.
```

The cluster and node group were `ACTIVE` at this point (`aws eks
describe-nodegroup`: `status ACTIVE, desiredSize 2, AL2023_ARM_64_STANDARD,
m7g.xlarge`). The failure is a bug in `tofu/scheduler.tf`: the universal
target's `input` used the camelCase keys of the EKS REST body, but EventBridge
Scheduler wants the SDK request shape in PascalCase. Fixed in place (see
[Fixes applied](#fixes-applied)) and re-run:

- Started: `2026-09-09T16:37:50Z`
- Finished: `2026-09-09T16:38:12Z`
- Command: `./demo.sh apply --yes`
- Exit status: `0`

```text
==> tofu apply
  # aws_scheduler_schedule.nightly_scale_down will be created
      + schedule_expression          = "cron(0 23 * * ? *)"
      + schedule_expression_timezone = "America/New_York"
      + state                        = "ENABLED"
      + target {
          + arn      = "arn:aws:scheduler:::aws-sdk:eks:updateNodegroupConfig"
          + input    = jsonencode({ ClusterName = "otel-demo-eks", NodegroupName = "demo",
                                    ScalingConfig = { DesiredSize = 0, MaxSize = 2, MinSize = 0 } })
Plan: 1 to add, 0 to change, 0 to destroy.
aws_scheduler_schedule.nightly_scale_down: Creation complete after 1s [id=default/otel-demo-eks-nightly-scale-down]
Apply complete! Resources: 1 added, 0 changed, 0 destroyed.
Outputs:
cluster_name = "otel-demo-eks"
ecr_registry = "<ecr-registry>"
ecr_repository_url = "<ecr-registry>/otel-demo-frontend"
node_count = 2
node_platform = "linux/arm64"
nodegroup_name = "demo"
region = "us-east-1"
scheduler_name = "otel-demo-eks-nightly-scale-down"
subnet_ids = [ "subnet-<a>", "subnet-<b>" ]
vpc_id = "vpc-<default>"
==> kubeconfig
NAME                            STATUS   ROLES    AGE     VERSION               INTERNAL-IP     EXTERNAL-IP     OS-IMAGE                        KERNEL-VERSION                            CONTAINER-RUNTIME
ip-172-31-31-246.ec2.internal   Ready    <none>   2m56s   v1.36.3-eks-cb19647   172.31.31.246   <public-ip>     Amazon Linux 2023.12.20260831   6.18.44-99.149.amzn2023.aarch64 (arm64)   containerd://2.2.5+unknown
ip-172-31-39-130.ec2.internal   Ready    <none>   2m55s   v1.36.3-eks-cb19647   172.31.39.130   <public-ip>     Amazon Linux 2023.12.20260831   6.18.44-99.149.amzn2023.aarch64 (arm64)   containerd://2.2.5+unknown
Next:
  ./demo.sh build-frontend
  ./demo.sh deploy
```

Wall clock for the first apply, taken as one operation from the start of the
resumed run to the end of the completing run: `16:22:57Z` to `16:38:12Z`,
**15 min 15 s**, of which 10 min 8 s was the EKS control plane and 1 min 28 s
the node group. The 79 s between the two runs (reading the error, changing six
lines) is included; the six seconds of the quota-failed first attempt are not.
The `172.31.x.x` node addresses confirm the default VPC (`172.31.0.0/16`).

## 4. `kubectl get nodes -L kubernetes.io/arch`

- Timestamp: `2026-09-09T16:38:35Z`
- Command: `kubectl get nodes -L kubernetes.io/arch`
- Exit status: `0`

```text
NAME                            STATUS   ROLES    AGE     VERSION               ARCH
ip-172-31-31-246.ec2.internal   Ready    <none>   3m21s   v1.36.3-eks-cb19647   arm64
ip-172-31-39-130.ec2.internal   Ready    <none>   3m20s   v1.36.3-eks-cb19647   arm64
```

`node_count` is 2; both nodes are `Ready` and `arm64`.

## 5. `aws eks list-access-entries --cluster-name otel-demo-eks`

- Timestamp: `2026-09-09T16:38:36Z`
- Command: `aws eks list-access-entries --cluster-name otel-demo-eks`
- Exit status: `0`

```text
{
    "accessEntries": [
        "<role: SolutionArchitect permission set>",
        "<role: AWSServiceRoleForAmazonEKS, EKS service-linked>",
        "<role: demo-eks-node-group-<suffix>, node role>"
    ]
}
```

The first entry is the cluster-creator access entry for the `SolutionArchitect`
permission-set role that ran the apply (`enable_cluster_creator_admin_permissions`),
which is what lets the same SSO session run `kubectl`. The other two are the EKS
service-linked role and the node role EKS adds itself.

## 6. `./demo.sh build-frontend`

Run while the control plane was still creating: the ECR repository existed from
the first attempt and `tofu output` already served its URL, so the image build
overlapped the cluster wait. On a fresh account this step simply runs after
`apply` as the README says.

- Started: `2026-09-09T16:24:12Z`
- Finished: `2026-09-09T16:31:23Z` (7 min 11 s, cold buildx cache, native
  linux/arm64 on Apple Silicon)
- Command: `./demo.sh build-frontend`
- Exit status: `0`

```text
==> fetching the OpenTelemetry demo source at d6fd782e
Cloning into 'opentelemetry-demo'...
HEAD is now at d6fd782 chore(deps): bump go.opentelemetry.io/contrib to v1.46.0 line in checkout and product-catalog (#3902)
==> applying patches/frontend-session-replay.patch (the ClickStack browser SDK wiring)
==> logging docker in to <ecr-registry>
Login Succeeded
==> building <ecr-registry>/otel-demo-frontend:d6fd782e-c9b5ca3e for linux/arm64 (first run downloads the npm tree; buildx caches it after)
Pushed <ecr-registry>/otel-demo-frontend:d6fd782e-c9b5ca3e
Next: ./demo.sh deploy (rolls the frontend onto this image)
```

`frontend_tag` printed `d6fd782e-c9b5ca3e`, matching the pushed tag. The pushed
object is a single-platform manifest (attestations are disabled in
`build-frontend.sh` on purpose), so the plain `imagetools inspect` shows only
the digest; the `--format` template reads the platform from the image config.

- Timestamp: `2026-09-09T16:32:05Z` and `2026-09-09T16:32:48Z`
- Commands and exit status `0` for both:

```text
$ docker buildx imagetools inspect "$(tofu -chdir=tofu output -raw ecr_repository_url):$(frontend_tag)"
Name:      <ecr-registry>/otel-demo-frontend:d6fd782e-c9b5ca3e
MediaType: application/vnd.docker.distribution.manifest.v2+json
Digest:    sha256:3d1c5f3ff4505c99bd82064713a984354bad8655f0f5071346b8c15a0626d6e4

$ docker buildx imagetools inspect "$(tofu -chdir=tofu output -raw ecr_repository_url):$(frontend_tag)" \
    --format '{{.Name}} {{.Manifest.MediaType}} platform={{.Image.OS}}/{{.Image.Architecture}}'
<ecr-registry>/otel-demo-frontend:d6fd782e-c9b5ca3e application/vnd.docker.distribution.manifest.v2+json platform=linux/arm64
```

## 7. `./demo.sh deploy`

- Started: `2026-09-09T16:38:40Z`
- Finished: `2026-09-09T16:41:00Z` (2 min 20 s, including the collector's
  schema seed against ClickHouse Cloud and the demo's image pulls on fresh nodes)
- Command: `./demo.sh deploy`
- Exit status: `0`

```text
==> ensuring namespaces clickstack and otel-demo
namespace/clickstack created
namespace/otel-demo created
==> creating Secret clickstack/clickstack-credentials from envvars.clickhouse
secret/clickstack-credentials created
==> deploying the ClickStack OTel collector
deployment.apps/clickstack-otel-collector created
service/clickstack-otel-collector created
deployment "clickstack-otel-collector" successfully rolled out
==> ClickStack collector logs (last 40 lines)
16:38:57 [seed] Running ClickHouse schema seed...
16:38:57 [seed] Target database: otel
16:39:38 [seed] Successfully connected to ClickHouse
16:39:38 [seed] OK   00001_create_database.sql (16.13ms)
16:39:38 [seed] OK   00002_otel_logs.sql (24.37ms)
16:39:38 [seed] OK   00003_otel_metrics.sql (92.13ms)
16:39:38 [seed] OK   00004_hyperdx_sessions.sql (18.17ms)
16:39:38 [seed] OK   00005_otel_traces.sql (21.16ms)
16:39:38 [seed] OK   00006_otel_logs_rollups.sql (58.12ms)
16:39:38 [seed] OK   00007_otel_traces_rollups.sql (41.08ms)
16:39:38 [seed] Schema seed completed successfully
Running in standalone mode (OPAMP_SERVER_URL not set)
OTLP_AUTH_TOKEN is configured, enabling bearer token authentication
==> creating Secret otel-demo/clickstack-otlp-token
secret/clickstack-otlp-token created
==> frontend image otel-demo-frontend:d6fd782e-c9b5ca3e is in ECR
==> installing the OpenTelemetry demo (chart 0.41.0)
Release "otel-demo" does not exist. Installing it now.
NAME: otel-demo
NAMESPACE: otel-demo
STATUS: deployed
REVISION: 1
==> tunnel up (pid 40617): http://localhost:8080/
Storefront:      http://localhost:8080
Feature flags:   http://localhost:8080/feature/
Load generator:  http://localhost:8080/loadgen/
```

The deploy log was grepped for the literal endpoint, password and token values
from `envvars.clickhouse`: zero occurrences (the collector logs the token's
*presence*, not its value).

### Pods, frontend image, tunnel

- Timestamp: `2026-09-09T16:41:20Z` to `16:41:21Z`
- Commands (exit status `0` for all four):

```text
$ kubectl get pods -n clickstack
NAME                                         READY   STATUS    RESTARTS   AGE
clickstack-otel-collector-6669f689dd-mf5qf   1/1     Running   0          2m27s

$ kubectl get pods -n otel-demo
NAME                               READY   STATUS    RESTARTS   AGE
accounting-7c96777d9-lfhfp         1/1     Running   0          83s
ad-777564cc96-8sps4                1/1     Running   0          81s
agent-fcd697844-lq2n7              1/1     Running   0          82s
astronomy-db-575b5557d5-lmtxb      1/1     Running   0          82s
cart-766c5f85df-4s4ns              1/1     Running   0          82s
chatbot-7d4bddbbd-7d2tl            1/1     Running   0          82s
checkout-5dd5c98f4-4qqp4           1/1     Running   0          82s
currency-5bdfd58c5c-bbnnc          1/1     Running   0          82s
email-b498b7dc6-rzqcm              1/1     Running   0          82s
flagd-5b88d44f56-nx72x             2/2     Running   0          80s
fraud-detection-cd454fb6d-5hdj8    1/1     Running   0          82s
frontend-59dddd467c-9844l          1/1     Running   0          82s
frontend-proxy-65d7679769-zzzdx    1/1     Running   0          81s
image-provider-6c95f5cc6d-q4xf8    1/1     Running   0          82s
kafka-56f6d7949f-crbjj             1/1     Running   0          81s
load-generator-547b66979-4gq6p     1/1     Running   0          80s
mcp-b6b67c7c4-5vb9f                1/1     Running   0          81s
opamp-server-6b4b789bf6-lr9br      1/1     Running   0          80s
otel-collector-agent-4vsql         1/1     Running   0          83s
otel-collector-agent-w7hxn         1/1     Running   0          83s
payment-5d6476fcfd-tbqfd           1/1     Running   0          81s
product-catalog-5fbf55b876-rsg2v   1/1     Running   0          80s
quote-77b6695647-q74fw             1/1     Running   0          80s
recommendation-56bc476dd-fhllc     1/1     Running   0          82s
shipping-794c6866fb-g4scn          1/1     Running   0          82s
telemetry-docs-75f89cd79b-949sc    1/1     Running   0          81s
valkey-cart-c5cf5757b-4smbq        1/1     Running   0          83s

$ kubectl -n otel-demo get deploy frontend -o jsonpath='{.spec.template.spec.containers[0].image}'
<ecr-registry>/otel-demo-frontend:d6fd782e-c9b5ca3e

$ curl -s -o /dev/null -w %{http_code} http://localhost:8080/
200
```

Every pod is `Running` (no Jaeger, Prometheus, Grafana or OpenSearch pods, as
the values file disables them; no `Completed` Jobs exist in this chart); the
frontend Deployment runs the ECR image at exactly `$(frontend_tag)`; the
storefront answers 200 through the port-forward tunnel.

## 8. Browsing and `./demo.sh verify`

At `16:41:29Z` `open http://localhost:8080` put the storefront in the user's
browser and they clicked around the shop for over a minute (this is what
creates `hyperdx_sessions` rows: the session-replay SDK runs in the browser,
not in curl). In parallel, nine rounds of `curl` hit `/`, `/cart`, three
product pages, `/api/products`, `/api/recommendations` and `/api/cart`
(all 200; `/loadgen/` answers 308 to its trailing-slash redirect), and the
chart's load generator ran continuously.

- Timestamp: `2026-09-09T16:44:15Z` (2 min 46 s after the browser opened,
  5 min 15 s after the demo release was deployed)
- Command: `./demo.sh verify`
- Exit status: `0`

```text
==> Rows written in the last 15 minutes to the database: otel
   ┌─signal──────────┬─count()─┐
1. │ logs            │    6053 │
2. │ traces          │   14858 │
3. │ metrics (sum)   │   28293 │
4. │ metrics (gauge) │   18780 │
   └─────────────────┴─────────┘

==> Services reporting spans
    ┌─ServiceName─────┬─spans─┐
 1. │ frontend        │  3558 │
 2. │ frontend-proxy  │  3446 │
 3. │ flagd           │  2456 │
 4. │ frontend-web    │  1485 │
 5. │ product-catalog │  1304 │
 6. │ cart            │   675 │
 7. │ load-generator  │   489 │
 8. │ image-provider  │   334 │
 9. │ recommendation  │   278 │
10. │ checkout        │   230 │
11. │ currency        │   130 │
12. │ ad              │   130 │
13. │ quote           │    78 │
14. │ shipping        │    68 │
15. │ email           │    64 │
16. │ accounting      │    49 │
17. │ payment         │    44 │
18. │ fraud-detection │    37 │
19. │ chatbot         │     2 │
20. │ mcp             │     1 │
    └─────────────────┴───────┘

==> Session replay events and distinct sessions (last 15 minutes)
   ┌─count()─┬─uniqExact(ar⋯essionId'))─┐
1. │    2235 │                       24 │
   └─────────┴──────────────────────────┘

==> Collector pod state
NAME                                         READY   STATUS    RESTARTS   AGE
clickstack-otel-collector-6669f689dd-mf5qf   1/1     Running   0          5m25s
NAME                               READY   STATUS    RESTARTS   AGE
otel-collector-agent-4vsql         1/1     Running   0          4m22s
otel-collector-agent-w7hxn         1/1     Running   0          4m22s

==> Collector logs (last 40 lines)
16:39:38 [seed] Schema seed completed successfully
Running in standalone mode (OPAMP_SERVER_URL not set)
OTLP_AUTH_TOKEN is configured, enabling bearer token authentication
```

All four signals have rows; the span table names `frontend` (3558) and
`checkout` (230) among 20 services; `hyperdx_sessions` holds 2235 replay
events from 24 distinct `rum.sessionId`s. The collector log has no export
errors.

## 9. Feature flags and status

- Commands, timestamps and exit status:

```text
$ ./demo.sh flag paymentFailure 50%            # 2026-09-09T16:42:15Z, exit 0
paymentFailure: off -> 50%

$ ./demo.sh flag paymentFailure                # 2026-09-09T16:42:20Z, exit 0
paymentFailure is "50%" (available: 10%, 100%, 25%, 50%, 75%, 90%, off)

$ ./demo.sh flag --reset                       # 2026-09-09T16:42:42Z, exit 0
==> Restarting flagd -- all flags return to the cm/flagd-config defaults
deployment.apps/flagd restarted
Waiting for deployment "flagd" rollout to finish: 1 old replicas are pending termination...
deployment "flagd" successfully rolled out

$ ./demo.sh flag paymentFailure                # 2026-09-09T16:42:49Z, exit 0
paymentFailure is "off" (available: 10%, 100%, 25%, 50%, 75%, 90%, off)
```

The flag flipped live (no restart) and the read-back inside `flag` confirmed
the new variant; `--reset` restarted flagd and the flag returned to `off`.

- Timestamp: `2026-09-09T16:42:54Z` (and again at `16:44:24Z` to capture the
  exit status separately; identical output)
- Command: `./demo.sh status`
- Exit status: `0`

```text
==> identity
|  Account|  <account-id>
|  Arn    |  <assumed-role: SolutionArchitect permission set>/<user>
==> node group
    desiredSize=2 status=ACTIVE
==> cluster
NAME                            STATUS   ROLES    AGE     VERSION
ip-172-31-31-246.ec2.internal   Ready    <none>   7m43s   v1.36.3-eks-cb19647
ip-172-31-39-130.ec2.internal   Ready    <none>   7m42s   v1.36.3-eks-cb19647
==> pods by phase (all namespaces)
  34 Running
==> helm releases in otel-demo
NAME     	NAMESPACE	REVISION	UPDATED                             	STATUS  	CHART                    	APP VERSION
otel-demo	otel-demo	1       	2026-09-09 12:39:55.463487 -0400 EDT	deployed	opentelemetry-demo-0.41.0	3.0.0
==> tunnel
tunnel: up (pid 40617) http://localhost:8080/
==> frontend image in ECR
    otel-demo-frontend:d6fd782e-c9b5ca3e present
==> nightly scale-down schedule
| ScheduleExpression  |  State   |     Timezone       |
|  cron(0 23 * * ? *) |  ENABLED |  America/New_York  |
```

## 10. `./demo.sh down`

- Started: `2026-09-09T16:44:56Z`
- Finished: `2026-09-09T16:46:29Z` (1 min 33 s)
- Command: `./demo.sh down`
- Exit status: `0`

```text
==> tunnel stopped
==> helm uninstall otel-demo
release "otel-demo" uninstalled
==> deleting namespaces otel-demo and clickstack
namespace "otel-demo" deleted
namespace "clickstack" deleted
==> scaling node group demo to desiredSize=0 (minSize=0, maxSize=2)
==> demo is down: node group at 0, control plane kept. Telemetry in ClickHouse Cloud is untouched.
Bring it back with: ./demo.sh up
```

### Idle-state checks

```text
$ aws eks describe-nodegroup --cluster-name otel-demo-eks --nodegroup-name demo \
    --query 'nodegroup.{status:status,scalingConfig:scalingConfig}'     # 2026-09-09T16:46:50Z, exit 0
{
    "status": "ACTIVE",
    "scalingConfig": {
        "minSize": 0,
        "maxSize": 2,
        "desiredSize": 0
    }
}

$ kubectl get ns                                                        # 2026-09-09T16:46:54Z, exit 0
NAME              STATUS   AGE
default           Active   17m
kube-node-lease   Active   17m
kube-public       Active   17m
kube-system       Active   17m

$ tofu -chdir=tofu plan                                                 # 2026-09-09T16:46:59Z, exit 0
No changes. Your infrastructure matches the configuration.
OpenTofu has compared your real infrastructure against your configuration and
found no differences, so no changes are needed.
```

The node group is at desired 0, neither `otel-demo` nor `clickstack` exists,
and the plan is empty: the node-group submodule ignores `desired_size` drift,
so scaling by script never fights OpenTofu. `kubectl get nodes` still listed
the two node objects at `16:46:55Z`; `aws eks wait nodegroup-active` returns
when the scaling update is accepted, and the EC2 instances take a few more
minutes to terminate and drop out of the API. The `up` below was started at
`16:54:06Z`, seven minutes after `down` finished, when one drained node object
(`Ready,SchedulingDisabled`) was still listed; `wait_nodes_ready` counts only
nodes whose status is exactly `Ready`, so it waited for the two new nodes.

## 11. `./demo.sh up`

- Started: `2026-09-09T16:54:06Z`
- Finished: `2026-09-09T16:56:33Z` (2 min 27 s: scale-up and node
  registration about 60 s, CoreDNS rollout, then the same deploy as step 7
  with the image already in ECR)
- Command: `./demo.sh up`
- Exit status: `0`

```text
==> scaling node group demo to desiredSize=2 (minSize=0, maxSize=2)
==> waiting for 2 Ready node(s)
==> 2 node(s) Ready; waiting for CoreDNS
deployment "coredns" successfully rolled out
==> ensuring namespaces clickstack and otel-demo
namespace/clickstack created
namespace/otel-demo created
==> creating Secret clickstack/clickstack-credentials from envvars.clickhouse
secret/clickstack-credentials created
==> deploying the ClickStack OTel collector
deployment "clickstack-otel-collector" successfully rolled out
==> ClickStack collector logs (last 40 lines)
Running in standalone mode (OPAMP_SERVER_URL not set)
OTLP_AUTH_TOKEN is configured, enabling bearer token authentication
==> creating Secret otel-demo/clickstack-otlp-token
secret/clickstack-otlp-token created
==> frontend image otel-demo-frontend:d6fd782e-c9b5ca3e is in ECR
==> installing the OpenTelemetry demo (chart 0.41.0)
Release "otel-demo" does not exist. Installing it now.
STATUS: deployed
REVISION: 1
==> tunnel up (pid 44494): http://localhost:8080/
Storefront:      http://localhost:8080
Feature flags:   http://localhost:8080/feature/
Load generator:  http://localhost:8080/loadgen/
```

```text
$ curl -s -o /dev/null -w %{http_code} http://localhost:8080/        # 2026-09-09T16:57:19Z, exit 0
200

$ kubectl get nodes -L kubernetes.io/arch                              # 2026-09-09T16:57:19Z, exit 0
NAME                           STATUS   ROLES    AGE     VERSION               ARCH
ip-172-31-26-40.ec2.internal   Ready    <none>   2m31s   v1.36.3-eks-cb19647   arm64
ip-172-31-41-47.ec2.internal   Ready    <none>   2m30s   v1.36.3-eks-cb19647   arm64
```

Two new nodes (different addresses from step 4), storefront answering 200
through the new tunnel. At `16:57:20Z` every `otel-demo` and `clickstack` pod
was `Running`; the only non-Running pods in the cluster were two `kube-system`
DaemonSet pods (`aws-node`, `kube-proxy`) `Pending` against the last drained
node from step 10, which disappeared with it.

## 12. Final `./demo.sh down` (end state)

The user wanted the cluster left idle: control plane kept, node group at 0.

- Started: `2026-09-09T16:57:20Z`
- Finished: `2026-09-09T16:58:51Z` (1 min 31 s)
- Command: `./demo.sh down`
- Exit status: `0`

```text
==> tunnel stopped
==> helm uninstall otel-demo
release "otel-demo" uninstalled
==> deleting namespaces otel-demo and clickstack
namespace "otel-demo" deleted
namespace "clickstack" deleted
==> scaling node group demo to desiredSize=0 (minSize=0, maxSize=2)
==> demo is down: node group at 0, control plane kept. Telemetry in ClickHouse Cloud is untouched.
Bring it back with: ./demo.sh up
```

```text
$ aws eks describe-nodegroup --cluster-name otel-demo-eks --nodegroup-name demo \
    --query 'nodegroup.{status:status,desiredSize:scalingConfig.desiredSize}'   # 2026-09-09T16:59:21Z, exit 0
{ "status": "ACTIVE", "desiredSize": 0 }

$ kubectl get ns --no-headers | awk '{print $1}'                                  # 2026-09-09T16:59:22Z, exit 0
default kube-node-lease kube-public kube-system

$ ./demo.sh tunnel status                                                         # 2026-09-09T16:59:22Z, exit 1 (= not running, as intended)
tunnel: not running
```

The nightly schedule (`cron(0 23 * * ? *)` America/New_York, ENABLED) stays
armed as a backstop. `./demo.sh destroy` was never run.

## Timings

| Step | Started (UTC) | Finished (UTC) | Duration |
| --- | --- | --- | --- |
| `./demo.sh init` | 15:56:47 | 15:56:51 | 4 s |
| `./demo.sh apply --yes` (first attempt, quota failure) | 15:57:35 | 15:57:41 | 6 s |
| `./demo.sh apply --yes` (first apply, resumed run + completing run) | 16:22:57 | 16:38:12 | 15 min 15 s |
| `./demo.sh build-frontend` (overlapped with apply) | 16:24:12 | 16:31:23 | 7 min 11 s |
| `./demo.sh deploy` | 16:38:40 | 16:41:00 | 2 min 20 s |
| browser open to `./demo.sh verify` | 16:41:29 | 16:44:15 | 2 min 46 s |
| `./demo.sh down` | 16:44:56 | 16:46:29 | 1 min 33 s |
| `./demo.sh up` | 16:54:06 | 16:56:33 | 2 min 27 s |
| `./demo.sh down` (final) | 16:57:20 | 16:58:51 | 1 min 31 s |

The README `## Timings` table carries the first apply, build-frontend, deploy,
down and up rows from this table.

## Fixes applied

Committed in this task, in the same commit as this record.

1. **`tofu/scheduler.tf`: EventBridge Scheduler universal-target input keys.**
   The `input` for `arn:aws:scheduler:::aws-sdk:eks:updateNodegroupConfig`
   used the camelCase field names of the EKS REST body (`clusterName`,
   `nodegroupName`, `scalingConfig.minSize/maxSize/desiredSize`).
   `CreateSchedule` rejected it: `ValidationException: Invalid RequestJson
   provided. Reason Request payload is missing the following field(s):
   ClusterName, NodegroupName` (step 3). Universal targets take the SDK request
   shape in PascalCase, so the keys are now `ClusterName`, `NodegroupName`,
   `ScalingConfig = { MinSize, MaxSize, DesiredSize }`, with a comment citing
   the error. The schedule then created in 1 s and `./demo.sh status` reads it
   back as `ENABLED`. This is the one fix outside `scripts/` and `k8s/` (the
   two documents are this task's deliverables); it was the only way to get
   `apply` to exit 0, and it corrects the literal key
   spelling that task 2's acceptance criterion had pinned. `scripts/nightly.sh`
   passes the schedule's `Input` through from `get-schedule` unchanged, so it
   needed no edit.
2. **`scripts/apply.sh` and `scripts/destroy.sh` header comments** (plan
   deferral d2). Both said a VPC is created/removed unconditionally; since the
   `use_default_vpc` toggle (default `true`) that holds only when it is
   `false`. Comment-only change.

Operational notes, no code change:

- **AWS session type.** The profile in `envvars.aws` was authenticated with
  `aws login` (an IAM Identity Center *login session*), not with `aws configure
  sso`. `aws_login` in `scripts/lib/aws.sh` only checks `aws sts
  get-caller-identity` and falls back to `aws sso login`, which cannot refresh
  this kind of session. The session stayed valid for the whole run (about one
  hour), so the fallback was never exercised. If it expires, run `aws login`
  yourself and re-run the failed subcommand; the library is deliberately not
  patched here (plan constraint: no task after task 1 edits `scripts/lib/`).
- **Image platform inspection.** Because `build-frontend.sh` disables
  provenance and SBOM attestations, the pushed tag is a single
  `manifest.v2+json`, and `docker buildx imagetools inspect <image>` prints
  only name, media type and digest. Add
  `--format '{{.Image.OS}}/{{.Image.Architecture}}'` to see the platform
  (step 6).
- **Nodes linger after `down`.** `aws eks wait nodegroup-active` returns when
  the scaling update is accepted; the EC2 instances then take several minutes
  to drain and terminate, during which `kubectl get nodes` still lists them
  (`Ready,SchedulingDisabled` once cordoned). `wait_nodes_ready` counts only
  exact `Ready`, so an immediate `up` is not fooled by them.
