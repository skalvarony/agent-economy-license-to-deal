#!/usr/bin/env bash
# Fill in the secrets of deploy/.env by typing them on the machine, so they
# never travel through a chat, a shell history or a repository.
#
#   ssh -t root@<vm> /opt/agent-economy/deploy/set-secrets.sh
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env

current() { grep -E "^$1=" .env | cut -d= -f2- || true; }

read -rp "Email for certificate notices [$(current ACME_EMAIL)]: " ACME
echo "Team sign-in (the console, and the agents without a personal login):"
read -rp "  User [$(current AGENT_USER)]: " USER_NAME
read -rsp "  Password: " AGENT_PASSWORD; echo
echo "Personal sign-in for each agent (empty keeps the current one or the team's):"
read -rsp "  Alvaro's password: " PW_ALVARO; echo
read -rsp "  David's password: " PW_DAVID; echo
read -rsp "  Emmanouil's password: " PW_EMMANOUIL; echo
read -rsp "Password of the demo account (demo@example.com): " DEMO; echo
echo "Telegram bot tokens from @BotFather, one per agent (empty keeps the current):"
read -rsp "  Alvaro's agent: " TG_ALVARO; echo
read -rsp "  David's agent: " TG_DAVID; echo
read -rsp "  Emmanouil's agent: " TG_EMMANOUIL; echo
echo "Stripe (leave empty to keep the mock rail or the current keys):"
read -rsp "  Secret key (sk_test_…): " STRIPE_SK; echo
read -rp "  Publishable key (pk_test_…): " STRIPE_PK
echo "OpenAI, the agents' brain (empty keeps the current key or the scripted brain):"
read -rsp "  API key: " OPENAI; echo
read -rp "  Model [$(current AGENT_MODEL)]: " MODEL
read -rp "  Base URL [$(current OPENAI_BASE_URL)] (Anthropic: https://api.anthropic.com/v1): " BASE_URL

hash() { [ -n "$1" ] && docker run --rm caddy:2-alpine caddy hash-password --plaintext "$1" || true; }
HASH=$(hash "$AGENT_PASSWORD")
HASH_ALVARO=$(hash "$PW_ALVARO")
HASH_DAVID=$(hash "$PW_DAVID")
HASH_EMMANOUIL=$(hash "$PW_EMMANOUIL")

ACME="$ACME" USER_NAME="$USER_NAME" HASH="$HASH" DEMO="$DEMO" \
  HASH_ALVARO="$HASH_ALVARO" HASH_DAVID="$HASH_DAVID" HASH_EMMANOUIL="$HASH_EMMANOUIL" \
  TG_ALVARO="$TG_ALVARO" TG_DAVID="$TG_DAVID" TG_EMMANOUIL="$TG_EMMANOUIL" \
  STRIPE_SK="$STRIPE_SK" STRIPE_PK="$STRIPE_PK" OPENAI="$OPENAI" MODEL="$MODEL" BASE_URL="$BASE_URL" python3 - <<'PY'
import os, re, pathlib
env = pathlib.Path(".env")
text = env.read_text()

def put(key, value):
  global text
  if not value:
    return
  # Compose reads $ as interpolation; a literal $ is written $$.
  value = value.replace("$", "$$")
  if re.search(rf"^{key}=", text, re.M):
    text = re.sub(rf"^{key}=.*$", lambda _: f"{key}={value}", text, flags=re.M)
  else:
    text += f"\n{key}={value}\n"

put("ACME_EMAIL", os.environ["ACME"])
put("AGENT_USER", os.environ["USER_NAME"])
put("AGENT_PASSWORD_HASH", os.environ["HASH"])
put("DEMO_PASSWORD", os.environ["DEMO"])
for who in ("ALVARO", "DAVID", "EMMANOUIL"):
  put(f"TELEGRAM_BOT_TOKEN_{who}", os.environ[f"TG_{who}"])
# Each agent's own login: user is the first name, in lower case.
for who in ("ALVARO", "DAVID", "EMMANOUIL"):
  if os.environ[f"HASH_{who}"]:
    put(f"LOGIN_USERS_{who}", f"{who.lower()}:{os.environ[f'HASH_{who}']}")
if os.environ["STRIPE_SK"]:
  # Keys given: the shops move to the stripe rail and the agents pay with
  # Stripe's test card.
  put("STRIPE_SECRET_KEY", os.environ["STRIPE_SK"])
  put("STRIPE_PUBLISHABLE_KEY", os.environ["STRIPE_PK"])
  put("PAYMENT_RAIL", "stripe")
  put("AGENT_CARD_HANDLER", "stripe")
  put("AGENT_CARD_TOKEN", "pm_card_visa")
put("OPENAI_API_KEY", os.environ["OPENAI"])
put("AGENT_MODEL", os.environ["MODEL"])
put("OPENAI_BASE_URL", os.environ["BASE_URL"])
if "SIMULATION_SECRET=\n" in text or re.search(r"^SIMULATION_SECRET=$", text, re.M):
  put("SIMULATION_SECRET", os.urandom(24).hex())
env.write_text(text)
PY
chmod 600 .env
echo "Saved. Empty answers left the current values."
