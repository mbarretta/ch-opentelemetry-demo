#!/usr/bin/env bash
# Take the demo down to the idle state: close the tunnel, remove the workloads
# and scale the node group to zero. The EKS control plane, the ECR image and
# the OpenTofu state stay, so ./demo.sh up brings it back in minutes.
#
#   ./demo.sh down           uninstall the demo and collector, then scale to 0
#   ./demo.sh down --keep    scale to 0 but leave the workloads in the API;
#                            they reschedule when the nodes return
#
# Telemetry already written to ClickHouse Cloud is never touched.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

usage() {
  echo "usage: ./demo.sh down [--keep]" >&2
  exit 1
}

KEEP=0
case "${1:-}" in
  "") ;;
  --keep) KEEP=1 ;;
  *) usage ;;
esac
[[ $# -le 1 ]] || usage

# Local only, and a no-op when nothing is running.
tunnel_stop

aws_login

if ((KEEP)); then
  log "--keep: leaving the workloads in place"
else
  kubeconfig
  # Demo first, collector second: the demo produces the telemetry, so taking
  # it down first spares the collector a burst of export failures. Only the
  # demo is a Helm release; the collector namespace is deleted directly.
  log "helm uninstall $RELEASE"
  helm uninstall "$RELEASE" -n "$NS_DEMO" --ignore-not-found --wait --timeout 5m
  log "deleting namespaces $NS_DEMO and $NS_CS"
  kubectl delete ns "$NS_DEMO" "$NS_CS" --ignore-not-found --timeout=300s
fi

ng_scale 0

echo
log "demo is down: node group at 0, control plane kept. Telemetry in ClickHouse Cloud is untouched."
echo "Bring it back with: ./demo.sh up"
