#!/usr/bin/env bash
# One command for the morning: make sure the EKS cluster exists, then `eks start`.
# On a machine or account where `eks init` / `eks apply` have never run, run them first.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
demo() { "$PY" scripts/demo.py "$@"; }

MISSING=10 # distinct from 1, which is what a crash or a `die` exits with

# probe init|apply: exit 0 when the step has been run, $MISSING when it has not.
#   init  - the S3 backend is configured in this checkout (local file, no AWS call)
#   apply - the remote state holds the cluster's outputs (signs in to AWS first)
probe() {
  "$PY" - "$1" "$MISSING" <<'EOF'
import sys

sys.path.insert(0, ".")
from launcher.eks import aws, infra

step, missing = sys.argv[1], int(sys.argv[2])
if step == "init":
    done = infra.initialised()
else:
    aws.aws_login()
    try:
        done = "nodegroup_name" in aws.tf_outputs(refresh=True)
    except SystemExit:  # tf_outputs dies when there is no state to read
        done = False
sys.exit(0 if done else missing)
EOF
}

# `set -e` is suspended inside `if`, so capture the status and only accept 0 or $MISSING.
step_done() {
  local rc=0
  probe "$1" || rc=$?
  case "$rc" in
    0) return 0 ;;
    "$MISSING") return 1 ;;
    *) echo "go.sh: could not check whether 'eks $1' has run (exit $rc)" >&2; exit "$rc" ;;
  esac
}

if ! step_done init; then
  echo "==> 'eks init' has not been run in this checkout; running it now"
  demo eks init
fi

if ! step_done apply; then
  cat <<'EOF'

==============================================================================
  No EKS cluster found in the OpenTofu state: running a FULL DEPLOY.

  eks apply (~15 min: control plane, node group, ECR, schedule)
  -> build + publish the four images (several minutes, cold cache)
  -> eks up (Secrets, collector, demo release, tunnel) -> eks verify

  This creates billable AWS resources (control plane ~$73/month, nodes
  ~$0.33/hour while up). Ctrl-C now to abort.
==============================================================================

EOF
  demo eks apply --yes
fi

demo eks start
