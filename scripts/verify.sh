#!/usr/bin/env bash
# Confirm telemetry is landing in ClickHouse Cloud.
#
#   ./demo.sh verify
#
# Queries the ClickStack tables over the ClickHouse HTTP interface for rows
# written in the last 15 minutes (traces, logs, metrics, session replay), then
# shows the collector pods and the ClickStack collector's recent logs so a
# zero count can be traced to its cause.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/common.sh
. scripts/lib/common.sh
# shellcheck source=scripts/lib/aws.sh
. scripts/lib/aws.sh
# shellcheck source=scripts/lib/k8s.sh
. scripts/lib/k8s.sh

need aws tofu kubectl curl
load_clickhouse_env
aws_login
kubeconfig

# ClickHouse reads a POST body as the query verbatim, so send it raw rather
# than form-encoded. The credentials reach curl through a config file on a
# process-substitution fd instead of the command line: argv is world-readable
# via ps for as long as the query runs. Curl's config syntax reads the value
# as a double-quoted string, so backslashes and double quotes are escaped.
q() {
  local user=${CLICKHOUSE_USER//\\/\\\\} pass=${CLICKHOUSE_PASSWORD//\\/\\\\}
  user=${user//\"/\\\"} pass=${pass//\"/\\\"}
  printf '%s' "$1" | curl -sS --max-time 60 --data-binary @- \
    -K <(printf 'user = "%s:%s"\n' "$user" "$pass") "${CLICKHOUSE_ENDPOINT}/"
}

DB="$HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE"

log "Rows written in the last 15 minutes to the database: $DB"
q "SELECT 'traces' AS signal, count() FROM ${DB}.otel_traces WHERE Timestamp > now() - INTERVAL 15 MINUTE
   UNION ALL
   SELECT 'logs', count() FROM ${DB}.otel_logs WHERE Timestamp > now() - INTERVAL 15 MINUTE
   UNION ALL
   SELECT 'metrics (sum)', count() FROM ${DB}.otel_metrics_sum WHERE TimeUnix > now() - INTERVAL 15 MINUTE
   UNION ALL
   SELECT 'metrics (gauge)', count() FROM ${DB}.otel_metrics_gauge WHERE TimeUnix > now() - INTERVAL 15 MINUTE
   FORMAT PrettyCompactMonoBlock"

echo
log "Services reporting spans"
q "SELECT ServiceName, count() AS spans FROM ${DB}.otel_traces
   WHERE Timestamp > now() - INTERVAL 15 MINUTE
   GROUP BY ServiceName ORDER BY spans DESC FORMAT PrettyCompactMonoBlock"

echo
# Session replay events from the patched frontend land in their own table;
# the sessionId is a resource attribute, so distinct browser sessions are
# counted from it.
log "Session replay events and distinct sessions (last 15 minutes)"
q "SELECT count(), uniqExact(ResourceAttributes['rum.sessionId']) FROM ${DB}.hyperdx_sessions WHERE Timestamp > now() - INTERVAL 15 MINUTE FORMAT PrettyCompactMonoBlock"

echo
log "Collector pod state"
kubectl -n "$NS_CS" get pods
# Captured first so a kubectl failure stops the script like every other call
# here; only a genuine no-match (namespace without collector pods) gets the
# friendly message instead of a bare grep exit 1.
pods="$(kubectl -n "$NS_DEMO" get pods)"
grep -E 'NAME|otel-collector' <<<"$pods" \
  || echo "    no otel-collector pods in namespace $NS_DEMO"

echo
log "Collector logs (last 40 lines)"
# Address the Deployment, not a pod: kubectl resolves it to a live pod, so the
# rollout-generated pod name (clickstack-otel-collector-5fff8f6b97-7ggzt) never
# has to be hardcoded -- it changes on every redeploy.
kubectl -n "$NS_CS" logs deploy/clickstack-otel-collector --tail=40
