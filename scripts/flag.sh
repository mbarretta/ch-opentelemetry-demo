#!/usr/bin/env bash
# Read and flip the demo's flagd feature flags without restarting anything.
#
#   ./demo.sh flag                            list every flag + current variant
#   ./demo.sh flag paymentUnreachable         show one flag and its variants
#   ./demo.sh flag paymentUnreachable on      set the variant
#   ./demo.sh flag --reset                    restore every flag to chart defaults
#
# Why it pokes at a file inside a pod rather than the ConfigMap: flagd never
# reads `cm/flagd-config` directly. An init container copies it once into an
# emptyDir shared with flagd-ui, and flagd fsnotify-watches that copy. Patching
# the ConfigMap therefore changes nothing until the pod restarts -- and the
# restart discards every toggle made here. That restart is what --reset does.
#
# The edit runs in the flagd-ui container because the flagd container is
# distroless and has no shell; both mount the same volume.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/common.sh
. scripts/lib/common.sh
# shellcheck source=scripts/lib/aws.sh
. scripts/lib/aws.sh
# shellcheck source=scripts/lib/k8s.sh
. scripts/lib/k8s.sh

need aws tofu kubectl jq
aws_login
kubeconfig

FLAG_FILE=/app/data/demo.flagd.json

kexec() { kubectl -n "$NS_DEMO" exec deploy/flagd -c flagd-ui -- "$@"; }

if [[ "${1:-}" == "--reset" ]]; then
  log "Restarting flagd -- all flags return to the cm/flagd-config defaults"
  kubectl -n "$NS_DEMO" rollout restart deploy/flagd
  kubectl -n "$NS_DEMO" rollout status deploy/flagd --timeout=120s
  exit 0
fi

FLAG="${1:-}"
VARIANT="${2:-}"

# Pull the live file once; parse it on the host with jq.
CONFIG=$(kexec cat "$FLAG_FILE")

# Prints "<flag>\t<defaultVariant>\t<variant,variant,...>" so the shell can
# read it without needing a JSON parser of its own. With no argument every
# flag is listed, sorted by name; with one, only that flag, after checking it
# exists so a typo is reported by name rather than as an empty line.
describe() {
  local want="${1:-}"
  if [[ -n "$want" ]] && ! jq -e --arg f "$want" '.flags | has($f)' <<<"$CONFIG" >/dev/null; then
    {
      echo "no such flag: $want"
      echo
      echo "known flags:"
      jq -r '.flags | keys[] | "  " + .' <<<"$CONFIG"
    } >&2
    return 1
  fi
  # variants is a JSON object (name -> value), so its keys are the valid
  # variant names, listed in file order.
  jq -r --arg f "$want" '
    .flags | to_entries
    | if $f == "" then sort_by(.key) else map(select(.key == $f)) end
    | .[] | [.key, .value.defaultVariant, (.value.variants | keys_unsorted | join(","))]
    | @tsv' <<<"$CONFIG"
}

if [[ -z "$FLAG" ]]; then
  log "Feature flags currently served by flagd"
  describe | while IFS=$'\t' read -r name current variants; do
    printf '  %-28s %-8s [%s]\n' "$name" "$current" "$variants"
  done
  exit 0
fi

INFO=$(describe "$FLAG")
IFS=$'\t' read -r _ CURRENT VARIANTS <<<"$INFO"

if [[ -z "$VARIANT" ]]; then
  printf '%s is "%s" (available: %s)\n' "$FLAG" "$CURRENT" "${VARIANTS//,/, }"
  exit 0
fi

# Reject an unknown variant here; flagd would otherwise accept the file and
# silently fall back, which is a confusing thing to debug mid-demo.
if [[ ",$VARIANTS," != *",$VARIANT,"* ]]; then
  die "'$VARIANT' is not a variant of $FLAG (available: ${VARIANTS//,/, })"
fi

if [[ "$VARIANT" == "$CURRENT" ]]; then
  echo "$FLAG is already \"$VARIANT\""
  exit 0
fi

# Rewrite only the defaultVariant line inside this flag's block. sed -i swaps
# the inode, so flagd logs REMOVE rather than WRITE -- it re-arms the watch and
# re-syncs regardless. FLAG and VARIANT were both validated against the file
# above, so nothing unchecked reaches the sed expression.
kexec sed -i "/\"$FLAG\"/,/^    }/ s|\"defaultVariant\": *\"[^\"]*\"|\"defaultVariant\": \"$VARIANT\"|" "$FLAG_FILE"

# Read the file back rather than trusting the write. describe() closes over
# CONFIG, so refresh it first or the check just re-reads the pre-edit copy.
sleep 2
CONFIG=$(kexec cat "$FLAG_FILE")
IFS=$'\t' read -r _ NOW _ <<<"$(describe "$FLAG")"
if [[ "$NOW" != "$VARIANT" ]]; then
  die "write did not take: $FLAG is still \"$NOW\""
fi

echo "$FLAG: $CURRENT -> $NOW"
