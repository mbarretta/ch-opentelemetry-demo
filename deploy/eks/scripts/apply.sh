#!/usr/bin/env bash
# Create or update the AWS infrastructure: EKS cluster, node group, ECR
# repository and the nightly scale-down schedule, in the account default VPC
# (a dedicated VPC is created only when use_default_vpc = false).
#
#   ./demo.sh apply          show the plan and ask before applying
#   ./demo.sh apply --yes    apply without the confirmation prompt
#
# The first apply takes 10-15 minutes (the EKS control plane is the slow part).
# Re-running is safe: OpenTofu only changes what differs.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

usage() {
  echo "usage: ./demo.sh apply [--yes]" >&2
  exit 1
}

AUTO_APPROVE=""
case "${1:-}" in
  "") ;;
  --yes|-y) AUTO_APPROVE=-auto-approve ;;
  *) usage ;;
esac
[[ $# -le 1 ]] || usage

need aws tofu kubectl

# `tofu apply` on an uninitialised backend fails with a generic message; the
# fix is always the same, so name it. The backend-state file is the marker:
# tofu/.terraform/ alone also appears after check.sh's `init -backend=false`.
[[ -f tofu/.terraform/terraform.tfstate ]] \
  || die "OpenTofu backend not initialised: run ./demo.sh init first"

aws_login

log "tofu apply"
# Not `-input=false`: that would also refuse the interactive approval prompt.
# Unquoted on purpose so an empty value contributes no argument.
# shellcheck disable=SC2086
tofu -chdir=tofu apply $AUTO_APPROVE

log "kubeconfig"
kubeconfig
# The node group starts at desiredSize = node_count, but kubelets can take a
# minute or two to register after apply returns; an empty list here is normal.
kubectl get nodes -o wide

echo
log "tofu output"
tofu -chdir=tofu output

cat <<'MSG'

Next:
  ./demo.sh build-frontend   build the session-replay frontend image and push it to ECR (once)
  ./demo.sh deploy           install the ClickStack collector and the demo, then open the tunnel
MSG
