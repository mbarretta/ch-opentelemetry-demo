#!/usr/bin/env bash
# Manage the local port-forward to the storefront (http://localhost:8080).
#
#   ./demo.sh tunnel           (re)start: stop any running tunnel, then start one
#   ./demo.sh tunnel stop      stop it
#   ./demo.sh tunnel status    report whether it is up; exit 0 if so, 1 if not
#
# deploy opens the tunnel for you; use this when it has died (laptop sleep,
# expired SSO session) or to take it down without scaling the cluster.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

usage() {
  cat >&2 <<'USAGE'
usage: ./demo.sh tunnel [stop|status]

  (none)   stop any running tunnel, then start one
  stop     stop the tunnel
  status   print the tunnel state; exit 0 when up, 1 otherwise
USAGE
}

case "${1:-}" in
  "")
    # Starting needs an AWS session (the restart loop re-runs `aws sso login`
    # with $AWS_PROFILE) and the right kubectl context; stopping does not.
    need aws kubectl lsof curl
    aws_login
    kubeconfig
    tunnel_stop
    tunnel_start
    ;;
  stop)
    tunnel_stop
    ;;
  status)
    if tunnel_status; then exit 0; else exit 1; fi
    ;;
  *)
    usage
    exit 1
    ;;
esac
