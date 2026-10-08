#!/bin/sh
# Seed the shop's database on the first run, then start the shop.
#
#   SHOP                a, b or c: the catalogue under /shops/<SHOP>
#   SIMULATION_SECRET   the header value merchant actions need (required)
#   DEMO_PASSWORD       password of the demo account (else the one in users.csv)
#   REQUIRE_SIGNATURES  1 (default) turns away unsigned agents
#   REQUIRE_SIGNATURE_NONCE  1 (default) turns away signatures with no nonce
#   ALLOW_INSECURE_PROFILES  1 lets agents publish http/private profile URLs
#   SECURE_COOKIES      1 (default) marks cookies Secure; 0 for plain http
#   PAYMENT_RAIL        mock (default) or stripe; stripe needs
#                       STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY
#   RESEED              1 wipes the database and imports the catalogue again
set -eu

: "${SHOP:?SHOP must be a, b or c}"
: "${SIMULATION_SECRET:?SIMULATION_SECRET is required}"
DATA="/data/$SHOP"
CATALOGUE="/shops/$SHOP"
mkdir -p "$DATA"
DB="--products_db_path=$DATA/products.db --transactions_db_path=$DATA/transactions.db"

if [ ! -f "$DATA/transactions.db" ] || [ "${RESEED:-0}" = "1" ]; then
  echo "seeding shop $SHOP from $CATALOGUE"
  rm -f "$DATA"/*.db "$DATA"/*.db-shm "$DATA"/*.db-wal
  # shellcheck disable=SC2086
  uv run --no-sync import_csv.py $DB --data_dir="$CATALOGUE"
fi

FLAGS=""
[ "${REQUIRE_SIGNATURES:-1}" = "1" ] && FLAGS="$FLAGS --require_signatures"
[ "${REQUIRE_SIGNATURE_NONCE:-1}" = "1" ] && FLAGS="$FLAGS --require_signature_nonce"
[ "${ALLOW_INSECURE_PROFILES:-0}" = "1" ] && FLAGS="$FLAGS --allow_insecure_profile_urls"
[ "${SECURE_COOKIES:-1}" = "1" ] && FLAGS="$FLAGS --secure_cookies"

# shellcheck disable=SC2086
exec uv run --no-sync server.py $DB --port="${PORT:-8080}" \
  --shop_dir="$CATALOGUE" --ledger_path=/data/ledger.jsonl \
  --simulation_secret="$SIMULATION_SECRET" \
  --payment_rail="${PAYMENT_RAIL:-mock}" $FLAGS
