# Service voucher

An extension of UCP's checkout and order for shops that sell vouchers for
services: something paid for now, used later and delivered by someone else.
UCP describes goods that ship; it has no service date, expiry or redemption.

## Deals and options

A deal is one catalog product. Each way to buy it (30, 60 or 90 minutes, say)
is an option: a variant of the product with its own price and its own pool of
codes. The variant's ID is the item to put in a checkout. An option is
available while its pool has codes left.

## Where the fields appear

- Catalog products (`/catalog/search`, `/catalog/lookup`).
- Each line item of a checkout, so the buyer approves the terms with the price.
- Each line item of an order, with the voucher's code and state added.

## Fields

| Field | Meaning |
|---|---|
| `service.deal_id` | The deal the option belongs to |
| `service.option` (line items) | The option bought, e.g. `60 minutes` |
| `service.category` | What kind of service it is |
| `service.merchant` | Who delivers it |
| `service.location` | Where |
| `service.includes` (line items) | What the option bought includes |
| `service.window` | `not_before` and `not_after`: when the service happens. Dated deals only |
| `cancellation.refundability` | `refundable`, `partially_refundable` or `non_refundable` |
| `cancellation.refundable_until` | Last moment a refund is due |
| `cancellation.refund_days` | Open-dated deals: days after buying in which a refund is due |
| `voucher.expires_at` | When the promotional value runs out |
| `voucher.valid_days` | Open-dated deals: days after buying the promotional value lasts |
| `voucher.amount_paid_expires` | Always false: what was paid is never lost |
| `redemption.method` | How the voucher is used, e.g. `code_at_venue` |
| `redemption.instructions` | What the customer has to do |
| `redemption.appointment_required` | Whether the service has to be booked ahead |
| `redemption.booking_contact` (order) | How to reach the merchant to book |
| `purchase.limit_per_person` | Most units one person may buy, across the deal's options |
| `purchase.repurchase_days` | Days after which a person may buy again |
| `voucher.codes` (order) | One code per unit bought, shown at the venue |
| `voucher.status` (order) | `issued`, `redeemed` or `refunded` |
| `redemption.status` (order) | `unredeemed`, `redeemed`, `redemption_failed`, `cancelled_by_merchant` or `cancelled_by_shopper` |

All times are ISO 8601 with an offset. The JSON Schema is at
`/schemas/service_voucher.json`.

## Dated and open-dated deals

A dated deal has a `service.window` and fixed deadlines. An open-dated deal has
none: the service is booked after buying, and its deadlines are counted in days
from the purchase (`voucher.valid_days`, `cancellation.refund_days`). On the
order those become dates (`voucher.expires_at`, `cancellation.refundable_until`).

## Promo codes

A catalog product lists the codes the shop advertises under `promotions`. A
platform applies one with UCP's discount extension, `discounts.codes`.

## Coins

A shop may give its customers coins: its own currency, one coin worth one unit
of the shop's currency, held in a wallet per buyer email.

- A catalog product says what the shop gives back under `rewards`
  (`coins_back_percent`, `coin_value`), and each variant says how many coins
  buying it earns (`coins_earned`).
- `GET /wallet?email=` tells a platform what the buyer's wallet holds.
- A checkout takes `coins: {"use": n}`. The answer's `coins` says how many
  were `applied` (never more than the wallet holds or the purchase costs), the
  wallet's `balance` and what the purchase `earns`. The totals carry a `coins`
  line, and `total` is what is left to pay by card.
- When coins cover the whole purchase, `complete` needs no payment instrument.
- An order's `payment` records the split: `amount` by card, `coins` from the
  wallet, and `coins_earned`. A refund sends each part back where it came from.

## Checkout behaviour

- A checkout that asks for more units of a deal than `purchase.limit_per_person`
  answers `409 PURCHASE_LIMIT_REACHED`. On `complete` the units the buyer
  already bought count too, by the buyer's email, until `repurchase_days` have
  passed. A refunded purchase stops counting.

- A checkout of vouchers needs no fulfillment: the voucher comes with the order.
- If the total changes after the platform last fetched the checkout, `complete`
  answers `409 requires_consent` and charges nothing. The platform fetches the
  checkout again and asks the buyer.
