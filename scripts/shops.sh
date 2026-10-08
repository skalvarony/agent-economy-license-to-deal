#!/usr/bin/env bash
# Start, stop or check the three demo shops. Each shop is the same UCP sample
# server (rest/python/server) with its own catalogue (shops/<name>) and port.
#
#   scripts/shops.sh start    # re-seeds each shop's database, then starts it
#   scripts/shops.sh stop
#   scripts/shops.sh status
#
# REQUIRE_SIGNATURES=1 makes the shops reject unsigned agents (local profile
# URLs are allowed, so this is for localhost only).
# SIMULATION_SECRET is the header value merchant and refund actions need
# (default: demo-secret). All shops write their events to .run/ledger.jsonl.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/rest/python/server"
RUN="$ROOT/.run"
SHOPS=(a:8181 b:8182 c:8183)
LEDGER="$RUN/ledger.jsonl"

start() {
  (cd "$SERVER" && uv sync -q)
  # A fresh start gets a fresh ledger.
  pgrep -f "server.py .*--ledger_path=$LEDGER" >/dev/null || rm -f "$LEDGER"
  for shop in "${SHOPS[@]}"; do
    name="${shop%%:*}" port="${shop##*:}" dir="$RUN/${shop%%:*}"
    if [[ -f "$dir/server.pid" ]] && kill -0 "$(cat "$dir/server.pid")" 2>/dev/null; then
      echo "shop $name already running on :$port"
      continue
    fi
    rm -rf "$dir" && mkdir -p "$dir"
    db=(--products_db_path="$dir/products.db" --transactions_db_path="$dir/transactions.db")
    (cd "$SERVER" && uv run --no-sync import_csv.py "${db[@]}" \
      --data_dir="$ROOT/shops/$name" >"$dir/import.log" 2>&1)
    sig=()
    [[ "${REQUIRE_SIGNATURES:-0}" == 1 ]] && sig=(--require_signatures --require_signature_nonce --allow_insecure_profile_urls)
    (cd "$SERVER" && nohup uv run --no-sync server.py "${db[@]}" --port="$port" \
      --shop_dir="$ROOT/shops/$name" --ledger_path="$LEDGER" \
      --simulation_secret="${SIMULATION_SECRET:-demo-secret}" \
      ${sig[@]+"${sig[@]}"} >"$dir/server.log" 2>&1 & echo $! >"$dir/server.pid")
    echo "shop $name starting on http://localhost:$port"
  done
}

stop() {
  for shop in "${SHOPS[@]}"; do
    name="${shop%%:*}" pidfile="$RUN/${shop%%:*}/server.pid"
    [[ -f "$pidfile" ]] || continue
    pkill -P "$(cat "$pidfile")" 2>/dev/null || true
    kill "$(cat "$pidfile")" 2>/dev/null || true
    rm -f "$pidfile"
    echo "shop $name stopped"
  done
}

status() {
  for shop in "${SHOPS[@]}"; do
    name="${shop%%:*}" port="${shop##*:}"
    if curl -sf -o /dev/null "http://localhost:$port/.well-known/ucp"; then
      echo "shop $name up on :$port"
    else
      echo "shop $name down"
    fi
  done
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
