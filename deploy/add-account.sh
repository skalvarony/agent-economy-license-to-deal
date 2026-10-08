#!/usr/bin/env bash
# Create a customer's account in one shop, or set its password if it exists.
# People can't register themselves; the shop does it. Run it on the machine:
#
#   ssh -t root@<vm> /opt/agent-economy/deploy/add-account.sh a ana@example.com "Ana Buyer"
#
# The password is typed here and never travels through a chat or a history.
set -euo pipefail
cd "$(dirname "$0")"
SHOP="${1:?shop: a, b or c}"
EMAIL="${2:?email}"
NAME="${3:?full name}"
DOMAIN=$(grep -E "^DOMAIN=" .env | cut -d= -f2-)
SECRET=$(grep -E "^SIMULATION_SECRET=" .env | cut -d= -f2-)
read -rsp "Password for $EMAIL at shop $SHOP: " PASSWORD; echo

body=$(EMAIL="$EMAIL" NAME="$NAME" PASSWORD="$PASSWORD" python3 -c \
  'import json,os; print(json.dumps({"full_name": os.environ["NAME"], "email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]}))')
answer=$(curl -s -w "\n%{http_code}" -X POST "https://$SHOP.$DOMAIN/accounts" \
  -H "Simulation-Secret: $SECRET" -H "Content-Type: application/json" -d "$body")
status=${answer##*$'\n'}
if [ "$status" = "200" ]; then
  echo "created $EMAIL at $SHOP.$DOMAIN"
elif echo "$answer" | grep -q "already an account"; then
  body=$(PASSWORD="$PASSWORD" python3 -c 'import json,os; print(json.dumps({"password": os.environ["PASSWORD"]}))')
  curl -sf -X PUT "https://$SHOP.$DOMAIN/accounts/$EMAIL/password" \
    -H "Simulation-Secret: $SECRET" -H "Content-Type: application/json" -d "$body" >/dev/null
  echo "password set for $EMAIL at $SHOP.$DOMAIN"
else
  echo "the shop refused: ${answer%$'\n'*}" >&2
  exit 1
fi
