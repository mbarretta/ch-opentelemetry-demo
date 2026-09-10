#!/usr/bin/env bash
# One-time host and account setup. Safe to re-run.
#
#   ./demo.sh init
#
# Checks the required tools, logs in to the SSO profile, creates the OpenTofu
# state bucket if it does not exist, initialises the S3 backend, adds the demo
# Helm repository, and reminds you about any envvars.* file still missing.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh

if [[ $# -gt 0 ]]; then
  echo "usage: ./demo.sh init" >&2
  exit 1
fi

# --- tools ---------------------------------------------------------------------

# kubectl and helm are the two most often missing on a fresh Mac and share one
# install line, so they get a specific hint before the generic check.
if ! command -v kubectl >/dev/null 2>&1 || ! command -v helm >/dev/null 2>&1; then
  die "kubectl and/or helm are missing; install with: brew install kubectl helm"
fi
need aws tofu docker jq curl git kubectl helm

# --- aws -----------------------------------------------------------------------

aws_login
log "authenticated as"
aws sts get-caller-identity --output table

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
# One bucket per account, deterministic name so destroy --purge-state (and a
# presenter on another laptop) can find it without any local state.
BUCKET="otel-demo-eks-tfstate-$ACCOUNT_ID"

# --- state bucket --------------------------------------------------------------

if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  log "state bucket s3://$BUCKET already exists"
else
  log "creating state bucket s3://$BUCKET in $AWS_REGION"
  # us-east-1 is the one region that rejects an explicit LocationConstraint.
  if [[ "$AWS_REGION" == us-east-1 ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  fi
fi

# Applied on every run, not only on create: both calls are idempotent, and a
# bucket left behind by an interrupted first run would otherwise stay
# unversioned. Versioning is what makes a clobbered state file recoverable.
log "enabling versioning and blocking public access on s3://$BUCKET"
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# --- opentofu backend ----------------------------------------------------------

# backend.tf declares `backend "s3"` with no bucket/key/region, so the
# account-specific values never land in the repo; they are supplied here.
# TF_PLUGIN_CACHE_DIR (shared with scripts/check.sh) comes from lib/common.sh.
log "tofu init (backend s3://$BUCKET/otel-demo-eks/terraform.tfstate)"
tofu -chdir=tofu init -input=false \
  -backend-config="bucket=$BUCKET" \
  -backend-config="key=otel-demo-eks/terraform.tfstate" \
  -backend-config="region=$AWS_REGION"

# --- helm ----------------------------------------------------------------------

log "helm repo $HELM_REPO_NAME"
helm_repo_ensure

# --- local config --------------------------------------------------------------

missing=0
for f in envvars.clickhouse envvars.aws; do
  if [[ ! -f "$f" ]]; then
    echo "warning: $f is missing; create it with: cp $f.example $f  (then fill it in)" >&2
    missing=1
  fi
done

echo
log "init complete"
if ((missing)); then
  echo "Fill in the envvars.* file(s) above, then run: ./demo.sh apply"
else
  echo "Next: ./demo.sh apply"
fi
