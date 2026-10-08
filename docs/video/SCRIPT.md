# The demo film: shot list

This is the script the video agent reads. Edit it to change the film; keep
the rules in `BRIEF.md`; write what you thought of each version in
`NOTES.md`. Hard limit: 2:00. One caption at a time, exactly the words below.

All shots are recordings of the running local stack (`record/record.mjs`),
in the order they are filmed, so each shows a believable state.

| # | Time (v1) | Shot: what is on screen and what happens | Camera | Caption |
|---|---|---|---|---|
| 1 | 0:00–0:21 | Agent web app, empty thread, composer centred. "a spa day for two this Saturday, refundable, under $100" typed at human pace; send. The agent's steps ("3 signed requests to 3 shops"), then the proposal card: photos, Spa Day for Two, Dusk Deals, the breakdown to "Your card pays $49.10", Approve. | Wide, then a slow punch-in to the total and the button. | Your agent searches three marketplaces and proposes. It cannot pay. |
| 2 | 0:21–0:29 | Cursor to Approve, click. "Approved. Paying the shop…", the receipt with the voucher code, "Charged exactly what you approved: $49.10". | Punch-in on the code. | You approve. Only then it pays: the exact total, nothing else. |
| 3 | 0:29–0:42 | "the three best beer tastings for two, under $40": the three-card shortlist (Praha Pass), approve one ($14.75), the receipt. | Wide. | Ask for the three best; pick one. |
| 4 | 0:42–0:57 | Dusk Deals, My vouchers: the row "Your AI agent · approved on the agent's page"; open the spa order; "Who bought this" (placed by the agent over UCP, signature verified, You approved it, Agent's records) beside "What the shop recorded" (checkout opened, paid by your AI agent, voucher issued). | Pan down, then one punch-in framing both cards. | The shop verified the agent's signature and shows who bought, and how. |
| 5 | 0:57–1:18 | Shops console: Orders; the spa order's panel (card $49.10, coins, rail, door, "signature verified", the payment reference, the agent's side); then the beer order, Refund, a reason, "Yes, go ahead"; the list shows Refunded. | Punch-in on the payment block, cut to the refund. | Marketplaces get a console: every order, the payment behind it, a refund in one click. |
| 6 | 1:18–1:29 | Venue simulator: the spa arrival; the ticket with the code; "Customer arrives, voucher honoured"; the state becomes used. | Punch-in on the ticket. | At the venue the code is honoured; the shop and the agent see it at once. |
| 7 | 1:29–1:42 | Agent, Your purchases: the spa as Used, the beer tasting as Refunded; the spa's ticket panel ("Used at the venue", the Paid lines, "Approved · Web"). | Pan to the panel. | Both sides keep the record; the evidence page cross-checks them. |
| 8 | 1:42–1:49 | The evidence page for the spa order: the findings, "What the agent recorded" beside "What the shop says". | Still. | (none, so the columns stay readable) |
| 9 | 1:49–1:53 | The product mark, "License to Deal", "licensetodeal.app". | Still. | |

## Left out, on purpose

- Telegram and the ChatGPT / Claude approval cards: they cannot be recorded
  from the local stack. If we record them on the real phone and desktop, they
  go between shots 2 and 3 (≈10 s) and something else gets shorter.
- The History view and the shop sign-in: cut for length; the sign-in happens
  off camera.
- Payments show the mock rail ("Simulated · no money moved") because the
  footage is local. Recording against production would show Stripe, test mode.
