#!/usr/bin/env bash
# Deploy the ClickStack collector and the OpenTelemetry demo onto the running
# cluster, then open the local tunnel to the storefront.
#
#   ./demo.sh deploy
#
# Expects the node group to be scaled up (./demo.sh up does that and then runs
# this). Safe to re-run: every step is an apply or an upgrade. Secrets are
# created straight from envvars.clickhouse through a pipe -- no rendered file
# with credentials ever touches disk.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

usage() { echo "usage: ./demo.sh deploy" >&2; }

if (($# > 0)); then
  usage
  exit 1
fi

need aws tofu kubectl helm

aws_login
kubeconfig
load_clickhouse_env

# Deploying onto zero nodes would leave every pod Pending until the 20-minute
# helm --wait gives up. Refuse early instead.
if [[ "$(ng_desired)" == 0 ]]; then
  die "the node group is scaled to 0; run ./demo.sh up (it scales up and then deploys)"
fi

# `create ... --dry-run=client -o yaml | apply -f -` is the idempotent spelling
# of create: it succeeds whether or not the object already exists.
log "ensuring namespaces $NS_CS and $NS_DEMO"
kubectl create ns "$NS_CS" --dry-run=client -o yaml | kubectl apply -f -
kubectl create ns "$NS_DEMO" --dry-run=client -o yaml | kubectl apply -f -

# The five KEY=VALUE lines of envvars.clickhouse become the five keys of the
# Secret (which is why that file must be plain KEY=VALUE: quotes would end up
# inside the values). k8s/clickstack-collector.yaml reads them via envFrom.
log "creating Secret $NS_CS/clickstack-credentials from envvars.clickhouse"
kubectl -n "$NS_CS" create secret generic clickstack-credentials \
  --from-env-file=envvars.clickhouse --dry-run=client -o yaml | kubectl apply -f -

log "deploying the ClickStack OTel collector"
kubectl apply -f k8s/clickstack-collector.yaml
kubectl -n "$NS_CS" rollout status deploy/clickstack-otel-collector --timeout=300s
log "ClickStack collector logs (last 40 lines)"
kubectl -n "$NS_CS" logs deploy/clickstack-otel-collector --tail=40

# The demo's gateway collector authenticates to the ClickStack collector with
# this token; k8s/demo-values.yaml injects it with extraEnvsFrom and
# ${env:OTLP_AUTH_TOKEN}, so the token never appears in the chart values.
log "creating Secret $NS_DEMO/clickstack-otlp-token"
kubectl -n "$NS_DEMO" create secret generic clickstack-otlp-token \
  --from-literal=OTLP_AUTH_TOKEN="$OTLP_AUTH_TOKEN" --dry-run=client -o yaml | kubectl apply -f -

# Session replay is unconditional: the upstream frontend image has no ClickStack
# browser SDK in its bundle and no runtime hook to add one, so the patched image
# is a prerequisite. Its tag is a pure function of the pinned demo commit and
# the patch, so "does ECR hold that tag" is the whole up-to-date check.
TAG="$(frontend_tag)"
if aws ecr describe-images --repository-name "$ECR_REPO_NAME" \
     --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  log "frontend image $ECR_REPO_NAME:$TAG is in ECR"
else
  log "frontend image $ECR_REPO_NAME:$TAG is not in ECR; building it (first run is slow)"
  scripts/build-frontend.sh
fi

ECR_REPO_URL="$(tf_out ecr_repository_url)"

log "installing the OpenTelemetry demo (chart $CHART_VERSION)"
helm_repo_ensure
# Helm 4's --wait uses the kstatus watcher, which is stricter than Helm 3's
# readiness poll (it also waits on Jobs and custom resources). If it ever
# stalls on a resource that is in fact fine, `--wait=legacy` restores the old
# behaviour. --timeout 20m covers the first-time image pulls on fresh nodes.
helm upgrade --install "$RELEASE" "$HELM_REPO_NAME/opentelemetry-demo" --version "$CHART_VERSION" \
  -n "$NS_DEMO" -f k8s/demo-values.yaml \
  --set components.frontend.imageOverride.repository="$ECR_REPO_URL" \
  --set components.frontend.imageOverride.tag="$TAG" \
  --wait --timeout 20m

# tunnel_start dies on hard failures (port taken, loop exited) and returns 1
# with its own warning when the loop is up but the URL has not answered yet.
# The loop keeps retrying, so the URLs are still worth printing in that case.
tunnel_start || true

echo
echo "Storefront:      http://localhost:8080"
echo "Feature flags:   http://localhost:8080/feature/"
echo "Load generator:  http://localhost:8080/loadgen/"
echo
echo "Check telemetry is landing with ./demo.sh verify; reopen the tunnel with ./demo.sh tunnel."
