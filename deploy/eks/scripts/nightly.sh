#!/usr/bin/env bash
# Enable or disable the nightly scale-to-zero schedule (EventBridge Scheduler).
# Turn it off before a demo that runs past the scheduled hour; turn it back on
# afterwards so a forgotten cluster does not bill for nodes all night.
#
#   ./demo.sh nightly on
#   ./demo.sh nightly off
#
# The hour and timezone themselves are OpenTofu variables (scale_down_hour,
# scale_down_timezone); change those with ./demo.sh apply.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh

usage() {
  echo "usage: ./demo.sh nightly on|off" >&2
  exit 1
}

case "${1:-}" in
  on)  STATE=ENABLED ;;
  off) STATE=DISABLED ;;
  *) usage ;;
esac
[[ $# -eq 1 ]] || usage

need aws tofu jq

aws_login

NAME="$(tf_out scheduler_name)"

# update-schedule replaces the whole schedule, so everything not passed is
# reset. Read the current definition and feed it back with only State changed.
current="$(aws scheduler get-schedule --name "$NAME" --output json)"
expression="$(jq -r '.ScheduleExpression' <<<"$current")"
timezone="$(jq -r '.ScheduleExpressionTimezone' <<<"$current")"
window="$(jq -c '.FlexibleTimeWindow' <<<"$current")"
target="$(jq -c '.Target' <<<"$current")"
description="$(jq -r '.Description // ""' <<<"$current")"

log "setting schedule $NAME to $STATE"
# Unquoted on purpose so an empty description contributes no argument.
# shellcheck disable=SC2086
aws scheduler update-schedule --name "$NAME" \
  --schedule-expression "$expression" \
  --schedule-expression-timezone "$timezone" \
  --flexible-time-window "$window" \
  --target "$target" \
  --state "$STATE" \
  ${description:+--description "$description"} >/dev/null

aws scheduler get-schedule --name "$NAME" \
  --query '{State: State, ScheduleExpression: ScheduleExpression, Timezone: ScheduleExpressionTimezone}' \
  --output table
