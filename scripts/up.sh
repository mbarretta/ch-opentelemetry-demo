#!/usr/bin/env bash
# Bring the demo up from idle: scale the node group back to node_count, wait
# for the nodes, then deploy (or re-deploy) everything and open the tunnel.
#
#   ./demo.sh up
#
# The reverse is ./demo.sh down. Plan for 5-8 minutes end to end.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

if [[ $# -gt 0 ]]; then
  echo "usage: ./demo.sh up" >&2
  exit 1
fi

# helm is deploy's, not this script's, but deploy runs only after the nodes
# are up and billing; better to learn it is missing before scaling.
need aws tofu kubectl helm

aws_login
kubeconfig

NODE_COUNT="$(tf_out node_count)"
ng_scale "$NODE_COUNT"
wait_nodes_ready "$NODE_COUNT"

# deploy owns the rest (secrets, collector, chart, tunnel); exec so its exit
# status and output are this command's.
exec "$REPO_ROOT/scripts/deploy.sh"
