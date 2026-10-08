#!/usr/bin/env bash
# Start, stop or check the venue simulator (venue/): the merchants' world
# for the shops in venue/venue.json, so start those first with scripts/shops.sh.
#
#   scripts/venue.sh start    # http://localhost:8196
#   scripts/venue.sh stop
#   scripts/venue.sh status
#
# VENUE_PORT changes the port. SIMULATION_SECRET must match the shops'
# (default demo-secret, as in scripts/shops.sh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/.run/venue"
PORT="${VENUE_PORT:-8196}"
PIDFILE="$DIR/server.pid"

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if running; then
    echo "venue already running on :$PORT"
    return
  fi
  mkdir -p "$DIR"
  (cd "$ROOT/venue" && uv sync -q)
  (cd "$ROOT/venue" && nohup uv run --no-sync uvicorn app:app --host 127.0.0.1 --port "$PORT" \
    >"$DIR/server.log" 2>&1 & echo $! >"$PIDFILE")
  echo "venue starting on http://localhost:$PORT"
}

stop() {
  [[ -f "$PIDFILE" ]] || return 0
  pkill -P "$(cat "$PIDFILE")" 2>/dev/null || true
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "venue stopped"
}

status() {
  if curl -sf -o /dev/null "http://localhost:$PORT/api/log"; then
    echo "venue up on :$PORT"
  else
    echo "venue down"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
