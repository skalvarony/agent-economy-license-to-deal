#!/usr/bin/env bash
# Print the sheet of secrets a teammate needs, to hand over by a safe channel.
# Run it on the machine, in deploy/: it reads .env and the agents' volumes.
# Nothing here goes to git or to a chat; the terminal is the only output.
#
#   ssh -t root@<vm> /opt/agent-economy/deploy/team-sheet.sh
set -euo pipefail
cd "$(dirname "$0")"

env_value() { grep -E "^$1=" .env | cut -d= -f2- || true; }

echo "License to Deal — secrets sheet ($(date -u +%Y-%m-%dT%H:%MZ))"
echo
echo "Team user (console, venue, agent pages): $(env_value AGENT_USER)"
echo "  password: the one typed into set-secrets.sh; only its hash is stored"
echo
echo "Demo shopper (every shop): demo@example.com / $(env_value DEMO_PASSWORD)"
echo
for who in alvaro david emmanouil; do
  host=$(env_value "AGENT_$(echo "$who" | tr a-z A-Z)_HOST")
  key=$(docker compose exec -T "agent-$who" sh -c 'cat /data/mcp_key 2>/dev/null' || true)
  token=$(env_value "TELEGRAM_BOT_TOKEN_$(echo "$who" | tr a-z A-Z)")
  bot="(no Telegram token set)"
  if [[ -n "$token" ]]; then
    bot=$(curl -s "https://api.telegram.org/bot$token/getMe" \
      | python3 -c 'import json,sys; r=json.load(sys.stdin); print("@" + r["result"]["username"] if r.get("ok") else "(token refused by Telegram)")' 2>/dev/null || echo "(could not ask Telegram)")
  fi
  echo "$who"
  echo "  agent:    https://$host"
  echo "  MCP URL:  https://$host/mcp/${key:-<agent not running>}"
  echo "  Telegram: $bot"
  echo
done
echo "Shops: a/b/c.$(env_value DOMAIN) — accounts are created with add-account.sh; passwords are not stored in clear."
