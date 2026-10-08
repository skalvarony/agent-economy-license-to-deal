#!/usr/bin/env bash
# Start, stop or check the shops' console (console/). It manages the shops
# in console/console.json, so start those first with scripts/shops.sh.
#
#   scripts/console.sh start    # http://localhost:8195
#   scripts/console.sh stop
#   scripts/console.sh status
#
# CONSOLE_PORT changes the port. SIMULATION_SECRET must match the shops'
# (default demo-secret, as in scripts/shops.sh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/.run/console"
PORT="${CONSOLE_PORT:-8195}"
PIDFILE="$DIR/server.pid"

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if running; then
    echo "console already running on :$PORT"
    return
  fi
  mkdir -p "$DIR"
  (cd "$ROOT/console" && uv sync -q)
  (cd "$ROOT/console" && nohup uv run --no-sync uvicorn app:app --host 127.0.0.1 --port "$PORT" \
    >"$DIR/server.log" 2>&1 & echo $! >"$PIDFILE")
  echo "console starting on http://localhost:$PORT"
}

stop() {
  [[ -f "$PIDFILE" ]] || return 0
  pkill -P "$(cat "$PIDFILE")" 2>/dev/null || true
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "console stopped"
}

status() {
  if curl -sf -o /dev/null "http://localhost:$PORT/api/shops"; then
    echo "console up on :$PORT"
  else
    echo "console down"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
