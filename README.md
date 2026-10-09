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
marketplaces over **UCP** (the Universal Commerce Protocol), signs every
request with its own key (RFC 9421, with expiry and nonce), compares what
they sell against the person's budget, rules and coin wallets, checks the
open time slots, and proposes one purchase with the exact total. The person
says yes on the agent's page, on a card inside ChatGPT or Claude, on
Telegram, or **by voice on a phone call**. The agent then pays through the
shop's checkout with a Stripe PaymentIntent in test mode (authorise, capture;
refund on cancellation), the shop issues the voucher booked for that slot,
and both sides keep a record: the agent's journal and approvals, the shop's
ledger with the agent's identity and the signature's outcome. Later the
person, or the agent on their behalf, can move the visit or cancel it for a
refund; the shop's console shows every order, who paid, through which door,
and Stripe's fraud verdict.

Executed last night in production, as evidence: order
`befff457-e5a6-465d-ae13-b9a1d9bbee8e` in Dusk Deals, proposed and approved
through the agent's MCP door, $99 authorised and captured on Stripe
(PaymentIntent in test mode, Radar risk "normal", score 58), then cancelled
through the agent and refunded in full; the slot went back on the calendar.
The console at `admin.licensetodeal.app` shows it with its Stripe timeline.

What is real, what is simulated, what is missing: section 7. Everything
below runs on one VM; every link in section 2 is live.

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
| Demo video (90 s) | submitted in HQ; script and notes in `docs/video/` | |
| Poster | `docs/poster/overview.png` | |
| Team guide, deployment, plan | `TEAM.md`, `deploy/README.md`, `PLAN.md` | |

The shop account is the demo shopper the agent buys for in the deployed
instance, so the agent's purchases appear under "My vouchers" in each shop,
marked as the agent's, next to the shop's own ledger of the order. The
`judge` login is a second user beside the team's; the shops' passwords are
scrypt hashes, the team's and the jury's bcrypt, and all live only in the
VM's `deploy/.env` (`deploy/set-secrets.sh` writes it; nothing secret is in
this repository). Stripe runs in test mode with Stripe's test payment
methods (`pm_card_visa` and the Radar ones), never a card number.

**Five minutes, in this order.** (1) Open the agent, sign in, type "A spa
day for two, refundable, under $120, Saturday at 11:00"; it searches the
three shops, shows what it turned down and why, and proposes one with the
slot; approve; the receipt shows the voucher and Stripe's charge. (2) In the
console, open that order: who paid, the agent's signature verified, Radar's
verdict, the approval record and the conversation behind it. (3) Back on the
agent, "move my spa day to Sunday at 12", approve the change; then "cancel
it", approve: the refund goes back through Stripe and the slot is released.
(4) Call +1 279 240 6436 and ask for the same; say yes on the call. (5) Try
to break it: ask for the full-day spa ($139, above the agent's $120 cap
in code: refused before it is even proposed), run
`scripts/replay-demo.py` against a local shop (a copied signed request gets
`401 signature_replayed`), or pay on the web with `4000 0000 0000 9995`.

## 3. The problem and why this answers it (value and track relevance)

Agents can act, but the moment they have to pay, every flow ends at a human
with a credit card, because nobody on the other side can tell a genuine
agent from a scraper, nobody can prove the person wanted that exact
purchase, and nothing stops an agent from paying twice or paying too much.
License to Deal takes the most ordinary purchase there is, a voucher for a
local experience, and makes the whole loop work agent-to-shop:

- **Discovery.** Each marketplace publishes `/.well-known/ucp` with its
  capabilities and endpoints; the agent reads it, searches, reads terms,
  stock, coins and open time slots as data, not as prose to scrape.
- **Trust.** Every agent request is signed (ES256, RFC 9421) with a key the
  agent publishes on a domain the shops do not control; the shops verify it,
  refuse unsigned, stale, replayed or wrongly signed requests, record the
  outcome on the order, and send declared bots on the web storefront to the
  agent door instead of the human checkout.
- **Payments.** The shop charges through Stripe (test mode): a PaymentIntent
  authorised at `complete`, captured when the voucher is issued, cancelled or
  refunded otherwise, with idempotency keys on lock, capture and refund so
  nothing pays twice; the shop reads Stripe Radar's fraud verdict and acts on
  it.
- **Wallets.** Each shop keeps coins per customer; a purchase can be paid with
  coins, card or both, with the exact split shown before the yes and returned
  the same way on refund.
- **Accountability and disputes.** The agent's journal and approvals, the
  shop's ledger, `/evidence/{order}` putting the two side by side, a console
  where the marketplace cancels and refunds, Stripe's fraud flags, and
  customer self-service to move or cancel a booking, by hand or through the
  agent.
- **Spending policy in code.** A hard cap per purchase and per day that no
  brain, rule or approval can lift (section 6), plus the person's own rules
  (maximum total, refundable only, categories to avoid) enforced before any
  checkout opens.

The user is a person who would rather say what they want than browse three
sites; the shops are merchants who want agent traffic they can trust. Both
get something they cannot get today.

## 4. What is new (originality)

- **The agent is the person's, not the shop's and not the model's.** It is a
  harness around any brain: a scripted one, a language model (Claude Sonnet
  4.5 through OpenRouter in production), or an external assistant using the
  agent as MCP tools (ChatGPT, Claude). The brain proposes; code signs, keeps
  memory and rules, enforces caps, collects the yes and pays. No brain has a
  tool that pays.
- **The yes travels with the purchase.** The shop's order records who
  proposed, through which channel the person approved (page, ChatGPT card,
  Claude card, Telegram, phone) and when; on a phone call the person's own
  words ("yes, go ahead") are kept as the quote of the approval.
- **One agent, five front doors.** The same conversation continues on the
  web page, in Telegram, inside a ChatGPT or Claude chat as a live card
  (ChatGPT Apps SDK and MCP Apps, one widget), and on a phone call
  (ElevenLabs Conversational AI reaching the agent as an MCP server), with
  one journal that never forks.
- **Three independent marketplaces that verify a key they do not control**,
  the way real shops would, rather than one mock API; and the shops'
  console sees both sides of every agent order, including the conversation
  that led to it.
- **Changes are purchases too.** Moving a visit or cancelling for a refund are
  proposals that wait for the person's yes, after a live run showed a model
  cancelling on a misread "move".

## 5. It works end to end (working result)

The five scenarios the entry is judged on, each runnable live:

| Scenario | What happens | How to see it |
|---|---|---|
| 1 Normal purchase | Search three shops, propose with the exact split, yes on page / card / Telegram / phone, Stripe charge, voucher booked for the slot | Agent page, then the console's order |
| 2 Surprise fee | The shop changes the total after the proposal; `complete` answers `409 requires_consent`, nothing is charged, the agent asks again | Console → Demo controls → booking fee |
| 3 Fake or careless bot | Unsigned `401 signature_missing`, wrong key `401 signature_invalid`, expired `401 signature_expired`, replayed `401 signature_replayed`, private profile `400`, declared bot on the web storefront `403` | `scripts/replay-demo.py`; `rest/python/server/signature_integration_test.py` |
| 4 Cancellations and refunds | The customer or the agent moves the visit or cancels for a refund under the deal's terms; the marketplace cancels from the console; card and coins go back the way they came, the slot is released, the code voided | Agent: "cancel my spa day"; console: Cancel and refund |
| 5 "I never bought this" | `/evidence/{order}` lays the approval record next to the shop's order: approved vs charged, signature verified, which channel, the quote on a call | Agent → Purchases → Evidence |

Checked by: 394 shop tests, 61 agent tests, 17 console tests, 8 browser
flows, a 31-step live smoke test against fresh shops, and `eval_live.py`,
an 11-step live conversation against the deployed brain (ask without a
time, answer with one, buy, move, an impossible hour, cancel, decline,
Spanish), last run 11/11 with Claude Sonnet 4.5 for $0.63.

## 6. How it is built (technical execution)

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

- **Protocols.** UCP for discovery, catalogue, checkout and orders (the shop
  is the official sample server, Apache 2.0, with our voucher, booking,
  coin, account and payment services added); RFC 9421 HTTP message
  signatures with `created`, `expires` (5 min, at most 8) and a `nonce` the
  shop remembers for the window; MCP (JSON-RPC over HTTP, with the ChatGPT
  Apps SDK and MCP Apps UI extension) to expose the agent as tools; OpenAI
  chat completions (through OpenRouter) for the model brain; ElevenLabs
  Conversational AI with Twilio for the phone.
- **Nothing pays twice.** One proposal is approved once (state closed on the
  first yes; card and voice approvals carry one-time tokens); the shop's
  Stripe calls carry idempotency keys; `complete` refuses a total that moved
  since the agent last saw it; a replayed signed request is refused.
- **Caps hold in code.** `AGENT_HARD_CAP` (per purchase: $200 by default,
  $120 on the deployed agent so the full-day spa shows the refusal) and
  `AGENT_DAILY_CAP` ($500 per 24 h, from the agent's own record) are checked
  before a proposal is shown and again at the moment of paying, after the
  yes, in `agent/session.py`; the person's own rules (maximum, refundable
  only, no category) are enforced the same way before any checkout opens.
  The page shows the caps and what was spent today.
- **Fraud and failure paths.** Stripe Radar's verdict is read on every
  charge: blocked or highest risk is refused before any voucher exists,
  elevated or manual review is sold but flagged in the console with a
  `PAYMENT_RISK_REVIEW` ledger line, a 3-D Secure request is refused with a
  message saying an agent cannot do that step. The model brain retries,
  falls back to a second model, and a scripted stand-in takes a turn no
  model could; a failed request is dropped from the model's episode so it is
  never read later as a live order.
- **Records.** The agent: `journal.jsonl` (every message, tool call,
  proposal, receipt, with its channel), `approvals.jsonl` (each yes and what
  came of it), `memory.json`. The shop: orders with `agent_context`,
  signature outcome and Stripe's verdict; one ledger across the three shops.
- **Operations.** One VM, Docker Compose, Caddy with automatic HTTPS, secrets
  only in the VM's `.env`; a demo reset script; a team sheet script; local
  run with `uv` in two commands (section 8).

## 7. Real, simulated, missing (validation and honest limitations)

| | |
|---|---|
| **Real (executed)** | Stripe payments in test mode: PaymentIntent authorised, captured, cancelled and refunded through Stripe's API, with Radar's verdict (test mode moves no money; the hackathon counts it as real). UCP discovery, catalogue, checkout and orders. Signature verification with expiry and replay refusal. Bookings with capacity. Coins and mixed payment. The agent's journal and approvals. The model brain (Claude Sonnet 4.5 via OpenRouter) in production. The phone channel (ElevenLabs + Twilio). ChatGPT, Claude and Telegram channels. |
| **Simulated, and labelled** | The three shops, their catalogues, prices, reviews and merchants are fictional. The `mock` payment rail exists for local runs and is labelled "Simulated" on the console and "(simulated)" on receipts; production runs the `stripe` rail. The merchant's side of a cancellation is the console, operated by us. |
| **Missing or partial** | The approval is a button press (or spoken words) the agent records; the shop cannot verify it cryptographically yet (an AP2-style signed mandate is the next step; `approval_checks.py` is the stub). Every purchase waits for the person's yes: there is no "below $X the agent buys on its own" threshold yet, though the caps in code make that a small change. The nonce cache is in memory, one process per shop; replicas would need a shared store. The web storefront's bot detection is hints, not proof. The ElevenLabs voice agent's own reasoning runs at ElevenLabs (Claude Haiku 4.5) and is tuned by prompt; our agent enforces the rules and caps whatever it says. Disputes end at the console and the evidence page; there is no arbitration flow. Stripe's live mode has not been used. |

What we checked: the test suites above on every change; the live smoke
against fresh shops; `eval_live.py` against the deployed model; a real
purchase and refund through the production agent after the last deploy
(section 1); a real phone call that bought a non-refundable spa with "yes"
on the call; a replay of a signed request against the deployed shop code.

## 8. The case's hard rules, checked

- **Real money only from our own funds; no card numbers in code.** Stripe
  test mode on our own account; payments use Stripe's test payment method
  tokens (`pm_card_visa`), never a PAN. The web storefront's own checkout
  uses Stripe's hosted card element.
- **No private keys, seed phrases or API secrets in the repo.** The agent's
  signing key is generated on first start in its run directory
  (gitignored); Stripe, OpenRouter, ElevenLabs, Telegram keys live only in
  the VM's `.env`, typed through `deploy/set-secrets.sh`. The MCP address in
  section 2 is a demo access key for the jury and is rotated after the
  event.
- **Every spending agent has a hard cap enforced in code, not in the
  prompt.** `agent/session.py`: per purchase and per 24 hours, tested in
  `agent/agent_test.py` (a proposal over the cap is refused; an approved one
  over the cap stops before the shop is asked to charge).
- **No token launches, no speculative trading.** None.
- **Mocked payments labelled SIMULATED.** The mock rail is labelled on every
  screen that shows it; the deployed shops run on Stripe.
- **Fresh build.** The shop server starts from the UCP sample (the
  repository's second commit is the unmodified copy); everything else is
  ours. The hackathon's own rules let us bring our own boilerplate.

## 9. Run it yourself

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
pytest` (61), `cd console && uv run pytest` (17). Browser tests in
`tests-e2e/` (Node 22.12+, shops and agent running, scripted brain):
`npm install && npx playwright install chromium` once, then `npm test`
(five shop flows) and `npm run test:agent` (three agent flows).
`cd agent && uv run python eval_live.py` drives a running agent through the
demo's conversations and prints what the model cost per step.

`deploy/` runs it all on one VM with Docker Compose and Caddy; its
`README.md` has the steps, including the phone number
(`deploy/elevenlabs-voice.py`). Secrets live only in the VM's `deploy/.env`.

## 10. Reference

The detail of each part: what a shop sells, the two doors (agents over UCP,
people in a browser), the agent, the console, the Stripe rail, and the
layout of the repository.

### What a shop sells

A deal (a spa day) is bought through one of its options (2 hours, 3 hours,
full day). Each option has its own price and its own pool of codes; the codes
left are the stock. A purchase takes one code per unit and hands it over as
the voucher. A code is sold once: a refund voids it and it never goes back on
sale.

Deals carry rules as data, not text: dated (a set day) or open-dated
(validity and refund period counted from the purchase), a limit per person
with a repurchase period (kept by email), appointment required, and one promo
code per shop (`DUSK10`, `PRAHA15`, `DAWN5`).

**Booked for a date and time.** Every deal is booked when bought: its
catalogue row says on which days and hours it takes bookings, how far apart
its start times are, how many places each start time has and how far ahead
it can be booked (`slot_days`, `slot_hours`, `slot_minutes`,
`slot_capacity`, `booking_days_ahead` in `deals.csv`; times in the shop's
`timezone`). The shop lists its open slots with the places left at
`GET /deals/{id}/availability`; the web checkout offers them and an agent
sends the chosen one with the checkout (`bookings: {"<option>": "<start>"}`).
A slot the deal doesn't offer, a past one or a full one is refused before
anything is charged (`409 SLOT_UNAVAILABLE`), and so is paying without one
(`400 BOOKING_REQUIRED`). The order keeps the slot under `service.booking`;
a refund or the merchant's cancellation gives the place back
(`services/booking_service.py`).

**Coins.** Each shop has its own, worth $1 there, in a wallet per customer.
A card payment earns the shop's percentage back. A checkout can be paid with
coins, a card or both; it shows the exact split, and a refund returns each
part the way it came. Agents read `GET /wallet?email=` and send
`coins: {"use": n}`.

### The two doors

Both end in the same checkout code, order, voucher, payment and ledger events.

**For agents (UCP, `UCP-Agent` header, RFC 9421 signature):**

| Call | What it does |
|---|---|
| `GET /.well-known/ucp` | Capabilities, endpoint, keys |
| `POST /catalog/search`, `/catalog/lookup` | Deals by text, category and price; options are variants |
| `GET /deals/{id}/availability` | The deal's open slots by day, with the places left in each |
| `POST /checkout-sessions` | Open a checkout; line items carry the voucher terms; `bookings` names the slot |
| `POST /checkout-sessions/{id}/complete` | Pay; the order comes back with the voucher |
| `GET /orders/{id}` | The order: only for the agent that placed it, or the shop |
| `PUT /orders/{id}/booking` | Move the visit to another open slot; the agent that placed the order |
| `POST /orders/{id}/cancellation` | Cancel for a refund under the deal's terms; the agent that placed the order |
| `GET /wallet?email=` | The buyer's coins here and the shop's percentage back |

With `REQUIRE_SIGNATURES=1` (the default on the server) an unsigned request
gets `401 signature_missing`, a wrong key `401 signature_invalid`, and a
profile that is not public https `400 invalid_profile_url`. Each signature
also carries `expires` (five minutes after `created`, and never more than
eight) and a random `nonce`. A signature past its `expires`, made in the
future, or older than five minutes without `expires` gets `401
signature_expired`. The same nonce a second time inside its window gets `401
signature_replayed`, so a copied request is useless. With `REQUIRE_SIGNATURE_NONCE=1`
(also the default) a signature without a nonce gets `401 signature_invalid`.
The shop remembers nonces in memory, one process per shop. `scripts/replay-demo.py` shows it: one signed request, sent twice. If the total
moved since the agent last fetched the checkout, `complete` answers
`409 requires_consent` and charges nothing. Each agent order records the
agent's profile, the outcome of the signature check and when it was placed.
An agent may add `agent_context` to `complete` (outside UCP): its name, who
proposed the deal, through which channel the customer said yes
(`page`, `card:ChatGPT`, `card:Claude Desktop`, `telegram`), when, and where
it keeps its records. The shop keeps the known keys, as short strings, and
shows them to the buyer in "My vouchers" next to its own ledger of the order
(checkout opened, paid, voucher issued, used, refunded…), so a person can
tell what they bought themselves from what their agent bought, and how.

**For people (browser, cookies):** `/` and `/deals/{id}` to browse, `/cart`,
`/login`, `/account`, `/checkout/{id}` (needs an account), and `/vouchers`.
From an order's page the customer can move the visit to another open slot
(`POST /vouchers/{id}/reschedule`; the old slot is freed) and cancel the
order for a refund while the deal is refundable and its refund deadline has
not passed (`POST /vouchers/{id}/cancel`): the card payment and the coins go
back the way they came, the voucher is voided and its slot released. What
the customer can do by hand here, their agent can do over UCP.
There is no public registration: the shop creates accounts (`POST /accounts`
with the merchant secret; `shops/<shop>/users.csv` seeds them, and on the
server `deploy/add-account.sh` adds one). An order belongs to the account
whose email is its buyer, so an agent's purchase shows up in the person's
account, marked as the agent's. Passwords are scrypt hashes; cookies are
HttpOnly, and Secure behind HTTPS. The pages are plain HTML forms that
`routes/assets/shop.js` submits in place.

The web door does not guess whether a browser is driven by a person or an AI.
It recognises agents that declare themselves (published names such as
`ChatGPT-User`, or Web Bot Auth signatures): they may browse and see a notice,
and get `403` at the checkout with the way to the agent door. Every web order
keeps hints about its sender (headless browser, HTTP client, payment sent
without the page's script, paid within two seconds). Hints are not proof; they
are for whoever decides a complaint (`services/visitor.py`).

**For the marketplace** (`Simulation-Secret` header, the console's calls): `POST /orders/{id}/cancel`,
`/redeem`, `/refund`; `POST /testing/booking-fee` adds a fee to every
checkout; `POST /wallets/grant`, `GET /wallets/{email}`; `GET /inventory`,
`GET /inventory/{option}/codes`, `POST` to add codes; `GET /deals`;
`GET /orders`, every order; `GET /ledger`, the events of all three shops; `PUT /orders/{id}`;
`POST /accounts` and `PUT /accounts/{email}/password`.

### The agent

The person asks ("a spa day for two, refundable, under $120"). The agent
searches the three shops, reads the person's coins in each, and proposes one
deal: what, why, what it turned down and why, and the exact total with the
coins/card split. Each turn shows the signed requests it sent.

1. The brain (`agent/brain.py`) has the tools `search_deals`,
   `read_wallets`, `check_availability` and `propose_purchase`, and, for
   what was already bought, `list_purchases`, `reschedule_purchase` and
   `cancel_purchase`: everything the person could do by hand on the shop's
   order page ("move my spa day to Sunday at 12", "cancel the beer
   tasting"). None of them pays. A deal is booked for a date and time: the brain asks for them
   when the person gave none ("Saturday at 11:00", "mañana a las 18"), reads
   the shop's open slots and proposes with the slot's `starts_at`.
2. `propose_purchase` opens a checkout in the shop, booked for that slot; the
   page shows it with the total the shop will charge.
3. The person's approval is handled by code outside the brain
   (`agent/session.py`): it pays that checkout, once. A `409` from the shop
   stops it; the agent fetches the new total and asks again.
4. The receipt shows the voucher and compares charged with approved.

The agent is the person's: it keeps a **memory** in its run directory
(`memory.json`), separate from any model. Three parts: a profile (city, who
they buy for, a note), preferences the agent learns from what the person says
(the brain's `remember` tool; the person can drop any of them), and **hard
rules** only the person sets on the page: a maximum total, refundable deals
only, categories to avoid. The brain reads them with `recall` and sees them
at the start of every conversation, but the rules are enforced in code:
`propose_purchase` refuses a deal that breaks one before any checkout is
opened, whatever the brain asked. The page also lists **everything bought**
as the shops see it today (voucher code, state, what was charged) with a
printable **receipt** per purchase (`/receipts/{order}`) and the evidence.

The shop's data, the person's layout. The catalog sends everything the
shop's own page shows (photos, highlights, the merchant's blurb, top
reviews, terms) and the agent lays a proposal out the way its person set in
the memory's **presentation**: the best one or the three best to pick from
(a shortlist with "Pick this one", which opens that checkout instead), the
photos, the price or the terms first, full or brief detail, and the language
the agent writes in (English or Spanish; `texts.py` for the scripted brain
and the session, an instruction for the model).

**Another brain can use the agent as its tools.** `POST /mcp/kEZczaKChbWAH3U_5gnlC53RMxB_2DT-` serves
the agent's tools over MCP (JSON-RPC over HTTP): ChatGPT, Claude Code or any
assistant that speaks it connects and gets `search_deals`, `read_wallets`,
`recall`, `remember` and `propose_purchase` (plus `search` and `fetch`, as
ChatGPT's connectors expect). The external brain thinks; the agent signs,
keeps the memory and the rules, and puts the proposal in the person's
**Approvals** (`/approvals/{id}`, the link the assistant is handed), stamped
with who proposed it. In ChatGPT and in Claude (web and desktop) the
proposal also arrives as a card drawn inside the chat: `propose_purchase`
names the `ui://` resource `agent/widget.html`, which speaks both ChatGPT's
Apps SDK bridge and the MCP Apps standard (the server negotiates the
client's protocol version and declares the `io.modelcontextprotocol/ui`
extension, which Claude requires). The card's Approve and Decline call
`approve_from_card` / `decline_from_card` with a one-time token the result
carries in `_meta`, which reaches the card but never the model; the record
says the yes came "by card:ChatGPT" or "card:Claude". Clients without a UI
(Claude Code) get the link to Approvals instead. **Telegram** is another
channel of the same agent (`agent/telegram.py`): one bot per agent, the
chat linked once from the page; a message there is a turn of the
conversation, every proposal arrives as a card with Approve / Not this
one, and a tap is recorded as `method: telegram`. Approving happens only
on an interface the agent owns: its page, those cards, Telegram, voice
later. There is no tool that pays, for any brain. Proposals that wait survive a restart. The key is a secret in
the address, made on the first start (`mcp_key` in the run directory); the
page shows the full address under "What it knows about you".

Two brains, and the page says which one decides: a scripted stand-in (fixed
rules), or a language model over the OpenAI chat API (`agent/model_brain.py`)
when `OPENAI_API_KEY` is set; `AGENT_MODEL` names it, `OPENAI_BASE_URL` moves
it to another provider (OpenRouter: `https://openrouter.ai/api/v1` with its
model ids), `AGENT_MAX_TOKENS` caps an answer, `AGENT_BRAIN=scripted`
forces the stand-in. A request the model can't take (three tries with
growing waits on rate limits and server trouble) goes to
`AGENT_FALLBACK_MODEL` if one is set, and if that fails too the scripted
stand-in takes that turn and says so; a request no brain could take is
kept in the thread but dropped from the model's episode, so it is never
read later as a live order. Changes to a purchase (move the visit, cancel
for a refund) are proposals like a purchase: the brain can't apply them,
only the person's yes does. The model
brain is tested against a fake of the API and has not run against the real
one yet.

The agent signs every request with an ES256 key it keeps in its run directory
and publishes at `/profile.json`. It keeps a journal (`journal.jsonl`: every
message, tool call, proposal and receipt, with the channel each came
through; the page shows it as one thread that never resets, newest at the
bottom and paged back on scroll, marking what was said on Telegram or by
another assistant over MCP, and **History** reads it by decision; the brain
works on episodes of it, cut after three hours of silence) and writes each approval and what
came of it to `approvals.jsonl`, and `/evidence/{order}` lays that record next to the
shop's order with findings: approval on record, charged what was approved,
signature verified, this agent, this customer, and what it cannot prove yet
(the approval is a button press, not something the shop can verify).

Settings (customer, test card, shops) are in `agent/agent.json`;
`AGENT_NAME`, `AGENT_CUSTOMER_NAME` and `AGENT_CUSTOMER_EMAIL` say whose
agent an instance is, and `SHOP_<ID>_URL` and `SHOP_<ID>_PUBLIC_URL` override
a shop's addresses. On the server there is one agent per person, each with
its own key and record, on a name apart from the shops' domain: the shops
verify a key published somewhere they don't control, as they would in real
life.

### The console

One page for the marketplace's side of the three shops, with a shop selector
(`console/`). Orders: every purchase with who paid, what, how much on the
card and in coins, which door (agent with the signature's outcome, or web
with its hints) and the voucher's state. Opening one shows the facts, the
marketplace's one action, cancel and refund (the service is cancelled, the
card payment goes back through the rail, the coins to the wallet, the
voucher is voided and its slot released), the shop's events behind it, and,
for an agent order, the agent's side: the approval findings and the whole conversation that led to the
purchase. Other sections: the shop's events, the code inventory, customers
(wallet lookup, grant coins, open an account, set a password) and the demo
controls (the surprise fee). It calls the shops with the merchant secret and
the agents for their records (each order names the agent that placed it, by
its profile URL; `AGENT_URLS` maps profiles to addresses); it stores nothing
itself.

### Real and simulated, in detail

Real: the UCP protocol, signature checks, code pools and stock, voucher
states, and the payment when the shop runs on the `stripe` rail
(`--payment_rail=stripe`, Stripe's API in the mode of the key: a test key
authorises, captures and refunds without moving money). Simulated: the
catalogue with its prices, ratings and reviews; the payment on the `mock`
rail (every order and ledger line names its rail);
the merchant; the agent's decisions until a model is connected.

On the `stripe` rail the shop reads Stripe's fraud check (Radar) on every
charge and keeps it on the order. Set `AGENT_CARD_TOKEN` to one of Stripe's
test payment methods (https://docs.stripe.com/testing) to show each case:
`pm_card_visa` is a normal payment; `pm_card_riskLevelElevated` is sold and
marked "Fraud check: review" in the console, with a `PAYMENT_RISK_REVIEW`
ledger line; `pm_card_riskLevelHighest` is authorised, then cancelled by the
shop (`RISK_HIGHEST`); `pm_card_radarBlock` is blocked by Stripe
(`RISK_BLOCKED`); `pm_card_threeDSecure2Required` asks the person to confirm
with the bank, which an agent cannot do (`SHOPPER_ACTION_REQUIRED`). Stripe
may itself block the highest-risk card, depending on the account's Radar
settings; then the shop reports `RISK_BLOCKED` instead.

### Layout

- `rest/python/`: the UCP sample (Apache 2.0, `Universal-Commerce-Protocol/
  samples` at `01755bc`; the repository's second commit is the unmodified
  copy). Added under `server/`: `services/voucher_service.py`,
  `payment_rail.py`, `ledger.py`, `account_service.py`, `coin_service.py`,
  `booking_service.py`, `visitor.py`; `routes/catalog.py`, `storefront.py`, `web_checkout.py`,
  `account.py`, `voucher.py`, `wallet.py` with `routes/assets/`;
  `approval_checks.py` (stub for the buyer-approval checks); `voucher_test.py`.
- `shops/<a|b|c>/`: each shop's catalogue as CSV (`deals`, `options`,
  `codes`, `discounts`, `reviews`, `users`, `wallets`; each deal's booking
  calendar in `deals.csv`), `shop.json` for its look and timezone, `images/`
  (credits in `shops/CREDITS.md`).
- `agent/`: `app.py` (web app), `session.py` (conversation, tools, approval,
  evidence), `brain.py` and `model_brain.py`, `shops.py` (UCP calls),
  `signing.py`, `static/`.
- `console/`: the shops' console, `app.py` and `static/`.
- `scripts/`: start and stop the shops and the agent; the smoke test.
- `deploy/`: Dockerfiles are in each app; here the Compose file, Caddyfile,
  `.env.example`, `set-secrets.sh` and the steps.
- `tests-e2e/`: browser tests, the only Node code in the repo.
