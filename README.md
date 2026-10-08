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

## Run

Needs [uv](https://docs.astral.sh/uv/). Every start re-seeds the shops.

```shell
scripts/shops.sh start       # REQUIRE_SIGNATURES=1 to turn away unsigned agents
python3 scripts/smoke.py     # 24 live checks: buys, cancels, refunds, booking fee
scripts/shops.sh stop
```

Tests: `cd rest/python/server && uv run pytest` (340), `cd agent && uv run
pytest` (40). Browser tests in
`tests-e2e/` (Node 22.12+, shops running): `npm install && npx playwright
install chromium` once, then `npm test` (five shop flows). They use exact
checks and need no model. Each shop also ships as a container (`docker build
rest/python/server`); `SHOP`, `SIMULATION_SECRET` and the other knobs are
listed at the top of `docker-entrypoint.sh`.

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
| `POST /checkout-sessions` | Open a checkout; line items carry the voucher terms |
| `POST /checkout-sessions/{id}/complete` | Pay; the order comes back with the voucher |
| `GET /orders/{id}` | The order: only for the agent that placed it, or the shop |
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
There is no public registration: the shop creates accounts (`POST /accounts`
with the merchant secret; `shops/<shop>/users.csv` seeds them). An order belongs to the account
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

**For the merchant** (`Simulation-Secret` header): `POST /orders/{id}/cancel`,
`/redeem`, `/refund`; `POST /testing/booking-fee` adds a fee to every
checkout; `POST /wallets/grant`, `GET /wallets/{email}`; `GET /inventory`,
`GET /inventory/{option}/codes`, `POST` to add codes; `GET /deals`;
`GET /orders`, every order; `GET /ledger`, the events of all three shops; `PUT /orders/{id}`;
`POST /accounts` and `PUT /accounts/{email}/password`.


## The agent

The person asks ("a spa day for two, refundable, under $120"). The agent
searches the three shops, reads the person's coins in each, and proposes one
deal: what, why, what it turned down and why, and the exact total with the
coins/card split.

1. The brain (`agent/brain.py`) has three tools, `search_deals`,
   `read_wallets` and `propose_purchase`. None of them pays.
2. `propose_purchase` opens a checkout in the shop, with the total the shop
   will charge.
3. The person's approval is handled by code outside the brain
   (`agent/session.py`): it pays that checkout, once. A `409` from the shop
   stops it; the agent fetches the new total and asks again.
4. The receipt records the voucher and compares charged with approved.

The agent is the person's: it keeps a **memory** in its run directory
(`memory.json`), separate from any model. Three parts: a profile (city, who
they buy for, a note), preferences the agent learns from what the person says
(the brain's `remember` tool; the person can drop any of them), and **hard
rules** only the person sets: a maximum total, refundable deals only,
categories to avoid. The brain reads them with `recall` and sees them at the
start of every conversation, but the rules are enforced in code:
`propose_purchase` refuses a deal that breaks one before any checkout is
opened, whatever the brain asked. The memory's **presentation** says how a
proposal is laid out: the best one or the three best to pick from, the
photos, the price or the terms first, full or brief detail, and the language
the agent writes in (English or Spanish; `texts.py` for the scripted brain
and the session, an instruction for the model).

**Another brain can use the agent as its tools.** `POST /mcp/{key}` serves
the agent's tools over MCP (JSON-RPC over HTTP): ChatGPT, Claude Code or any
assistant that speaks it connects and gets `search_deals`, `read_wallets`,
`recall`, `remember` and `propose_purchase` (plus `search` and `fetch`, as
ChatGPT's connectors expect). The external brain thinks; the agent signs,
keeps the memory and the rules, and holds the proposal until the person
approves it on an interface the agent owns, stamped with who proposed it. In
ChatGPT and in Claude (web and desktop) the proposal also arrives as a card
drawn inside the chat: `propose_purchase` names the `ui://` resource
`agent/widget.html`, which speaks both ChatGPT's Apps SDK bridge and the MCP
Apps standard (the server negotiates the client's protocol version and
declares the `io.modelcontextprotocol/ui` extension, which Claude requires).
The card's Approve and Decline call `approve_from_card` / `decline_from_card`
with a one-time token the result carries in `_meta`, which reaches the card
but never the model; the record says the yes came "by card:ChatGPT" or
"card:Claude". Clients without a UI (Claude Code) get a link to the proposal
instead. **Telegram** is another channel of the same agent
(`agent/telegram.py`): one bot per agent, the chat linked once with a
one-time code; a message there is a turn of the conversation, every proposal
arrives as a card with Approve / Not this one, and a tap is recorded as
`method: telegram`. Approving happens only on an interface the agent owns:
those cards, Telegram, voice later. There is no tool that pays, for any
brain. Proposals that wait survive a restart. The key is a secret in the address, made on the first
start (`mcp_key` in the run directory).

Two brains: a scripted stand-in (fixed rules), or a language model over the
OpenAI chat API (`agent/model_brain.py`) when `OPENAI_API_KEY` is set;
`AGENT_MODEL` names it, `OPENAI_BASE_URL` moves it to another provider,
`AGENT_BRAIN=scripted` forces the stand-in.

The agent signs every request with an ES256 key it keeps in its run directory
(`agent/signing.py`) and publishes as its profile. It keeps a journal
(`journal.jsonl`: every message, tool call, proposal and receipt) and writes
each approval and what came of it to `approvals.jsonl`, its own record of
what the person agreed to. Settings (customer, test card, shops) are in
`agent/agent.json`; `AGENT_NAME`, `AGENT_CUSTOMER_NAME` and
`AGENT_CUSTOMER_EMAIL` say whose agent an instance is, and `SHOP_<ID>_URL`
and `SHOP_<ID>_PUBLIC_URL` override a shop's addresses.

## Real and simulated

Real: the UCP protocol, signature checks, code pools and stock, voucher
states, and the payment when the shop runs on the `stripe` rail
(`--payment_rail=stripe`, Stripe's API in the mode of the key: a test key
authorises, captures and refunds without moving money). Simulated: the
catalogue with its prices, ratings and reviews; the payment on the `mock`
rail (every order and ledger line names its rail); redemption at the venue;
the merchant; the agent's decisions until a model is connected.


## Layout


- `rest/python/`: the UCP sample (Apache 2.0, `Universal-Commerce-Protocol/
  samples` at `01755bc`; the repository's second commit is the unmodified
  copy). Added under `server/`: `services/voucher_service.py`,
  `payment_rail.py`, `ledger.py`, `account_service.py`, `coin_service.py`,
  `visitor.py`; `routes/catalog.py`, `storefront.py`, `web_checkout.py`,
  `account.py`, `voucher.py`, `wallet.py` with `routes/assets/`;
- `agent/`: `session.py` (conversation, tools, approval), `brain.py` and
  `model_brain.py`, `shops.py` (UCP calls), `signing.py`, `memory.py`, `mcp.py` (the tool
  server) with `widget.html` (the card).
- `shops/<a|b|c>/`: each shop's catalogue as CSV (`deals`, `options`,
  `codes`, `discounts`, `reviews`, `users`, `wallets`), `shop.json` for its
  look, `images/` (credits in `shops/CREDITS.md`).
- `scripts/`: start and stop the shops; the smoke test.
- `deploy/`: Dockerfiles are in each app; here the Compose file, Caddyfile,
  `.env.example`, the front page and the steps.
- `tests-e2e/`: browser tests, the only Node code in the repo.
