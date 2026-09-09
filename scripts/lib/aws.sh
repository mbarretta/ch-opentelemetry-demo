#!/usr/bin/env bash
# AWS helpers: SSO login, OpenTofu outputs, node-group scaling.
# Source after lib/common.sh. Callers own `set -euo pipefail`.

# Establish an authenticated session for the IAM Identity Center profile named
# by AWS_PROFILE (from envvars.aws or the environment). Exports AWS_PROFILE and
# AWS_REGION so every aws/tofu/kubectl call in the script, and the exec-auth
# plugin kubectl spawns, picks them up.
aws_login() {
  load_aws_env
  [[ -n "${AWS_PROFILE:-}" ]] || die "AWS_PROFILE is unset: cp envvars.aws.example envvars.aws and set it"
  export AWS_PROFILE
  export AWS_REGION="${AWS_REGION:-us-east-1}"

  # Captured rather than piped: under pipefail an early grep exit could make a
  # SIGPIPE'd `aws` look like a missing profile.
  local profiles
  profiles="$(aws configure list-profiles)"
  if ! grep -qx -- "$AWS_PROFILE" <<<"$profiles"; then
    die "AWS profile '$AWS_PROFILE' is not configured; create it with: aws configure sso --profile \"$AWS_PROFILE\""
  fi

  # SSO sessions expire (typically 8-12 h); log in again only when the cached
  # credentials no longer work. `aws sso login` opens a browser.
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    log "no valid session for profile $AWS_PROFILE; running aws sso login"
    aws sso login --profile "$AWS_PROFILE"
    aws sts get-caller-identity >/dev/null 2>&1 \
      || die "still not authenticated as profile $AWS_PROFILE after aws sso login"
  fi
}

# tf_out <name> -- one OpenTofu output, raw. The state lives in the S3 backend
# configured by init, so this needs a working AWS session (aws_login first).
tf_out() {
  local v err
  err="$(mktemp)"
  if ! v="$(tofu -chdir="$REPO_ROOT/tofu" output -raw "$1" 2>"$err")"; then
    # Keep tofu's own reason (expired session, unreachable backend, unknown
    # output) next to the hint, so "no state" is not the only diagnosis offered.
    local reason
    reason="$(grep -v '^\s*$' "$err" | tail -n 1 | sed 's/^[[:space:]]*//')"
    rm -f "$err"
    die "no OpenTofu output '$1'${reason:+ ($reason)}: run ./demo.sh apply first"
  fi
  rm -f "$err"
  printf '%s\n' "$v"
}

# --- managed node group --------------------------------------------------------

_ng_describe() {
  # Resolved into locals first: a failing substitution inside an argument list
  # does not stop the script, so aws would otherwise run with empty names.
  local cluster ng
  cluster="$(tf_out cluster_name)"
  ng="$(tf_out nodegroup_name)"
  aws eks describe-nodegroup \
    --cluster-name "$cluster" --nodegroup-name "$ng" \
    --query "$1" --output text
}

ng_desired() { _ng_describe 'nodegroup.scalingConfig.desiredSize'; }
ng_status()  { _ng_describe 'nodegroup.status'; }

# ng_scale <n> -- set the node group's desired size and wait until it is ACTIVE.
ng_scale() {
  local n="$1" cluster ng NODE_MAX
  cluster="$(tf_out cluster_name)"
  ng="$(tf_out nodegroup_name)"
  NODE_MAX="$(tf_out node_count)"

  # A scaling request against a node group that is still UPDATING (for example
  # right after the nightly schedule fired) is rejected with
  # ResourceInUseException, so let the previous update settle first.
  if [[ "$(ng_status)" == UPDATING ]]; then
    log "node group $ng is UPDATING; waiting for it to become ACTIVE"
    aws eks wait nodegroup-active --cluster-name "$cluster" --nodegroup-name "$ng"
  fi

  # EKS rejects an update that changes nothing, so a no-op has to be skipped.
  if [[ "$(ng_desired)" == "$n" ]]; then
    log "node group $ng already has desiredSize=$n"
    return 0
  fi

  log "scaling node group $ng to desiredSize=$n (minSize=0, maxSize=$NODE_MAX)"
  aws eks update-nodegroup-config \
    --cluster-name "$cluster" --nodegroup-name "$ng" \
    --scaling-config "minSize=0,maxSize=$NODE_MAX,desiredSize=$n" >/dev/null
  aws eks wait nodegroup-active --cluster-name "$cluster" --nodegroup-name "$ng"
}
