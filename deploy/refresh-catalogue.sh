#!/usr/bin/env bash
# Bring the deployed shops' catalogues up to date with shops/<a|b|c>/ without
# touching orders, accounts, wallets or sold codes.
#
#   ssh root@<vm> /opt/agent-economy/deploy/refresh-catalogue.sh
#
# Deals, options, reviews and promotions live in each shop's products
# database: they are imported again from the CSVs (the importer's writes to
# the transactions database go to a throwaway file, so nothing there moves).
# Codes are inventory: the ones in codes.csv the shop does not have yet are
# added through the merchant API, which skips the ones it already knows.
# A full reseed (orders wiped too) is reset-for-demo.sh.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN=$(grep -E "^DOMAIN=" .env | cut -d= -f2-)
SECRET=$(grep -E "^SIMULATION_SECRET=" .env | cut -d= -f2-)

for shop in a b c; do
  echo "== shop $shop: deals, options, reviews, promotions"
  docker compose exec -T "shop-$shop" sh -c '
    uv run --no-sync import_csv.py \
      --products_db_path=/data/'"$shop"'/products.db \
      --transactions_db_path=/tmp/scratch-transactions.db \
      --data_dir=/shops/'"$shop"' >/tmp/import.log 2>&1 || { tail -5 /tmp/import.log; exit 1; }
    rm -f /tmp/scratch-transactions.db*'
  echo "== shop $shop: codes"
  python3 - "$shop" "$DOMAIN" "$SECRET" <<'PY'
import csv, json, sys, urllib.request
shop, domain, secret = sys.argv[1:4]
pools = {}
for row in csv.DictReader(open(f"../shops/{shop}/codes.csv")):
  pools.setdefault(row["option_id"], []).append(row["code"])
added = skipped = 0
for option, codes in pools.items():
  req = urllib.request.Request(
    f"https://{shop}.{domain}/inventory/{option}/codes",
    data=json.dumps({"codes": codes}).encode(),
    headers={"Content-Type": "application/json", "Simulation-Secret": secret},
    method="POST",
  )
  answer = json.load(urllib.request.urlopen(req, timeout=30))
  added += answer["added"]; skipped += answer["skipped"]
print(f"   {len(pools)} options: {added} codes added, {skipped} already there")
PY
done
echo "Done. Orders, accounts, wallets and sold codes are as they were."
