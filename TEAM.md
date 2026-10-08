# License to Deal — team guide

For David and Emmanouil. Everything that exists, where it is, how to get in,
how it works and what is left. Deep detail lives in `README.md`,
`deploy/README.md` and `PLAN.md`; this is the map.

Last updated: 8 Oct 2026, morning of the hackathon (Agents 0.0.7, Prague,
Thu 16:00 – Fri 12:00).

## What it is, in three sentences

A personal AI shopping agent — one per person — that buys local-experience
vouchers (spa, beer tasting, baking class…) for you, with your approval, from
three marketplaces that verify the agent. The shops sell to agents over
**UCP** (signed requests) and to people through a normal web storefront, and
charge through **Stripe in test mode**. Everything is recorded on both sides
(the agent's journal, the shop's ledger) so "who bought this, and how" has an
answer.

## Addresses

| What | URL | Sign-in |
|---|---|---|
| Landing | https://licensetodeal.app | none |
| Shop A · **Dusk Deals** (wellness, 10 % back in coins) | https://a.licensetodeal.app | shop account (email + password) |
| Shop B · **Praha Pass** (food & things to do, 5 % back) | https://b.licensetodeal.app | shop account |
| Shop C · **Dawn Saver** (final-sale wellness, no coins) | https://c.licensetodeal.app | shop account |
| Shops console (marketplace side: orders, payments, refunds, inbox) | https://admin.licensetodeal.app | team user |
| Álvaro's agent | https://ltd-agent-alvaro.duckdns.org | team user |
| David's agent | https://ltd-agent-david.duckdns.org | team user |
| Emmanouil's agent | https://ltd-agent-emmanouil.duckdns.org | team user |
| UCP discovery of a shop (what an agent reads first) | https://a.licensetodeal.app/.well-known/ucp | none |
| An agent's public profile and signing key | https://ltd-agent-alvaro.duckdns.org/profile.json | none |

The server is one Hetzner VM, `188.34.178.68`, Docker Compose behind Caddy
(automatic HTTPS). Only Álvaro has SSH today.

**Each marketplace is a complete, independent UCP server**: its own
`/.well-known/ucp`, catalogue, orders, accounts and coins. An agent discovers
and searches each one separately and opens the checkout in the one it picks;
an account or coins in Dusk Deals mean nothing in Praha Pass. What they share
is ours, not the protocol's: the same code started three times with three
catalogues (`shops/a`, `shops/b`, `shops/c`), one shared ledger volume so the
console reads them together, and the same merchant secret. To an outside
agent they are three shops of three owners.

## Who signs in where

**Team user.** The console and the three agent pages share one login:
user `team`, one password. Álvaro gives it to you in person or by a safe
channel; it is not written anywhere (only its bcrypt hash is on the server).
Personal per-agent logins (`alvaro`, `david`, `emmanouil`) exist as an option
but are not set up.

**Shop accounts** (one per shop, same email in all three; the shops create
accounts, there is no public registration):

| Person | Email in the shops | Password |
|---|---|---|
| Álvaro | `afernandezde@licensetodeal.app` | set by Álvaro |
| David | `dlorenzo@licensetodeal.app` | to be set — ask Álvaro, `deploy/add-account.sh` |
| Emmanouil | `eadamopoulos@licensetodeal.app` | to be set — same |
| Demo shopper (for shows and tests) | `demo@example.com` | in the server's `.env` (`DEMO_PASSWORD`) |

Your agent buys under **your** email, so what it buys appears in your account
in each shop ("My vouchers" → who bought it, in which app you approved).
`licensetodeal.app` emails are identities, not mailboxes.

**Other channels of your agent** (set up from the agent page, *What it
knows about you → Connect*):

| Channel | How | State |
|---|---|---|
| ChatGPT | Developer mode → connector with your agent's MCP URL (`https://ltd-agent-<you>.duckdns.org/mcp/<key>`). The key is shown on your agent page; never paste it in chat or in the repo | Works: live card with Approve inside the chat |
| Claude Desktop | Settings → Connectors → same URL | Works: same card (MCP Apps) |
| Claude Code | `claude mcp add --transport http …` | Proposes and links; cannot show the card |
| Telegram | Create a bot with @BotFather, Álvaro puts the token in the server with `deploy/set-secrets.sh`; then link from the agent page | Álvaro's bot works (`@shop_alvaro_bot`); David's and Emmanouil's need a token |

## Secrets: where they live

All on the VM in `/opt/agent-economy/deploy/.env`, written with
`deploy/set-secrets.sh` (asks, never echoes). Nothing secret is in git or in
chat. `deploy/team-sheet.sh`, run on the VM, prints the sheet to hand over by
a safe channel: demo password, the three MCP URLs, the Telegram bot names.
Rotating an MCP key: delete `mcp_key` in that agent's volume and restart it;
then update the connector in ChatGPT/Claude.

## Architecture

```
 you ── web app │ Telegram │ ChatGPT card │ Claude card ──► your agent (one per person)
                                                              │ signs (ES256), keeps journal + approvals,
                                                              │ pays with a Stripe token (pm_…), never a card
                                                              ▼ UCP, signed
                                   shop-a ───── shop-b ───── shop-c   (same server, three catalogues)
                                      │ web storefront for people · verifies the agent's signature
                                      │ checkout → exact total · complete → charge · vouchers · refunds
                                      ▼
                                   Stripe (test mode): PaymentIntent, manual capture, refunds
 admin console ◄── merchant secret ──┘
```

| Service | Code | Port in the VM | Data |
|---|---|---|---|
| `shop-a/b/c` | `rest/python/server` (UCP sample + our services) + `shops/<a|b|c>/` catalogue | 8080 | SQLite per shop + one shared ledger volume |
| `agent-alvaro/david/emmanouil` | `agent/` (FastAPI, scripted or OpenAI brain, MCP server, Telegram) | 8190 | volume: `journal.jsonl`, `approvals.jsonl`, `proposals.json`, `memory.json`, keys |
| `console` | `console/` | 8195 | volume: review marks |
| `caddy` | `deploy/Caddyfile` | 80/443 | certificates |
| landing | `deploy/landing/` (static) | via Caddy | — |

Stack: Python 3.12 + FastAPI + `uv`, vanilla JS/CSS front ends, Playwright
for browser tests, Docker Compose. Tests: 360 (shops) + 49 (agent) + 9 +
7 (console, login).

## What the system does

**Your agent**
- Takes a request from the web, Telegram, ChatGPT or Claude; one endless
  thread shows every message with the channel it came through.
- Knows you (budget, categories, refundable-only, how you like deals shown);
  editable with a live preview.
- Searches the three shops, proposes the best deal — or the three best — with
  the exact total the shop quotes; uses your coins and promo codes. Asks
  for a day and an hour when you gave none, checks the shop's open slots
  and books the one you named ("Saturday at 11:00"). Moves or cancels what
  you bought when you ask ("move my spa day to Sunday at 12", "cancel the
  beer tasting"), as you could yourself on the shop's order page.
- Never pays without your Approve; the model has no tool that pays. Approve
  covers one exact total; if the shop changes it, it stops and asks again.
- Approvals from the web, from the card in ChatGPT/Claude, or from Telegram
  buttons; all recorded with the channel.
- History (by decision), Your purchases (vouchers as tickets, state, payment,
  links), receipts, and `/evidence/{order}` for "I never bought this".

**The shops**
- UCP for agents: discovery, search, checkout, complete; refuse unsigned or
  wrongly signed agents (401), a changed total (409), out of stock, over the
  per-person limit.
- Vouchers with codes, service window, refund policy, validity; coins (store
  credit, 1 coin = $1 in its shop); promo codes; booking fee control.
- Every deal is booked for a date and time when bought: days, hours, slot
  length and places per slot come from the catalogue; the shop lists its
  open slots, the web checkout and the agent pick one, a full or past slot
  is refused before any charge, and a refund gives the place back.
- Storefront for people: browse, cart, Stripe Elements checkout, account page
  (coins, orders, who buys for you), My vouchers (newest first, filters, who
  bought it and through which app, the shop's own ledger of the order).
- From the order page the customer moves the visit to another open slot or
  cancels for a refund while the terms allow it; the marketplace cancels and
  refunds from the console. Refunds go to card and wallet; codes voided;
  everything in an append-only ledger.

**Console** (marketplace): all orders across shops, agent vs human, Stripe
details per order, cancel and refund in one click, inbox marks (new, seen,
handled), KPIs, CSV export.

## How a purchase is paid (the part to understand well)

1. Agent → shop, signed: search, then `POST /checkout-sessions` with option,
   quantity, your email → exact total. Nothing charged.
2. You approve (web / card / Telegram). The agent records it.
3. Agent → shop, signed: `POST /checkout-sessions/{id}/complete` with the
   payment token `pm_…` (a Stripe PaymentMethod; never a card number) and an
   `agent_context` saying who proposed and in which app you approved.
4. Shop → Stripe, with the shop's own account: PaymentIntent, manual capture:
   authorise → issue the voucher → capture. Failure cancels; nothing charged.
5. The order keeps `channel`, `agent`, `signature: verified`, `payment`
   (`pi_…`, amount, coins) and `agent_context`. The agent reads the order and
   shows the receipt.

What the shop verifies today: that the request came from that agent
(signature), that the total did not move, that the token is good (Stripe),
no double charge (idempotency). What it cannot verify: that **you** approved —
it has the agent's word. That gap is the "signed approval" item below.

## The five demo scenarios

| # | Scenario | State |
|---|---|---|
| 1 | Normal purchase, any channel | Works end to end, signature verified |
| 2 | Surprise fee: shop changes the total | Shop refuses (409); agent pays nothing, asks again |
| 3 | Fake bot | 401 unsigned / wrong key, 400 private profile, 403 on the web |
| 4 | The marketplace cancels | From the console: cancel and refund (card + coins), code voided, slot released |
| 5 | "I never bought this" | `/evidence/{order}`: approval vs charge, signature, agent, buyer |

## Operating it

On the VM (`ssh root@188.34.178.68`, then `cd /opt/agent-economy/deploy`):

| Task | Command |
|---|---|
| Deploy the latest `main` | `git pull && docker compose up -d --build` (or name the services) |
| Logs | `docker compose logs -f agent-alvaro` (any service name) |
| Set or change secrets | `./set-secrets.sh` (team password, personal passwords, Telegram tokens, demo password, Stripe keys) |
| Create a shop account | `./add-account.sh a dlorenzo@licensetodeal.app "David Lorenzo"` (one call per shop) |
| Reset the shops to their catalogues | `RESEED=1 docker compose up -d shop-a shop-b shop-c`, then `docker compose up -d` |
| Put everything in demo shape (reseed, empty ledger, team accounts in the three shops with 40 coins, optionally clean agents) | `./reset-for-demo.sh` (asks before wiping; passwords typed there) |
| Print the secrets sheet to hand over | `./team-sheet.sh` |

From a laptop, in one go: `ssh -t root@188.34.178.68
/opt/agent-economy/deploy/team-sheet.sh` — `ssh root@…` opens a session on
the VM, the path runs that script there instead of a shell, `-t` gives it a
terminal (the `docker compose exec` inside needs one). The sheet shows on
your screen and nowhere else.

Locally (needs `uv`, Node 22+): `scripts/shops.sh start`, `scripts/agent.sh
start` (http://localhost:8190, no login), `scripts/console.sh start`,
`python3 scripts/smoke.py` runs 28 live checks.
Every local start re-seeds.

## Repo map

`rest/python/server` shops · `shops/` catalogues · `agent/` the agent ·
`console/` · `shared/teamlogin.py` sign-in · `deploy/` Compose,
Caddy, scripts, landing · `scripts/` local run · `tests-e2e/` browser tests ·
`docs/poster/` one-page overview (PNG) · `PLAN.md` decisions and status ·
`README.md` the long version.

## Pending for the event

In order of value for the demo:

1. **OpenAI key** (arrives at the event): set it with `set-secrets.sh`
   (asks for key and model; default `gpt-5-mini`), `docker compose up -d
   agent-alvaro agent-david agent-emmanouil`, run the model brain for real,
   tune `agent/model_brain.py`'s instructions. Until then the scripted brain
   decides and the page says so. The brain asks GPT-5 models for low
   reasoning effort (`AGENT_REASONING`, default `low`) so a proposal takes
   seconds, and retries once on a rate limit or a 5xx. If the model
   misbehaves during the demo: `AGENT_BRAIN=scripted` in `.env` and restart
   the agents; the scripted brain handles the five scenarios.
2. **Telegram bots** for David and Emmanouil: create with @BotFather, hand
   the token to Álvaro, link from the agent page.
3. **Shop accounts and passwords** for David and Emmanouil (`add-account.sh`);
   connectors in your own ChatGPT / Claude with your agent's MCP URL.
4. **Thursday morning:** reseed the shops, recreate accounts and coins; clear
   old `approvals.jsonl` in the agents if we want clean histories.
5. **The night — signed approval (AP2):** the approval travels in `complete`
   as a mandate the shop verifies before charging (`approval_checks.py` is a
   stub with the slots). Level 1: signed by the agent. Level 2: passkey /
   Face ID on the agent's web app, signed by the owner. Shows as "Approved by
   the owner" vs "via the agent" in shop, console and evidence.
6. **Voice approval** (ElevenLabs) ending in that same record; **demo
   script** for the five scenarios; **pitch and 2-minute video** (HyperFrames
   skills are installed in the repo, brief drafted).
7. Open: Masumi (event partner), real payments (needs the sponsor's OK;
   switching Stripe to live is a pair of keys), email as identity.

## Ground rules

No employer name, data, systems or accounts anywhere. Play money: Stripe test
mode, simulated catalogue, labelled as such. Say openly what was built before
the night and what during it. Private, throwaway repository. Secrets never
through chat, only `set-secrets.sh` on the VM.
