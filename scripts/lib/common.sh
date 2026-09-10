#!/usr/bin/env bash
# Shared helpers and constants. Sourced by every scripts/*.sh, never executed:
#
#   cd "$(dirname "$0")/.."
#   . scripts/lib/common.sh
#   . scripts/lib/aws.sh     # when the script talks to AWS
#   . scripts/lib/k8s.sh     # when it talks to the cluster
#
# The callers own `set -euo pipefail`; this file only defines things, apart
# from creating the two scratch directories below (RUN_DIR, TF_PLUGIN_CACHE_DIR).

# --- output --------------------------------------------------------------------

log() { printf '==> %s\n' "$*"; }

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# need <cmd>...  -- die once, naming every missing command rather than the first.
need() {
  local c missing=()
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  ((${#missing[@]} == 0)) || die "missing required command(s): ${missing[*]}"
}

# --- paths ---------------------------------------------------------------------

# Absolute, so helpers work no matter which directory the caller ran from.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Scratch state that must survive between invocations (tunnel pidfile and log,
# cached tofu outputs). Gitignored.
RUN_DIR="$REPO_ROOT/.run"
mkdir -p "$RUN_DIR"

# OpenTofu providers are large (the AWS provider alone is several hundred MB);
# cache them across runs and worktrees so they download once per machine rather
# than once per checkout. Exported here so check.sh and init.sh share one cache.
export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
mkdir -p "$TF_PLUGIN_CACHE_DIR"

# --- constants -----------------------------------------------------------------

# Chart and demo source are pinned together: the session-replay patch is
# generated against DEMO_REF, and a chart bump can move the frontend image the
# patch expects. Re-check `git apply --check` when bumping either.
CHART_VERSION=0.41.0
HELM_REPO_NAME=open-telemetry
HELM_REPO_URL=https://open-telemetry.github.io/opentelemetry-helm-charts
RELEASE=otel-demo
NS_DEMO=otel-demo
NS_CS=clickstack
DEMO_REF=d6fd782e
SDK_PATCH=patches/frontend-session-replay.patch
ECR_REPO_NAME=otel-demo-frontend

# --- environment files ---------------------------------------------------------

# envvars.clickhouse is fed verbatim to `kubectl create secret --from-env-file`
# by deploy, so it must be plain KEY=VALUE lines: no quotes, no `export`. The
# same file is sourced here, which is why it also has to be valid shell.
load_clickhouse_env() {
  local f="$REPO_ROOT/envvars.clickhouse" k
  [[ -f "$f" ]] || die "envvars.clickhouse not found: cp envvars.clickhouse.example envvars.clickhouse and fill it in"
  set -a
  # shellcheck disable=SC1090
  . "$f"
  set +a
  for k in CLICKHOUSE_ENDPOINT CLICKHOUSE_USER CLICKHOUSE_PASSWORD \
           HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE OTLP_AUTH_TOKEN; do
    [[ -n "${!k:-}" ]] || die "$k is empty in envvars.clickhouse"
  done
}

# Optional: AWS_PROFILE / AWS_REGION may equally come from the caller's shell.
load_aws_env() {
  local f="$REPO_ROOT/envvars.aws"
  [[ -f "$f" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  . "$f"
  set +a
}

# --- helm ----------------------------------------------------------------------

# Register the demo chart repository and refresh its index. --force-update makes
# a re-run a no-op instead of "repository name already exists", and it repairs
# the URL when a presenter already has "open-telemetry" registered pointing
# somewhere else -- the case where a bare `helm repo add` exits non-zero.
helm_repo_ensure() {
  helm repo add "$HELM_REPO_NAME" "$HELM_REPO_URL" --force-update >/dev/null
  helm repo update "$HELM_REPO_NAME" >/dev/null
}

# --- frontend image ------------------------------------------------------------

# Deterministic tag for the patched frontend image: demo commit plus a prefix of
# the patch's digest. build-frontend and deploy both compute it, so deploy can
# tell whether the image in ECR matches the patch in the tree without any state.
frontend_tag() {
  [[ -f "$REPO_ROOT/$SDK_PATCH" ]] || die "$SDK_PATCH not found"
  printf '%s-%s\n' "$DEMO_REF" "$(shasum -a 256 "$REPO_ROOT/$SDK_PATCH" | cut -c1-8)"
}
