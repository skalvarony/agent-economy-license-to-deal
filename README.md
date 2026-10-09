# Demo shops and their customer's agent

Three pretend shops that sell vouchers for local experiences, and the agent
that buys from them for a person. Each shop is the [UCP](https://ucp.dev)
sample server with voucher selling added, started with its own catalogue; the
agent (`agent/`) only talks to the shops over UCP. `PLAN.md` says what is
left and why.

| Shop | Local | Live | Sells |
|---|---|---|---|
| A, Dusk Deals | :8181 | https://a.licensetodeal.app | Wellness, refundable, 10% back in coins |
| B, Praha Pass | :8182 | https://b.licensetodeal.app | Food and activities, 5% back |
| C, Dawn Saver | :8183 | https://c.licensetodeal.app | Wellness, cheaper, non-refundable, no coins |
| The agents | :8190 | `ltd-agent-alvaro`, `ltd-agent-david`, `ltd-agent-emmanouil` `.duckdns.org` | One per person, on its own name, behind a password |
| The console | :8195 | https://admin.licensetodeal.app | The shops' side, same password |

## Run

Needs [uv](https://docs.astral.sh/uv/). Every start re-seeds the shops.

```shell
scripts/shops.sh start       # REQUIRE_SIGNATURES=1 to turn away unsigned agents
scripts/agent.sh start       # http://localhost:8190
scripts/console.sh start     # http://localhost:8195
python3 scripts/smoke.py     # 28 live checks: buys, books, moves, cancels, refunds, booking fee
scripts/agent.sh stop && scripts/shops.sh stop
```

Tests: `cd rest/python/server && uv run pytest` (360), `cd agent && uv run
pytest` (49), `cd console && uv run pytest` (16). Browser tests in
`tests-e2e/` (Node 22.12+, shops and agent running): `npm install && npx
playwright install chromium` once, then `npm test` (five shop flows) and
`npm run test:agent` (three agent flows). They use exact checks and need no
model.

`deploy/` runs it all on one VM with Docker Compose and Caddy; its `README.md`
has the steps. Secrets live only in the VM's `deploy/.env`.

## What a shop sells

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

## The two doors

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
profile that is not public https `400 invalid_profile_url`. If the total
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

## The agent

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

**Another brain can use the agent as its tools.** `POST /mcp/{key}` serves
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

## The console

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

## Real and simulated

Real: the UCP protocol, signature checks, code pools and stock, voucher
states, and the payment when the shop runs on the `stripe` rail
(`--payment_rail=stripe`, Stripe's API in the mode of the key: a test key
authorises, captures and refunds without moving money). Simulated: the
catalogue with its prices, ratings and reviews; the payment on the `mock`
rail (every order and ledger line names its rail);
the merchant; the agent's decisions until a model is connected.

## Layout

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
