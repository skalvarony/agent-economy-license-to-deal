# License to Deal

**An AI agent that buys local experiences for a person, from shops that can
tell it is genuine, with real (Stripe test mode) money, a spending cap in
code, and a record both sides can check.**

Agents 0.0.7 · From Dusk Till Dawn, Prague · Case 02 **Agentic Economy**
(discovery / payments / trust) · side challenge **Best ElevenLabs Use** (the
agent answers a phone number and takes the person's yes on the call).
Team License to Deal: Álvaro Fernández, David Lorenzo, Emmanouil Adamopoulos.

## 1. In ninety seconds

A person tells their agent what they want ("a spa day for two, refundable,
under $120, Saturday at 11"). The agent discovers three independent
marketplaces over **UCP** (the Universal Commerce Protocol) and signs every
request with its own key (RFC 9421, with expiry and nonce). It compares what
they sell against the person's budget, rules and coin wallets, checks the
open time slots, and proposes one purchase with the exact total. The person
says yes on the agent's page, on a card inside ChatGPT or Claude, on
Telegram, or **by voice on a phone call**. The agent pays through the shop's
checkout with a Stripe PaymentIntent in test mode, and the shop issues the
voucher booked for that slot. Both sides keep a record: the agent's journal
and approvals, the shop's ledger with the agent's identity and the
signature's outcome. Later the person, or the agent on their behalf, can
move the visit or cancel it for a refund.

Executed last night in production, as evidence: order
`befff457-e5a6-465d-ae13-b9a1d9bbee8e` in Dusk Deals, proposed and approved
through the agent's MCP door, $99 authorised and captured on Stripe
(PaymentIntent in test mode, Radar risk "normal", score 58), then cancelled
through the agent and refunded in full; the slot went back on the calendar.

## 2. Links and access, for the jury

| What | Where | Sign in |
|---|---|---|
| Landing page | https://licensetodeal.app | none |
| Shop A · Dusk Deals (wellness, refundable, 10% back in coins) | https://a.licensetodeal.app | shop account: `demo@example.com` / `licensetodeal` |
| Shop B · Praha Pass (food and activities, 5% back) | https://b.licensetodeal.app | same account |
| Shop C · Dawn Saver (wellness, cheaper, non-refundable, no coins) | https://c.licensetodeal.app | same account |
| The agent (Álvaro's; the person's side: conversation, approvals, purchases, evidence, memory and rules, connections) | https://ltd-agent-alvaro.duckdns.org | `judge` / `license-to-deal-2026` |
| The shops' console (marketplace side: orders, payments, fraud check, refunds, ledger, inventory, customers) | https://admin.licensetodeal.app | `judge` / `license-to-deal-2026` |
| Call the agent (ElevenLabs Conversational AI on a Twilio number; English) | **+1 279 240 6436** | say what you want, then "yes" to buy |
| The agent as MCP tools for ChatGPT (developer mode connector) or Claude (Settings → Connectors) | `https://ltd-agent-alvaro.duckdns.org/mcp/kEZczaKChbWAH3U_5gnlC53RMxB_2DT-` | the address is the key (demo key; rotated after the event) |
| UCP discovery of a shop, what an agent reads first | https://a.licensetodeal.app/.well-known/ucp | none |
| The agent's public profile and signing key, what the shops verify against | https://ltd-agent-alvaro.duckdns.org/profile.json | none |
| A shop's open slots for a deal | https://a.licensetodeal.app/deals/spa_day_two/availability?days=3 | none |
| Repository | https://github.com/skalvarony/agent-economy-license-to-deal (private; the jury has the snapshot) | |
| Demo video (90 s) | submitted in HQ | |
| Team guide, deployment, plan | `TEAM.md`, `deploy/README.md`, `PLAN.md` | |

The shop account is the demo shopper the agent buys for, so the agent's
purchases appear under "My vouchers" in each shop, marked as the agent's.
Stripe runs in test mode with Stripe's test payment methods, never a card
number.

**Five minutes, in this order.**

1. Open the agent, sign in, type "A spa day for two, refundable, under $120,
   Saturday at 11:00". It shows what it turned down and why, and proposes
   one with the slot. Approve; the receipt shows the voucher and Stripe's
   charge.
2. In the console, open that order: who paid, the agent's signature
   verified, Radar's verdict, the approval record and the conversation.
3. On the agent, "move my spa day to Sunday at 12", approve; then "cancel
   it", approve: the refund goes back through Stripe and the slot is
   released.
4. Call +1 279 240 6436, ask for the same, and say yes on the call.
5. Try to break it: ask for the full-day spa ($139, above the agent's $120
   cap: refused before it is proposed), run `scripts/replay-demo.py` against
   a local shop (`401 signature_replayed`), or pay on the web with
   `4000 0000 0000 9995`.

## 3. Value and track relevance

Agents can act, but the moment they have to pay, every flow ends at a human
with a credit card. License to Deal makes the whole loop work agent-to-shop:

- **Discovery.** Each shop publishes `/.well-known/ucp`; the agent reads terms, stock, coins and open slots as data, not prose to scrape.
- **Trust.** Every request is signed (ES256, RFC 9421) with a key on a domain the shops do not control; unsigned, stale, replayed or wrongly signed requests are refused.
- **Payments.** Stripe test mode: authorised at `complete`, captured when the voucher is issued, refunded otherwise, with idempotency keys and Radar's fraud verdict.
- **Wallets.** Coins per customer in each shop; pay with coins, card or both, the exact split shown before the yes and returned the same way on refund.
- **Accountability and disputes.** The agent's journal and approvals, the shop's ledger, `/evidence/{order}` side by side, and a console where the marketplace cancels and refunds.
- **Spending policy in code.** A hard cap per purchase and per day that no brain, rule or approval can lift, plus the person's own rules, enforced before any checkout opens.

## 4. Originality

- **The agent is the person's, not the shop's and not the model's.** Any brain proposes; code signs, enforces caps, collects the yes and pays. No brain has a tool that pays.
- **The yes travels with the purchase.** The order records who proposed, through which channel the person approved and when; on a call, the person's own words.
- **One agent, five front doors.** Web page, Telegram, a ChatGPT or Claude card, and a phone call, with one journal that never forks.
- **Three independent marketplaces that verify a key they do not control**, rather than one mock API; the console sees both sides of every agent order.
- **Changes are purchases too.** Moving or cancelling waits for the person's yes, after a live run showed a model cancelling on a misread "move".

## 5. Working result

| Scenario | What happens | How to see it |
|---|---|---|
| 1 Normal purchase | Search three shops, propose with the exact split, yes on any channel, Stripe charge, voucher booked for the slot | Agent page, then the console's order |
| 2 Surprise fee | The total changes after the proposal; `complete` answers `409 requires_consent`, nothing is charged | Console → Demo controls → booking fee |
| 3 Fake or careless bot | Unsigned, wrong key, expired or replayed: `401`; private profile `400`; declared bot on the web storefront `403` | `scripts/replay-demo.py` |
| 4 Cancellations and refunds | The customer, the agent or the console cancels; card and coins go back, the slot is released, the code voided | Agent: "cancel my spa day"; console: Cancel and refund |
| 5 "I never bought this" | `/evidence/{order}`: approved vs charged, signature verified, which channel, the quote on a call | Agent → Purchases → Evidence |

Checked by: 394 shop tests, 61 agent tests, 17 console tests, 8 browser
flows, a 31-step live smoke test, and `eval_live.py`, last run 11/11 with
Claude Sonnet 4.5 for $0.63.

## 6. Technical execution

```
 person ── web page │ Telegram │ ChatGPT card │ Claude card │ phone (ElevenLabs) ──► the agent
                                                                                      │ FastAPI; brain = scripted | model | external over MCP
                                                                                      │ memory + rules; caps in code; ES256 signing; journal + approvals
                                                                                      ▼ UCP, RFC 9421 signatures with expires + nonce
                                              shop-a ───────── shop-b ───────── shop-c   (the UCP sample server + our services, three catalogues)
                                                 │ web storefront for people · verifies the agent's key from its profile
                                                 │ checkout → exact total · complete → Stripe PaymentIntent · voucher · bookings · refunds
                                                 ▼
                                              Stripe (test mode): authorise, capture, refund, Radar verdict
 shops' console ◄── merchant secret ─────────────┘        Caddy (HTTPS) · Docker Compose on one Hetzner VM
```

- **Protocols.** UCP (the official sample server, Apache 2.0, plus our services); RFC 9421 signatures with `expires` and `nonce`; MCP with the ChatGPT Apps SDK and MCP Apps; ElevenLabs Conversational AI with Twilio.
- **Nothing pays twice.** One proposal is approved once; Stripe calls carry idempotency keys; `complete` refuses a total that moved; a replayed request is refused.
- **Caps hold in code.** `AGENT_HARD_CAP` ($200 per purchase by default, $120 on the deployed agent) and `AGENT_DAILY_CAP` ($500 per 24 h), checked before proposing and again at paying, in `agent/session.py`.
- **Fraud and failure paths.** Radar blocked or highest risk is refused, elevated is flagged, 3-D Secure is refused; the model brain retries, falls back to a second model, then a scripted stand-in.
- **Records.** The agent's `journal.jsonl`, `approvals.jsonl` and `memory.json`; the shop's orders with `agent_context`, signature outcome and Stripe's verdict, in one ledger.

## 7. Real, simulated, missing

| | |
|---|---|
| **Real (executed)** | The agent pays: Stripe payments in test mode, authorised, captured, cancelled and refunded through Stripe's API by the agent's own checkout call, with Radar's verdict (test mode moves no money; the hackathon counts it as real). UCP discovery, catalogue, checkout and orders, signature verification with expiry and replay refusal, bookings with capacity, coins and mixed payment. The model brain (Claude Sonnet 4.5 via OpenRouter) in production, the phone channel (ElevenLabs + Twilio), and the ChatGPT, Claude and Telegram channels. |
| **Simulated, and labelled** | The three shops, their catalogues, prices, reviews and merchants are fictional. The `mock` payment rail exists for local runs and is labelled "Simulated"; production runs the `stripe` rail. The merchant's side of a cancellation is the console, operated by us. |
| **Missing or partial** | The person's yes is consent to the agent's proposal, not a pay button: the agent discovers, chooses, books, signs and pays on its own, and the shop never sees a human checkout. What is missing is a threshold below which the agent buys without asking (the caps in code make it a small change), and a way for the shop to verify that consent cryptographically instead of trusting the agent's record. The nonce cache is in memory, one process per shop; the web storefront's bot detection is hints, not proof; the ElevenLabs voice agent's own reasoning runs at ElevenLabs (Claude Haiku 4.5), while our agent enforces the rules and caps. Disputes end at the console and the evidence page with no arbitration flow, and Stripe's live mode has not been used. |

## 8. The case's hard rules, checked

- **Real money only from our own funds; no card numbers in code.** Stripe test mode on our own account, test payment method tokens (`pm_card_visa`), never a PAN.
- **No private keys, seed phrases or API secrets in the repo.** The signing key is generated on first start (gitignored); other keys live only in the VM's `.env`.
- **Every spending agent has a hard cap enforced in code, not in the prompt.** `agent/session.py`, per purchase and per 24 hours, tested in `agent/agent_test.py`.
- **No token launches, no speculative trading.** None.
- **Mocked payments labelled SIMULATED.** On every screen that shows the mock rail; the deployed shops run on Stripe.
- **Fresh build.** The shop server starts from the UCP sample (the repository's second commit is the unmodified copy); everything else is ours.

## 9. Run it

Needs [uv](https://docs.astral.sh/uv/). Every start re-seeds the shops.

```shell
scripts/shops.sh start       # REQUIRE_SIGNATURES=1 to turn away unsigned agents (adds the nonce check)
scripts/agent.sh start       # http://localhost:8190
scripts/console.sh start     # http://localhost:8195
python3 scripts/smoke.py     # 31 live checks: buys, books, moves, cancels, refunds, booking fee
cd agent && uv run python ../scripts/replay-demo.py http://localhost:8181   # a copied signed request is refused
scripts/agent.sh stop && scripts/shops.sh stop
```

Tests: `cd rest/python/server && uv run pytest` (394), `cd agent && uv run
pytest` (61), `cd console && uv run pytest` (17).

Deployment on one VM: `deploy/README.md`. The detail of every part:
`REFERENCE.md`.
