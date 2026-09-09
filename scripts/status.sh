#!/usr/bin/env bash
# Show where the demo stands: who you are, whether the nodes are up, what is
# running, whether the tunnel and the ECR image are in place, and what the
# nightly scale-down schedule will do. Exits 0 in both the idle and the up
# state; a non-zero exit means something is actually broken (auth, state).
#
#   ./demo.sh status
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

if [[ $# -gt 0 ]]; then
  echo "usage: ./demo.sh status" >&2
  exit 1
fi

aws_login

log "identity"
aws sts get-caller-identity --query '{Account: Account, Arn: Arn}' --output table

log "node group"
# Resolved into variables first: a failing substitution inside an echo
# argument would print an empty field and carry on.
DESIRED="$(ng_desired)"
STATUS="$(ng_status)"
echo "    desiredSize=$DESIRED status=$STATUS"

log "cluster"
kubeconfig
if ((DESIRED > 0)); then
  kubectl get nodes
else
  echo "    scaled to zero (./demo.sh up to start)"
fi

log "pods by phase (all namespaces)"
kubectl get pods -A --no-headers -o custom-columns=P:.status.phase | sort | uniq -c

log "helm releases in $NS_DEMO"
helm list -n "$NS_DEMO"

log "tunnel"
tunnel_status || true

log "frontend image in ECR"
TAG="$(frontend_tag)"
if aws ecr describe-images --repository-name "$ECR_REPO_NAME" --image-ids "imageTag=$TAG" >/dev/null 2>&1; then
  echo "    $ECR_REPO_NAME:$TAG present"
else
  echo "    $ECR_REPO_NAME:$TAG absent (./demo.sh build-frontend)"
fi

log "nightly scale-down schedule"
SCHEDULER_NAME="$(tf_out scheduler_name)"
aws scheduler get-schedule --name "$SCHEDULER_NAME" \
  --query '{State: State, ScheduleExpression: ScheduleExpression, Timezone: ScheduleExpressionTimezone}' \
  --output table
