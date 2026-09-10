#!/usr/bin/env bash
# Tear everything down: workloads, node group, cluster, ECR repository, the
# nightly schedule and, only when use_default_vpc = false, the dedicated VPC
# (the account default VPC is never touched). This is the end of the demo, not
# the nightly idle state -- for that use ./demo.sh down.
#
#   ./demo.sh destroy                 tofu destroy (asks for confirmation)
#   ./demo.sh destroy --purge-state   ... and then delete the state bucket too
#
# Without --purge-state the (empty) state bucket stays so a later
# ./demo.sh init && ./demo.sh apply reuses it.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

usage() {
  echo "usage: ./demo.sh destroy [--purge-state]" >&2
  exit 1
}

PURGE=0
case "${1:-}" in
  "") ;;
  --purge-state) PURGE=1 ;;
  *) usage ;;
esac
[[ $# -le 1 ]] || usage

# kubectl and helm are not listed: only the courtesy down.sh below uses them,
# it guards itself, and its failure is tolerated.
need aws tofu jq

# Delete every object version and delete marker, then the bucket. Versioning
# is on (init.sh), so a plain `aws s3 rm --recursive` would leave the old
# versions behind and delete-bucket would fail with BucketNotEmpty.
purge_state_bucket() {
  local bucket="$1" listing batch n
  while :; do
    listing="$(aws s3api list-object-versions --bucket "$bucket" --output json)"
    # delete-objects takes at most 1000 keys per call; loop until empty.
    batch="$(jq -c '{Objects: ([.Versions[]?, .DeleteMarkers[]?] | map({Key, VersionId}) | .[:1000]), Quiet: true}' <<<"$listing")"
    n="$(jq '.Objects | length' <<<"$batch")"
    ((n > 0)) || break
    aws s3api delete-objects --bucket "$bucket" --delete "$batch" >/dev/null
  done
  aws s3api delete-bucket --bucket "$bucket"
}

tunnel_stop

aws_login

# Run the normal down sequence first so the release, namespaces and nodes go
# away in order. It is a courtesy, not a requirement (nothing here creates an
# ELB or other out-of-band resource that would block the destroy), so a missing
# cluster or a half-torn-down one must not stop the destroy.
# The guard sits outside the substitution: tf_out's die exits the subshell
# before any `|| true` inside it could run.
cluster="$(tf_out cluster_name 2>/dev/null)" || cluster=""
if [[ -n "$cluster" ]] && aws eks describe-cluster --name "$cluster" >/dev/null 2>&1; then
  log "cluster $cluster exists; running down first"
  "$REPO_ROOT/scripts/down.sh" \
    || echo "warning: down did not complete cleanly; continuing with tofu destroy" >&2
else
  log "no reachable cluster in state; skipping down"
fi

log "tofu destroy"
tofu -chdir=tofu destroy

if ((PURGE)); then
  BUCKET="$(state_bucket)"
  if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    log "purging state bucket s3://$BUCKET"
    purge_state_bucket "$BUCKET"
    # The local backend config still points at the deleted bucket.
    rm -rf tofu/.terraform
    echo "State bucket deleted. To start over: ./demo.sh init && ./demo.sh apply"
  else
    log "state bucket s3://$BUCKET does not exist; nothing to purge"
  fi
fi

echo
log "destroy complete"
