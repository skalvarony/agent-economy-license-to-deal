#!/usr/bin/env bash
# Put the stack in demo shape: fresh catalogues, an empty ledger, the team's
# accounts in every shop with coins to spend, and (if asked) agents with no
# history. Run it on the machine, in deploy/. It asks before wiping.
#
#   ssh -t root@<vm> /opt/agent-economy/deploy/reset-for-demo.sh
#
# Passwords are typed here, once per person, and reused across the three
# shops; they never travel through a chat or a history.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN=$(grep -E "^DOMAIN=" .env | cut -d= -f2-)
SECRET=$(grep -E "^SIMULATION_SECRET=" .env | cut -d= -f2-)
PEOPLE=(
  "afernandezde@licensetodeal.app|Álvaro Fernández"
  "dlorenzo@licensetodeal.app|David Lorenzo"
  "eadamopoulos@licensetodeal.app|Emmanouil Adamopoulos"
)
COINS=${COINS:-40}

echo "This wipes every order, account and wallet in the three shops and the"
echo "shared ledger, then seeds the catalogues again. Agents keep their keys."
read -rp "Type RESET to continue: " sure
[ "$sure" = "RESET" ] || { echo "nothing done"; exit 1; }

read -rp "Also clear the agents' history (journal, approvals, proposals)? [y/N] " wipe_agents

echo "Passwords for the team's shop accounts (same in the three shops):"
declare -A PW
for entry in "${PEOPLE[@]}"; do
  email=${entry%%|*}
  read -rsp "  $email: " pw; echo
  PW[$email]=$pw
done

echo "== shops: reseed"
RESEED=1 docker compose up -d shop-a shop-b shop-c >/dev/null
sleep 8
docker compose up -d shop-a shop-b shop-c >/dev/null   # drop the RESEED flag
echo "== ledger: empty"
docker compose exec -T shop-a sh -c ': > /data/ledger.jsonl'

if [[ "$wipe_agents" =~ ^[Yy]$ ]]; then
  echo "== agents: history cleared"
  for who in alvaro david emmanouil; do
    docker compose exec -T "agent-$who" sh -c 'rm -f /data/journal.jsonl /data/approvals.jsonl /data/proposals.json /data/pending.json; rm -rf /data/conversations' || true
  done
  docker compose restart agent-alvaro agent-david agent-emmanouil >/dev/null
fi

for i in 1 2 3 4 5 6; do
  curl -sf -o /dev/null "https://a.$DOMAIN/.well-known/ucp" && break
  sleep 3
done

echo "== accounts and coins"
for shop in a b c; do
  for entry in "${PEOPLE[@]}"; do
    email=${entry%%|*}; name=${entry##*|}
    body=$(EMAIL="$email" NAME="$name" PASSWORD="${PW[$email]}" python3 -c \
      'import json,os; print(json.dumps({"full_name": os.environ["NAME"], "email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]}))')
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://$shop.$DOMAIN/accounts" \
      -H "Simulation-Secret: $SECRET" -H "Content-Type: application/json" -d "$body")
    [ "$code" = "200" ] || { echo "  $shop: account $email refused ($code)" >&2; continue; }
    grant=$(EMAIL="$email" COINS="$COINS" python3 -c 'import json,os; print(json.dumps({"email": os.environ["EMAIL"], "coins": int(os.environ["COINS"])}))')
    curl -sf -o /dev/null -X POST "https://$shop.$DOMAIN/wallets/grant" \
      -H "Simulation-Secret: $SECRET" -H "Content-Type: application/json" -d "$grant" || echo "  $shop: no coins for $email (shop gives none?)"
    echo "  $shop: $email ready, $COINS coins"
  done
done

echo "== check"
for shop in a b c; do
  printf "  %s: ucp %s\n" "$shop" "$(curl -s -o /dev/null -w '%{http_code}' "https://$shop.$DOMAIN/.well-known/ucp")"
done
echo "Done. The demo account (demo@example.com) came back with the seed."
