#!/usr/bin/env bash
# Kubernetes helpers: kubeconfig, node readiness, the local port-forward tunnel.
# Source after lib/common.sh and lib/aws.sh (uses tf_out, AWS_PROFILE,
# AWS_REGION, NS_DEMO, RUN_DIR). Callers own `set -euo pipefail`.

# Point kubectl at the cluster. The profile is embedded in the kubeconfig's
# exec-auth entry, so plain `kubectl` works from any shell afterwards.
kubeconfig() {
  local cluster_name
  cluster_name="$(tf_out cluster_name)"
  aws eks update-kubeconfig --region "$AWS_REGION" --name "$cluster_name" \
    --alias "$cluster_name" --profile "$AWS_PROFILE" >/dev/null
  kubectl config use-context "$cluster_name" >/dev/null
}

# wait_nodes_ready <n> -- block until n nodes report Ready (10 min ceiling),
# then until CoreDNS is rolled out. `aws eks wait nodegroup-active` can return
# before the kubelets have registered, which is why this exists.
wait_nodes_ready() {
  local n="$1" deadline=$((SECONDS + 600)) ready
  log "waiting for $n Ready node(s)"
  while :; do
    # kubectl's stderr is left visible on purpose: an auth or kubeconfig
    # failure must not masquerade as slow node startup for ten minutes.
    ready="$(kubectl get nodes --no-headers | awk '$2 == "Ready"' | wc -l | tr -d ' ')" || ready=0
    ((ready >= n)) && break
    ((SECONDS < deadline)) || die "only $ready of $n node(s) Ready after 10 minutes"
    sleep 10
  done
  log "$ready node(s) Ready; waiting for CoreDNS"
  kubectl -n kube-system rollout status deploy/coredns --timeout=300s
}

# --- tunnel --------------------------------------------------------------------
#
# The storefront is reached only through `kubectl port-forward` (no
# LoadBalancer or Ingress). A bare port-forward dies whenever the frontend-proxy
# pod restarts, the API connection drops, or the SSO session expires, so it runs
# inside a small restart loop that re-authenticates when needed.

TUNNEL_PORT=8080
TUNNEL_URL="http://localhost:$TUNNEL_PORT/"
TUNNEL_PIDFILE="$RUN_DIR/port-forward.pid"
TUNNEL_LOG="$RUN_DIR/port-forward.log"

_tunnel_pid() {
  [[ -f "$TUNNEL_PIDFILE" ]] && cat "$TUNNEL_PIDFILE"
}

_pid_alive() {
  [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null
}

_tunnel_probe() {
  # Any HTTP answer means the tunnel is up; a 503 from envoy while the frontend
  # is still starting is not the tunnel's fault, so no -f here.
  curl -sS -o /dev/null --max-time "${1:-2}" "$TUNNEL_URL" 2>/dev/null
}

tunnel_start() {
  local pid i
  pid="$(_tunnel_pid || true)"
  if _pid_alive "$pid"; then
    die "tunnel is already running (pid $pid): $TUNNEL_URL -- use ./demo.sh tunnel stop first"
  fi
  if lsof -iTCP:"$TUNNEL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $TUNNEL_PORT is already in use by another process (lsof -iTCP:$TUNNEL_PORT -sTCP:LISTEN)"
  fi
  rm -f "$TUNNEL_PIDFILE"

  # AWS_PROFILE / AWS_REGION are exported by aws_login and inherited here. The
  # namespace and port are passed as arguments so no value is spliced into the
  # loop's source text.
  nohup bash -c '
    while true; do
      aws sts get-caller-identity >/dev/null 2>&1 || aws sso login --profile "$AWS_PROFILE"
      kubectl -n "$1" port-forward --address 127.0.0.1 svc/frontend-proxy "$2:$2"
      sleep 2
    done' tunnel "$NS_DEMO" "$TUNNEL_PORT" >"$TUNNEL_LOG" 2>&1 &
  echo $! >"$TUNNEL_PIDFILE"
  pid="$(cat "$TUNNEL_PIDFILE")"

  for ((i = 0; i < 20; i++)); do
    if _tunnel_probe; then
      log "tunnel up (pid $pid): $TUNNEL_URL"
      return 0
    fi
    _pid_alive "$pid" || die "tunnel loop exited immediately; see $TUNNEL_LOG"
    sleep 1
  done
  echo "warning: tunnel loop is running (pid $pid) but $TUNNEL_URL did not answer within 20 s; see $TUNNEL_LOG" >&2
  return 1
}

tunnel_stop() {
  local pid i
  pid="$(_tunnel_pid || true)"
  if ! _pid_alive "$pid"; then
    rm -f "$TUNNEL_PIDFILE"
    log "tunnel is not running"
    return 0
  fi
  # kubectl first, then the loop: the loop sleeps 2 s before relaunching, which
  # is more than enough to take it down before it can respawn the child.
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  for ((i = 0; i < 10; i++)); do
    _pid_alive "$pid" || break
    sleep 0.5
  done
  _pid_alive "$pid" && kill -KILL "$pid" 2>/dev/null
  # A child launched in the window between the two kills would be orphaned and
  # keep the port; sweep by command line to be sure.
  pkill -TERM -f "kubectl -n $NS_DEMO port-forward --address 127.0.0.1 svc/frontend-proxy $TUNNEL_PORT:$TUNNEL_PORT" 2>/dev/null || true
  rm -f "$TUNNEL_PIDFILE"
  log "tunnel stopped"
}

# Exit 0 when the loop is alive and the URL answers, 1 otherwise.
tunnel_status() {
  local pid
  pid="$(_tunnel_pid || true)"
  if ! _pid_alive "$pid"; then
    echo "tunnel: not running"
    return 1
  fi
  if _tunnel_probe 3; then
    echo "tunnel: up (pid $pid) $TUNNEL_URL"
    return 0
  fi
  echo "tunnel: loop running (pid $pid) but $TUNNEL_URL is not answering; see $TUNNEL_LOG"
  return 1
}
