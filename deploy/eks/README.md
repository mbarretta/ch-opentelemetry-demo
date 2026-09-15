# EKS deployment: cluster design, costs and recovery

The EKS target runs the same demo as the laptop Compose stack — the Astronomy
Shop, the AI shopping assistant, ClickStack and Langfuse — on an AWS EKS cluster
that anyone with the AWS profile can reach, instead of one Mac. Between demos
the node group scales to zero and only the EKS control plane is billed.

This file is the reference half: why the cluster looks the way it does, what it
costs, and how to get out of trouble. The runbooks live in the root README:
[Run on EKS](../../README.md#run-on-eks) for the prerequisites, the first run and
the everyday cycle, and [Configuration](../../README.md#configuration) for what
each `.env` key does on each target. Every ordinary command is a subcommand of
the one CLI, run from the repository root:

```sh
scripts/demo.py eks --help
```

## Architecture

```
 your Mac
 ├── demo.py eks …          aws / tofu / kubectl / helm / docker, authenticated
 │                          with an IAM Identity Center (SSO) profile
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
 │                       frontend, frontend-proxy, agent, mcp  <- our four images
 │                       from ECR (ch-opentelemetry-demo/<service>)
 │                       gateway collector (DaemonSet)
 │                             │ OTLP/HTTP + `authorization: <OTLP_AUTH_TOKEN>`
 │   ns clickstack     clickstack-otel-collector 2.38.0
 │                             │ ClickHouse HTTPS interface (TLS, port 8443)
 └─────────────────────────────┴──> ClickHouse Cloud, database `otel`
                               └──> Langfuse Cloud (assistant traces only,
                                    via the collector's Langfuse pipeline)

 EventBridge Scheduler "otel-demo-eks-nightly-scale-down"
   every night at scale_down_hour: eks:UpdateNodegroupConfig desiredSize=0
```

Nothing in the cluster is reachable from the internet. The storefront, the
feature-flag UI and the load generator are served to your browser through the
port-forward; the only inbound path to the cluster is the EKS API endpoint,
which authenticates every request with IAM. The demo's bundled backends
(Jaeger, Prometheus, Grafana, OpenSearch) are turned off; ClickStack replaces
all four.

Inside the namespace, `k8s/network-policy.yaml` is the one restriction: only the
frontend pod may open a connection to the agent's port 8010. The agent's HTTP
API is unauthenticated and its pod holds the live model `API_KEY`, so without
that policy any pod in `otel-demo` -- and anything on your laptop that can reach
the port-forward, through the front proxy's `/api/assistant/` route -- could
spend the credential. The policy is ingress-only: the agent still calls the
model endpoint, the MCP server and the collector.

## Design decisions

| Decision | Why |
| --- | --- |
| **Private access, `kubectl port-forward` only** (no LoadBalancer, Ingress or NodePort) | Anyone with the AWS profile can open the tunnel, nobody else can reach the storefront at all, and there is no load balancer or public endpoint to secure or pay for. |
| **Public subnets, no NAT gateway** | ClickHouse Cloud accepts connections from any IP and nothing inbound reaches the nodes, so a NAT gateway (hourly plus per-GB) would be pure cost; nodes get public IPs (`map_public_ip_on_launch`) and egress directly through the internet gateway. |
| **Run in the account default VPC** (`use_default_vpc = true`) | The account's us-east-1 VPC quota is used up by other teams' VPCs, so the cluster uses the default VPC's public subnets instead of creating its own; that also means no NAT gateway to add and nothing network-side to delete on teardown. OpenTofu only *reads* the default VPC and subnets (data sources, no `aws_default_*` resources), so it can never modify or destroy them. Set `use_default_vpc = false` in an account with headroom for a dedicated 10.20.0.0/16 VPC. |
| **Graviton (arm64) nodes** | The four custom images are built on the Mac with `docker buildx`, and `linux/arm64` builds natively on Apple Silicon (no QEMU emulation); m7g is also cheaper than the x86 equivalent. `demo.py publish` refuses to push a manifest whose platform does not match the `node_platform` output, so a wrong build cannot reach the cluster silently. |
| **Scale to zero instead of destroy** | The EKS control plane is the slow part of `apply`, so keeping it (about $73/month) turns "stand the demo up" into scaling a node group rather than rebuilding a cluster; `demo.py eks destroy` is still there for when the demo is really over. |
| **OpenTofu, remote state in S3** | The control plane is long-lived and costs money; state on one laptop could orphan it, and a second presenter could not `up`/`down` without it. The bucket is versioned so a clobbered state file can be restored. |
| **Nightly scale-to-zero via EventBridge Scheduler** | A forgotten demo costs at most one day of nodes. The schedule calls `eks:UpdateNodegroupConfig` directly (universal target), so there is no Lambda to maintain. |
| **Cost trims in the EKS module** | No control-plane CloudWatch logs, no customer-managed KMS key (`create_kms_key = false` together with `encryption_config = null`, which the module needs to skip the encryption block entirely; EKS still encrypts secrets with an AWS-owned key), no IRSA OIDC provider, no EBS CSI (nothing needs a PersistentVolume with the backends off). |
| **One ECR repository per custom image** (`ch-opentelemetry-demo/{frontend,frontend-proxy,agent,mcp}`) | Per-repository lifecycle policies and scan settings, and `demo.py eks status` can report each image independently. Tags are `MUTABLE` because they are derived from image content: the same tag always means the same bytes, so re-pushing it is a no-op that must be allowed rather than an overwrite of something else. That is also why the Helm values pull with `IfNotPresent`. |
| **One NetworkPolicy, on the agent only** (`k8s/network-policy.yaml`, with `enableNetworkPolicy` on the `vpc-cni` add-on) | The agent is the one pod in the demo that both answers unauthenticated requests and holds a credential worth money, so it is the one pod worth fencing: ingress to port 8010 is limited to the frontend pod, which is its only real caller. Defense in depth rather than a closed hole -- the storefront route in front of it has no authentication either, and the laptop target has the same posture -- so it buys a bounded blast radius, not a secured endpoint. The add-on setting is what makes it real: the VPC CNI accepts NetworkPolicy objects and ignores them unless its network policy agent is switched on, so a cluster applied without it needs one more `eks apply`. |
| **Secrets only in Kubernetes Secrets** | `demo.py eks deploy` assembles the ClickStack, Langfuse and model credentials from the root `.env`, pipes the Secret manifests to `kubectl apply -f -` on stdin, and lets the collector and the agent read them through `${env:...}` and `secretKeyRef`. No credential is written to a file in the cluster, to the Helm release, or to a process argument list. |
| **Langfuse stays external** | Langfuse Cloud serves both targets, so nothing in the cluster stores assistant traces and the EKS and laptop runs land in the same project. The collector's Langfuse pipeline is only rendered when `.env` carries the keys; without them the demo still runs, ClickStack-only. |

### Pinned versions

| Component | Version | Where |
| --- | --- | --- |
| Demo Helm chart `open-telemetry/opentelemetry-demo` | 0.41.0 | `CHART_VERSION` in `launcher/eks/config.py` |
| Demo source (the four custom images) | 3.0.0 (`1755859a`) | `TAG` / `COMMIT` in `launcher/core.py`, shared with the laptop target |
| EKS Kubernetes | 1.36 | `kubernetes_version` in `tofu/variables.tf` |
| ClickStack collector `clickhouse/clickstack-otel-collector` | 2.38.0 | `k8s/clickstack-collector.yaml` |
| `terraform-aws-modules/eks/aws` | ~> 21.25 (needs `hashicorp/aws` >= 6.59) | `tofu/eks.tf`, `tofu/versions.tf`, `tofu/.terraform.lock.hcl` |
| `terraform-aws-modules/vpc/aws` | ~> 6.7 | `tofu/vpc.tf` |

The chart and the demo source are pinned together: the frontend patches are
generated against the demo release, and a chart bump can move the images the
patches expect. `demo.py eks check` re-renders the chart against the committed
values offline and fails when a processor or exporter a pipeline references has
disappeared, which is the cheapest way to catch a chart bump that drifted.

### Measured timings (why scale-to-zero)

Measured on 2026-09-09 in us-east-1 with the defaults (two `m7g.xlarge`
Graviton nodes, account default VPC, EKS 1.36), when the commands were still
the EKS repository's shell scripts. The full record is in
[docs/history/eks-live-validation-2026-09-09.md](../../docs/history/eks-live-validation-2026-09-09.md).

| Step | Duration | Notes |
| --- | --- | --- |
| `eks init` | 4 s | State bucket, `tofu init`, Helm repo |
| `eks apply` (first) | 15 min 15 s | EKS control plane 10 min 8 s, node group 1 min 28 s, add-ons about 2 min |
| image build + push | 7 min 11 s | Cold buildx cache, native `linux/arm64`; ran alongside `apply` |
| `eks deploy` | 2 min 20 s | Collector schema seed plus first image pulls |
| `eks down` | 1 min 33 s | `helm uninstall`, namespaces, node group to 0 |
| `eks up` | 2 min 27 s | Scale to 2, nodes Ready in about 60 s, CoreDNS, full deploy, tunnel |

The first apply is the only quarter-hour step, and it is the one scale-to-zero
avoids repeating: idle to a browsable storefront is under three minutes.
Telemetry was queryable in ClickHouse Cloud within three minutes of the
storefront opening. EC2 instances linger for a few minutes after `down` returns
(the node group update is accepted before the instances finish terminating);
this is invisible to `up`, which waits for exactly `node_count` nodes whose
status is `Ready`.

## What is in this directory

| Path | Purpose |
| --- | --- |
| `tofu/` | OpenTofu: network (`vpc.tf`: default-VPC lookup, or the dedicated-VPC module behind `use_default_vpc = false`), EKS + node group (`eks.tf`), the four ECR repositories (`ecr.tf`), nightly schedule (`scheduler.tf`), S3 backend with no account values (`backend.tf`), variables, outputs, `terraform.tfvars.example`, committed lock file. |
| `k8s/demo-values.yaml` | Static Helm values for the demo chart: backends off, chatbot off, gateway collector → ClickStack with the token from a Secret, the traces-pipeline transforms, and the assistant env for the frontend, agent and MCP services. Account-specific values (image repositories and tags) and the Langfuse pipeline are generated at deploy time into `.runtime/eks/values.generated.yaml`. |
| `k8s/clickstack-collector.yaml` | Namespace, Deployment and Service for the ClickStack collector; credentials come from the Secret `demo.py eks deploy` creates. |
| `k8s/network-policy.yaml` | The agent's ingress policy: only the frontend pod may reach port 8010, so nothing else in `otel-demo` can spend the model credential. Applied by `demo.py eks deploy` with the namespaces, parsed offline by `eks check`, and enforced only because the `vpc-cni` add-on carries `enableNetworkPolicy`. |
| `sql/create-user.sql` | Creates the `clickstack` ClickHouse user with grants on `otel`. Run it once in the ClickHouse Cloud SQL console with a real password in place of `SECURE_PASSWORD`; the collector creates the tables. |

The Python that drives all of it is in `launcher/eks/` at the repository root,
one module per concern (`infra`, `lifecycle`, `tunnel`, `flags`, `ops`,
`values`, `check`, `aws`, `k8s`).

## Costs

Everything is on-demand in us-east-1; list prices at the time of writing.

| State | What is billed | About |
| --- | --- | --- |
| **Idle** (node group at 0) | EKS control plane at $0.10/hour | **~$73/month** |
| **Up** (2 × m7g.xlarge) | Control plane $0.10/hour + two nodes at about $0.33/hour | **~$0.43/hour**, so roughly $10/day if forgotten |
| Always | S3 state, ECR image storage (a lifecycle policy keeps the 5 most recent per repository), the two nodes' public IPv4 addresses and 50 GiB gp3 root volumes while up | Cents |

There is no NAT gateway, no load balancer, no CloudWatch control-plane logs and
no KMS key, by design.

The nightly schedule bounds the "up" cost: EventBridge Scheduler scales the node
group to 0 every night at `scale_down_hour` in `scale_down_timezone` (defaults
23:00 `America/New_York`), so a demo nobody took down costs at most one day of
nodes. Two ways to change that:

- **For one evening**: `demo.py eks nightly off` before a demo that runs past
  the hour, `demo.py eks nightly on` afterwards. This flips the schedule's state
  in AWS without touching OpenTofu; the next `demo.py eks apply` puts it back to
  whatever `nightly_scale_down_enabled` says (default `true`), which is the
  intended cost-guard behaviour.
- **Permanently**: set `scale_down_hour` and `scale_down_timezone` (and, if you
  really want, `nightly_scale_down_enabled = false`) in `tofu/terraform.tfvars`
  and run `demo.py eks apply`.

Cheaper nodes: `instance_type = "m7g.large"` in `tofu/terraform.tfvars` halves
the node cost; the demo still fits on two of them, with a slower start. If you
go smaller than that, add `telemetry-docs: {enabled: false}` to
`k8s/demo-values.yaml` — it is the one remaining component the walkthrough never
opens. The agent and MCP services cannot be trimmed; they are the assistant.

When the demo is over for good, `demo.py eks destroy` removes everything
(`--purge-state` also deletes the state bucket). `down` is the nightly state;
`destroy` is the end.

## Recovery

### The tunnel died

`demo.py eks tunnel` stops whatever is left and starts a new one;
`demo.py eks tunnel status` exits 0 only when the storefront answers. A 503 from
Envoy means the tunnel is fine and the frontend is still starting.

Low-level: if port 8080 is held by something else, `lsof -iTCP:8080 -sTCP:LISTEN`
names it. The laptop Compose stack defaults to the same port, so running both at
once needs `SHOP_PORT` or `EKS_TUNNEL_PORT` changed in `.env`.

### A command says to run `demo.py eks apply` first

Every subcommand reads the cluster name and friends from the OpenTofu outputs in
the S3 backend. On a fresh clone or a new laptop that means `demo.py eks init`
has not been run yet (it configures the backend); an expired SSO session gives
the same message with tofu's own reason attached, and `init` or any subcommand
will re-login. If a `tofu apply` was interrupted, re-run `demo.py eks apply`; it
only changes what differs.

### The node group is stuck `UPDATING`

Usually the nightly schedule fired moments ago, and the scaling helper waits for
it.

Low-level, if you are in a hurry:

```sh
aws eks wait nodegroup-active --cluster-name otel-demo-eks --nodegroup-name demo
```

### The state file is lost or corrupted

First try to get it back. `init` turned versioning on, so every previous state
is still in the bucket. **Low-level disaster recovery below: these commands talk
to AWS and OpenTofu directly, on purpose, because the CLI needs working state to
do anything.**

```sh
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
BUCKET=otel-demo-eks-tfstate-$(aws sts get-caller-identity --query Account --output text)
KEY=otel-demo-eks/terraform.tfstate

aws s3api list-object-versions --bucket "$BUCKET" --prefix "$KEY" \
  --query 'Versions[].{VersionId:VersionId,LastModified:LastModified,Size:Size,IsLatest:IsLatest}' --output table
aws s3api copy-object --bucket "$BUCKET" --key "$KEY" \
  --copy-source "$BUCKET/$KEY?versionId=<a good VersionId>"
scripts/demo.py eks init
tofu -chdir=deploy/eks/tofu plan      # should show no (or only expected) changes
```

The bucket and key are not guesses: `demo.py eks init` derives exactly those two
names, and OpenTofu's state for this cluster has never lived anywhere else.

If the bucket itself is gone, or no version is usable, the resources are
orphaned and have to be removed by hand before `demo.py eks init &&
demo.py eks apply` can start over. Every resource has a deterministic name and
carries the tag `Project=otel-demo-eks`, so an inventory is one call:

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

# 3. The four image repositories (--force deletes the images with them)
for svc in frontend frontend-proxy agent mcp; do
  aws ecr delete-repository --force --repository-name "ch-opentelemetry-demo/$svc"
done

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
not tagged and is not created by OpenTofu; `demo.py eks destroy --purge-state`
is what normally removes it, so if you got here by hand, delete it by hand too
(`aws s3api list-object-versions` → `delete-objects` → `delete-bucket`) or keep
it for the next `demo.py eks init`.
