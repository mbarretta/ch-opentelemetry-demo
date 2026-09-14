#!/usr/bin/env bash
# Entry point for the demo. Dispatches to the matching script under scripts/:
#
#   ./demo.sh <subcommand> [args...]    ->  scripts/<subcommand>.sh [args...]
#   ./demo.sh                           list the available subcommands
#
# Kept deliberately thin: every subcommand is a standalone script that can also
# be run directly, so this file never needs to know what the subcommands do.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "usage: ./demo.sh <subcommand> [args...]"
  echo
  echo "subcommands:"
  local f n found=false
  # check.sh is the repo's own lint/validate step, not a demo action, so it is
  # the one script that does not show up here (run it as ./scripts/check.sh).
  for f in "$here"/scripts/*.sh; do
    [[ -f "$f" ]] || continue
    n="$(basename "$f" .sh)"
    [[ "$n" == check ]] && continue
    echo "  $n"
    found=true
  done
  $found || echo "  (none yet)"
}

cmd="${1:-}"
case "$cmd" in
  ""|-h|--help|help)
    usage >&2
    exit 1
    ;;
esac

target="$here/scripts/$cmd.sh"
if [[ "$cmd" == */* || ! -f "$target" ]]; then
  echo "unknown subcommand: $cmd" >&2
  echo >&2
  usage >&2
  exit 1
fi
if [[ ! -x "$target" ]]; then
  echo "scripts/$cmd.sh exists but is not executable; fix with: chmod +x scripts/$cmd.sh" >&2
  exit 1
fi

shift
exec "$target" "$@"
