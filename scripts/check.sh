#!/usr/bin/env bash
# Offline sanity check for the repo. This is the harness verification command.
#
#   ./scripts/check.sh
#
# Runs every check whose inputs exist: bash syntax on all scripts, shellcheck
# when installed, `tofu fmt/init/validate` when tofu/ has .tf files, a
# Kubernetes dry-run of the collector manifest, and a `helm template` of the
# demo chart against k8s/demo-values.yaml. Needs no AWS credentials and no
# cluster; it may download pinned OpenTofu providers and the demo chart.
set -euo pipefail

cd "$(dirname "$0")/.."

step() { echo "==> $*"; }

# --- bash -n -------------------------------------------------------------------

step "bash -n on every *.sh"
scripts=()
while IFS= read -r -d '' f; do
  scripts+=("$f")
done < <(find . \( -path ./.git -o -path ./opentelemetry-demo -o -path ./.run -o -path ./tofu/.terraform \) -prune \
              -o -type f -name '*.sh' -print0 | sort -z)
for f in "${scripts[@]}"; do
  bash -n "$f"
done
echo "    ${#scripts[@]} file(s)"

# Constants (CHART_VERSION, RELEASE) come from the library the scripts share, so
# the check renders the same chart version deploy installs. Sourced only after
# the syntax pass so a broken library is reported as such.
# shellcheck source=scripts/lib/common.sh
. scripts/lib/common.sh

# --- shellcheck ----------------------------------------------------------------

if command -v shellcheck >/dev/null 2>&1; then
  step "shellcheck"
  shellcheck -x "${scripts[@]}"
else
  step "shellcheck (skipped: not installed; brew install shellcheck)"
fi

# --- opentofu ------------------------------------------------------------------

if compgen -G "tofu/*.tf" >/dev/null; then
  # Providers are large (the AWS provider alone is several hundred MB); cache
  # them across runs and worktrees.
  export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
  mkdir -p "$TF_PLUGIN_CACHE_DIR"

  step "tofu fmt -check"
  tofu -chdir=tofu fmt -check
  step "tofu init -backend=false"
  tofu -chdir=tofu init -backend=false -input=false >/dev/null
  step "tofu validate"
  tofu -chdir=tofu validate
fi

# --- kubernetes manifest -------------------------------------------------------

if [[ -f k8s/clickstack-collector.yaml ]]; then
  # `kubectl apply --dry-run=client` still needs a live API server for schema
  # validation and resource discovery, so it only runs when one answers. With
  # no cluster (the normal case for this check) `kubectl kustomize` gives the
  # offline equivalent: it parses every document and requires apiVersion, kind
  # and metadata.name on each.
  if kubectl version --request-timeout=3s >/dev/null 2>&1; then
    step "kubectl apply --dry-run=client -f k8s/clickstack-collector.yaml"
    kubectl apply --dry-run=client -f k8s/clickstack-collector.yaml
  else
    step "kubectl kustomize k8s/clickstack-collector.yaml (no cluster reachable; offline parse)"
    kdir="$(mktemp -d)"
    trap 'rm -rf "$kdir"' EXIT
    cp k8s/clickstack-collector.yaml "$kdir/manifest.yaml"
    printf 'resources:\n  - manifest.yaml\n' >"$kdir/kustomization.yaml"
    kubectl kustomize "$kdir" >/dev/null
  fi
fi

# --- helm values ---------------------------------------------------------------

if [[ -f k8s/demo-values.yaml ]]; then
  step "helm template $RELEASE open-telemetry/opentelemetry-demo --version $CHART_VERSION"
  if ! helm repo list -o json 2>/dev/null | jq -e 'map(select(.name == "open-telemetry")) | length > 0' >/dev/null; then
    helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null
  fi
  # The real repository and tag are account-specific `--set`s at deploy time;
  # any non-empty pair lets the chart render here.
  helm template "$RELEASE" open-telemetry/opentelemetry-demo --version "$CHART_VERSION" \
    -f k8s/demo-values.yaml \
    --set components.frontend.imageOverride.repository=example/frontend \
    --set components.frontend.imageOverride.tag=check >/dev/null
fi

echo OK
