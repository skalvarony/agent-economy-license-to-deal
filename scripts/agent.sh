#!/usr/bin/env bash
# Start, stop or check the customer's shopping agent (agent/). It buys from
# the shops in agent/agent.json, so start those first with scripts/shops.sh.
#
#   scripts/agent.sh start    # http://localhost:8190
#   scripts/agent.sh stop
#   scripts/agent.sh status
#
# AGENT_PORT changes the port. AGENT_URL is the address shops fetch the
# agent's profile from (default http://localhost:$AGENT_PORT).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/.run/agent"
PORT="${AGENT_PORT:-8190}"
PIDFILE="$DIR/server.pid"

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if running; then
    echo "agent already running on :$PORT"
    return
  fi
  mkdir -p "$DIR"
  (cd "$ROOT/agent" && uv sync -q)
  (cd "$ROOT/agent" && AGENT_URL="${AGENT_URL:-http://localhost:$PORT}" \
    nohup uv run --no-sync uvicorn app:app --host 127.0.0.1 --port "$PORT" \
    >"$DIR/server.log" 2>&1 & echo $! >"$PIDFILE")
  echo "agent starting on http://localhost:$PORT"
}

stop() {
  [[ -f "$PIDFILE" ]] || return 0
  pkill -P "$(cat "$PIDFILE")" 2>/dev/null || true
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "agent stopped"
}

status() {
  if curl -sf -o /dev/null "http://localhost:$PORT/profile.json"; then
    echo "agent up on :$PORT"
  else
    echo "agent down"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
