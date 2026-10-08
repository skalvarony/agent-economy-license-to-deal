# Demo shops and their customer's agent

Three pretend shops that sell vouchers for local experiences, and the agent
that buys from them for a person. Each shop is the [UCP](https://ucp.dev)
sample server with voucher selling added, started with its own catalogue; the
agent (`agent/`) only talks to the shops over UCP. `PLAN.md` says what is
left and why.

| Shop | Local | Sells |
|---|---|---|
| A, Dusk Deals | :8181 | Wellness, refundable, 10% back in coins |
| B, Praha Pass | :8182 | Food and activities, 5% back |
| C, Dawn Saver | :8183 | Wellness, cheaper, non-refundable, no coins |

## Run

Needs [uv](https://docs.astral.sh/uv/). Every start re-seeds the shops.

```shell
scripts/shops.sh start       # REQUIRE_SIGNATURES=1 to turn away unsigned agents
python3 scripts/smoke.py     # 24 live checks: buys, cancels, refunds, booking fee
scripts/shops.sh stop
```

Tests: `cd rest/python/server && uv run pytest` (245). Each shop also ships
as a container (`docker build rest/python/server`); `SHOP`, `SIMULATION_SECRET`
and the other knobs are listed at the top of `docker-entrypoint.sh`.

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


## The agent door

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
it keeps its records.

**For the merchant** (`Simulation-Secret` header): `POST /orders/{id}/cancel`,
`/redeem`, `/refund`; `POST /testing/booking-fee` adds a fee to every
checkout; `POST /wallets/grant`, `GET /wallets/{email}`; `GET /inventory`,
`GET /inventory/{option}/codes`, `POST` to add codes; `GET /deals`;
`GET /orders`, every order; `GET /ledger`, the events of all three shops; `PUT /orders/{id}`.

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
  `payment_rail.py`, `ledger.py`, `account_service.py`, `coin_service.py`;
  `routes/catalog.py`, `voucher.py`, `wallet.py`; `approval_checks.py` (stub
  for the buyer-approval checks).
- `shops/<a|b|c>/`: each shop's catalogue as CSV (`deals`, `options`,
  `codes`, `discounts`, `reviews`, `users`, `wallets`), `shop.json` for its
  look, `images/` (credits in `shops/CREDITS.md`).
- `scripts/`: start and stop the shops; the smoke test.
