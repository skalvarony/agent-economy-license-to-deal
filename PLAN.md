# Plan

Where the entry stands, what is left and what was decided. The team's single
place for it. `README.md` says how what exists works.

Last updated: 5 October 2026.

## The entry

- **Event:** Agents 0.0.7 hackathon, Prague. Thu 8 Oct 16:00 to Fri 9 Oct
  12:00; building from 21:00, code freeze 07:14, 3-minute pitch.
- **Track:** 2, Agentic Economy. Team: License to Deal. Álvaro (shops, agent,
  infrastructure), David (payment account, demo, pitch), Emmanouil (takes
  work as it is handed out).
- **One line:** an AI agent buys a local experience for a person; the shop can
  check that the agent is genuine and that the person approved that exact
  purchase; the approval is kept as proof for any complaint later.
- **Where it runs:** one VM (Hetzner, Falkenstein) with Docker Compose and
  Caddy, since 3 Oct: `https://licensetodeal.app` (front page),
  `https://a|b|c.licensetodeal.app`, `admin.` and `venue.` (password). The
  older `licensetodeal.duckdns.org` names still answer. The agents live
  apart, one per person: `ltd-agent-alvaro`, `ltd-agent-david` and
  `ltd-agent-emmanouil.duckdns.org`. Code in the private
  repository `skalvarony/agent-economy-license-to-deal`. Everything also runs locally,
  which is the fallback for the demo.

## Ground rules

1. No employer name, data, systems or accounts anywhere: code, data or demo.
2. Play money only; the demo pays with a test card.
3. Throwaway code: private, never production.
4. Say openly what was built before the night and what is simulated.

## Status

**Done before the night**

- Three shops: catalogue, options with code pools, deal rules, promo codes,
  coins and mixed payment, accounts, web storefront, merchant actions,
  shared ledger.
- The agent: searches and compares the three shops, proposes with the exact
  total, buys only on the person's approval, stops when the total changes,
  signs every request, keeps its record. Scripted brain active; OpenAI brain
  written, untested against the real API.
- Trust so far: unsigned or wrongly signed agents refused (401); declared
  agents on the web sent to the agent door (403); orders record agent,
  signature outcome and time; web orders keep sender hints; `/evidence/
  {order}` for "I never bought this".
- The shops' console (`console/`, admin.licensetodeal.app): orders
  with who paid what and through which door, merchant actions (redeem,
  cancel, refund), the shop's events, the agent's approval and conversation
  behind each agent order, inventory, customers, the surprise-fee control.
- The agent as a personal shopping harness: memory per person (profile,
  learnt preferences, hard rules enforced in code), everything bought with
  the shop's current view, printable receipts, and the proposal laid out
  the person's way (one or three to pick from, photos/price/terms first,
  full or brief, English or Spanish) from the data the shops send over UCP.
- The agent as a tool server (MCP): ChatGPT or Claude Code as the brain,
  the agent signing, keeping memory and rules, and the person approving on
  the page. Tried with Claude Code: it searched and proposed; nothing paid.
- The venue simulator (`venue/`, venue.licensetodeal.app): every
  shop's deals and purchases; what happens at the merchant's venue (honoured,
  turned away, cancelled, no-show) and what the marketplace does about it
  (refund, voided code, goodwill coins), logged.
- Deployed with HTTPS, firewall, secrets on the machine. 326 + 23 + 5 + 5
  tests, a 24-step smoke test, 8 browser flows.

**The five scenarios**

| Scenario | Works today | Missing |
|---|---|---|
| 1 Normal purchase | End to end from the agent's page, signature verified; also with an external brain over MCP | The model with a real key |
| 2 Surprise fee | Shop refuses the changed total; agent pays nothing and asks again | Binding to the signed approval |
| 3 Fake bot | 401 unsigned, 401 wrong key, 400 private profile, 403 on the web | Nothing |
| 4 Merchant cancels | From the venue page: cancel or turn away → refund of card and coins, code voided, goodwill coins | Voucher expiry; a refund rule beyond "the merchant's failure refunds in full" |
| 5 "I never bought this" | Evidence page: approval vs. what the shop charged, signature, agent, buyer | The approval the shop can verify itself |

**Not started, lower priority:** changes of mind (date, quantity, option),
refund as coins with a bonus, coins to cash, redemption through the agent,
incentives across shops.

## To do

| Task | Owner | Notes |
|---|---|---|
| Invite David and Emmanouil to the repository; share addresses and passwords by a safe channel | Álvaro | |
| OpenAI key; run the model brain for real and tune its instructions | Álvaro | OpenAI is an event partner; credits announced, not confirmed |
| Telegram as a channel: the proposal and the Approve button on the phone, laid out the same way | Álvaro | Free, no phone number; needs a bot from BotFather |
| A "what the shop gains" panel in the console: agent vs human purchases, refunds | Álvaro | Two hours; the data is in the ledger |
| Demo script for the five scenarios, pitch, 2-minute video | David | Álvaro can draft the script with exact commands |
| Organisers' answers: pre-work, credits, pitch slot, private repository | David | |
| Decide Masumi | Team, 5 Oct | See open questions |

**During the night:** the signed approval of the person (AP2 mandate) that
the shop verifies, and the five checks in `approval_checks.py` (401
signature, 403 approval, 409 change, 409 stock, 402 payment); approval by
voice (ElevenLabs) ending in that record; recording the scenarios and
rehearsing.

## Simulated, and labelled as such

The catalogue with its prices, ratings and reviews; payments, in Stripe's
test mode (the code is the one that would move money with live keys);
redemption at the venue; the merchant, a set of actions on the shop; the
agent's decisions until a model is connected.

## Open questions

1. **Real payment.** Goes beyond rule 2; needs the sponsor's OK and a verified
   payment account. The demo runs in Stripe's test mode (decided 5 Oct); the
   switch to live is a pair of keys.
2. **Voice approval as proof.** A spoken "yes" has to end in a record the shop
   can verify, or the entry loses what makes it different.
3. **One payee or three.** One Stripe account is enough if only one shop takes
   payments in the demo.
4. **The merchant.** Its actions live inside the shop; a separate system the
   agent talks to would be closer to real redemption.
5. **Email as identity.** Limits, account ownership and wallets go by the
   buyer's unverified email. The signed approval should close this.
6. **Masumi.** Event partner: a payment network for AI agents on Cardano with
   escrow, refunds, agent identities, a registry and a decision log, which is
   the track's brief almost word for word. Replace or complement Stripe? Log
   approvals there? Test network and setup cost not yet looked at.

## Decisions

| Date | Decision |
|---|---|
| 29 Sep | Plan approved by the sponsor; the demo pays with a test card |
| 30 Sep | Added coin wallets, mixed payment, changes of mind, redemption through the agent |
| 1 Oct | Shops built before the event on the UCP sample; a refunded code is voided, never resold |
| 2 Oct | Three shops stay; coins and mixed payment are core; a coin is $1 in its shop, earned on what the card pays |
| 2 Oct | Payment provider: Stripe. The night goes to the agent and approval by voice. Say openly what was built before |
| 2 Oct | The agent's page, tools and approval flow are built before the night; the model can propose but never pay |
| 3 Oct | Shops don't try to detect agents posing as people; they recognise declared agents and keep hints on web orders |
| 3 Oct | Hosting: one VM with Docker Compose and Caddy and a free DuckDNS name; not Vercel, not split. Deployed the same day |
| 5 Oct | No public registration in the shops: the shop creates the accounts (Álvaro registers the ones the team asks for) |
| 5 Oct | One console for the three shops (merchant side), not one per shop |
| 5 Oct | The merchant's failure (turned away, cancelled) refunds in full plus 5 goodwill coins; a no-show refunds nothing |
| 5 Oct | Real payments are a goal of the entry, not only a demo detail: Stripe in live mode. The sponsor's OK (open question 1) is still to be confirmed |
| 5 Oct | Own domain, licensetodeal.app, so the Stripe account can be activated against a credible website |
| 5 Oct | One agent per person, each on its own name outside the shops' domain, with its own key: the shops verify a key they don't control |
| 5 Oct | The agent is a personal shopping harness: memory and hard rules per person, enforced by the app; channels (Telegram, MCP, voice) plug into the same approval. WhatsApp and SMS left out |
| 5 Oct | The interface moves from the shop to the agent: shops send data over UCP (photos, highlights, reviews, terms), the agent presents it the person's way. Web first, Telegram next |
| 6 Oct | Everything stays in test mode for the hackathon; the front page says so. No legal pages or contact address: nothing is sold and no real data is taken |
| 6 Oct | The agent's proposal is a card inside ChatGPT and Claude (MCP Apps / Apps SDK), with the person's Approve on it; the model has no approve tool and never sees the card's token. The team's pages have their own sign-in. The venue is the merchant's view; the console an inbox |
| 5 Oct | Payments through Stripe in test mode (sandbox), not live: same code and same flow, no real money and no sponsor approval needed. The shop never sees a card number: Stripe's form in the browser, a test PaymentMethod for the agents. In production the same day: agent purchase, web purchase, declined card and refund all seen in the Stripe dashboard; the MCP endpoint tried from ChatGPT's connector |
