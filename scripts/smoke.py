#!/usr/bin/env python3
"""Smoke test for the three demo shops (start them with scripts/shops.sh).

Walks the shops the way the demo does: find a deal in each shop, buy one in
shop A, have the merchant cancel it and refund it, then switch on the booking
fee and check that the purchase stops, then pay with coins and a card.
Stdlib only.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

SHOPS = {"a": 8181, "b": 8182, "c": 8183}
SECRET = os.environ.get("SIMULATION_SECRET", "demo-secret")
# The card to pay with: the shops' handler and its token. Against shops on
# the stripe rail: CARD_HANDLER=stripe CARD_TOKEN=pm_card_visa.
PAYMENT = {
  "payment": {
    "instruments": [{
      "id": "instr_1",
      "handler_id": os.environ.get("CARD_HANDLER", "mock_payment_handler"),
      "type": "card",
      "credential": {
        "type": "token",
        "token": os.environ.get("CARD_TOKEN", "success_token"),
      },
    }],
  },
  "risk_signals": {},
}

failures = 0


def check(label, ok, got=None):
  global failures
  failures += not ok
  print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f" (got {got})"))


def call(shop, method, path, body=None, merchant=False):
  """Call a shop as the agent, or as the merchant side when `merchant`."""
  headers = {"Content-Type": "application/json"}
  if merchant:
    headers["Simulation-Secret"] = SECRET
  else:
    headers.update({
      "UCP-Agent": 'profile="http://127.0.0.1:9/profile.json"',
      "idempotency-key": str(uuid.uuid4()),
      "request-id": str(uuid.uuid4()),
    })
  data = json.dumps(body).encode() if body is not None else None
  url = f"http://localhost:{SHOPS[shop]}{path}"
  req = urllib.request.Request(url, data=data, headers=headers, method=method)
  try:
    with urllib.request.urlopen(req, timeout=10) as resp:
      return resp.status, json.load(resp)
  except urllib.error.HTTPError as e:
    return e.code, json.load(e)


def search(shop, **body):
  return call(shop, "POST", "/catalog/search", body)[1].get("products", [])


def open_checkout(shop, deal_id, starts_at=None):
  """Open a checkout for one option; `starts_at` books its slot."""
  body = {
    "line_items": [{"item": {"id": deal_id}, "quantity": 1}],
    "buyer": {"email": "shopper@example.com", "full_name": "Demo Shopper"},
  }
  if starts_at:
    body["bookings"] = {deal_id: starts_at}
  return call(shop, "POST", "/checkout-sessions", body)


def first_slot(shop, deal_id):
  """The first open slot of a deal, from the shop's calendar."""
  _, listed = call(shop, "GET", f"/deals/{deal_id}/availability?days=14")
  return next((s for d in listed["days"] for s in d["slots"] if s["left"]),
              None), listed


def left_at(shop, deal_id, starts_at):
  """How many places a slot has left, as the shop lists it now."""
  _, listed = call(shop, "GET",
                   f"/deals/{deal_id}/availability?from={starts_at[:10]}&days=1")
  return next(s["left"] for d in listed["days"] for s in d["slots"]
              if s["starts_at"] == starts_at)


def pool(shop, option_id):
  """Return the counts of an option's codes, by status."""
  _, rows = call(shop, "GET", "/inventory", merchant=True)
  return next(r["codes"] for r in rows if r["option_id"] == option_id)


def total(checkout):
  return next(t["amount"] for t in checkout["totals"] if t["type"] == "total")


def main():
  # Discovery: each shop says who it is and what it can do.
  for shop in SHOPS:
    _, profile = call(shop, "GET", "/.well-known/ucp")
    caps = profile.get("ucp", {}).get("capabilities", {})
    check(f"shop {shop}: profile declares catalog and vouchers",
          "dev.ucp.shopping.catalog.search" in caps
          and "com.example.service_voucher" in caps, list(caps))

  # The request: wellness, at most $120, refundable.
  wanted = {"filters": {"categories": ["wellness"], "price": {"max": 12000}}}
  found = {shop: search(shop, **wanted) for shop in SHOPS}
  check("shop a: has refundable wellness deals",
        any(p["cancellation"]["refundability"] == "refundable"
            for p in found["a"]), found["a"])
  check("shop b: no wellness deals (rejected on category)", not found["b"],
        found["b"])
  check("shop c: wellness deals are all non-refundable (rejected)",
        found["c"] and all(
          p["cancellation"]["refundability"] == "non_refundable"
          for p in found["c"]), found["c"])

  status, sold_out = open_checkout("a", "thai_massage_90")
  check("shop a: sold-out deal refused", status == 400
        and sold_out["messages"][0]["code"] == "OUT_OF_STOCK", sold_out)

  # A deal's options are variants, each with its own price.
  spa = next(p for p in found["a"] if p["id"] == "spa_day_two")
  prices = {v["id"]: v["price"]["amount"] for v in spa["variants"]}
  check("shop a: the spa day has three options with their own prices",
        prices == {"spa_day_two_2h": 6900, "spa_day_two_3h": 9900,
                   "spa_day_two_day": 13900}, prices)
  before = pool("a", "spa_day_two_3h")
  check("shop a: the 3-hour option has a pool of 50 codes",
        before["total"] == 50, before)

  # The spa day is booked for a date and time: the shop lists its slots.
  slot, listed = first_slot("a", "spa_day_two")
  check("shop a: the spa day lists bookable slots with places left",
        slot is not None and listed["slot_minutes"] == 60
        and spa["service"]["booking"]["required"] is True, listed)
  places = left_at("a", "spa_day_two", slot["starts_at"])

  # Without a slot the shop takes no payment.
  _, unbooked = open_checkout("a", "spa_day_two_3h")
  status, refused = call(
    "a", "POST", f"/checkout-sessions/{unbooked['id']}/complete", PAYMENT)
  check("shop a: a purchase without a slot is refused before any charge",
        status == 400 and refused["messages"][0]["code"] == "BOOKING_REQUIRED",
        refused)

  # Buy the 3-hour spa day in shop A, for that slot.
  _, checkout = open_checkout("a", "spa_day_two_3h", slot["starts_at"])
  check("shop a: checkout shows the option, the terms and $99",
        total(checkout) == 9900
        and checkout["line_items"][0]["service"]["option"] == "3 hours",
        checkout)
  check("shop a: the checkout carries the slot",
        checkout["line_items"][0]["service"]["booking"]["starts_at"]
        == slot["starts_at"], checkout["line_items"][0]["service"])
  status, done = call(
    "a", "POST", f"/checkout-sessions/{checkout['id']}/complete", PAYMENT)
  check("shop a: purchase completes without shipping", status == 200, done)
  order_id = done["order"]["id"]
  _, order = call("a", "GET", f"/orders/{order_id}")
  line = order["line_items"][0]
  check("shop a: voucher issued and payment captured",
        line["voucher"]["status"] == "issued"
        and order["payment"]["status"] == "captured", order)
  check("shop a: the order is booked for the slot, and the slot has one"
        " place less",
        line["service"]["booking"]["status"] == "booked"
        and left_at("a", "spa_day_two", slot["starts_at"]) == places - 1,
        line["service"].get("booking"))

  # The agent that bought moves the visit to the next slot, and back.
  _, listed = call("a", "GET", "/deals/spa_day_two/availability?days=14")
  other = next((s for d in listed["days"] for s in d["slots"]
                if s["left"] and s["starts_at"] != slot["starts_at"]), None)
  status, moved = call("a", "PUT", f"/orders/{order_id}/booking",
                       {"starts_at": other["starts_at"]})
  check("shop a: the agent moves the visit to another open slot",
        status == 200
        and moved["line_items"][0]["service"]["booking"]["starts_at"]
        == other["starts_at"]
        and left_at("a", "spa_day_two", slot["starts_at"]) == places, moved)
  status, moved = call("a", "PUT", f"/orders/{order_id}/booking",
                       {"starts_at": slot["starts_at"]})
  check("shop a: and back to the first slot",
        status == 200 and left_at("a", "spa_day_two", slot["starts_at"])
        == places - 1, status)
  sold = pool("a", "spa_day_two_3h")
  check("shop a: the voucher's code came out of the option's pool",
        len(line["voucher"]["codes"]) == 1
        and sold["available"] == before["available"] - 1
        and sold["assigned"] == before["assigned"] + 1, sold)

  # The merchant cancels; the order is refunded.
  status, _ = call("a", "POST", f"/orders/{order_id}/cancel")
  check("shop a: agent can't cancel as the merchant", status in (403, 422),
        status)
  status, order = call("a", "POST", f"/orders/{order_id}/cancel",
                       merchant=True)
  check("shop a: merchant cancels",
        status == 200 and order["line_items"][0]["redemption"]["status"]
        == "cancelled_by_merchant", order)
  status, order = call("a", "POST", f"/orders/{order_id}/refund",
                       {"reason": "merchant cancelled"}, merchant=True)
  check("shop a: refund goes back and the voucher is void",
        status == 200 and order["payment"]["status"] == "refunded"
        and order["line_items"][0]["voucher"]["status"] == "refunded", order)
  voided = pool("a", "spa_day_two_3h")
  check("shop a: the refunded code is void, not back on sale",
        voided["void"] == sold["void"] + 1
        and voided["available"] == sold["available"], voided)
  check("shop a: the refund gives the slot's place back",
        left_at("a", "spa_day_two", slot["starts_at"]) == places, places)

  # Surprise fee: added after the checkout was shown, so nothing is charged.
  _, checkout = open_checkout("a", "spa_day_two_3h")
  call("a", "POST", "/testing/booking-fee", {"amount": 2500}, merchant=True)
  try:
    status, refused = call(
      "a", "POST", f"/checkout-sessions/{checkout['id']}/complete", PAYMENT)
    check("shop a: fee added after approval stops the purchase",
          status == 409
          and refused["messages"][0]["code"] == "requires_consent", refused)
    _, checkout = call("a", "GET", f"/checkout-sessions/{checkout['id']}")
    check("shop a: the checkout now shows $124", total(checkout) == 12400,
          checkout.get("totals"))
  finally:
    call("a", "POST", "/testing/booking-fee", {"amount": 0}, merchant=True)

  # Coins: the demo customer has 40 in shop A, which gives 10% back.
  demo = {"email": "demo@example.com", "full_name": "Demo Shopper"}
  _, wallet = call("a", "GET", "/wallet?email=demo@example.com")
  check("shop a: the demo customer's wallet holds 40 coins",
        wallet["balance"] == 40 and wallet["back_percent"] == 10, wallet)
  # The spa day is booked for a slot, like every deal; the refund above gave
  # the first slot's place back, so the calendar offers it again.
  slot, _ = first_slot("a", "spa_day_two")
  _, checkout = call("a", "POST", "/checkout-sessions", {
    "line_items": [{"item": {"id": "spa_day_two_3h"}, "quantity": 1}],
    "buyer": demo, "coins": {"use": 40},
    "bookings": {"spa_day_two_3h": slot["starts_at"]},
  })
  split = {t["type"]: t["amount"] for t in checkout["totals"]}
  check("shop a: 40 coins and $59 on the card",
        split == {"subtotal": 9900, "coins": -4000, "total": 5900}, split)
  _, done = call(
    "a", "POST", f"/checkout-sessions/{checkout['id']}/complete", PAYMENT)
  _, order = call("a", "GET", f"/orders/{done['order']['id']}")
  _, wallet = call("a", "GET", "/wallet?email=demo@example.com")
  check("shop a: the order records the split and the wallet earns 5 back",
        order["payment"]["amount"] == 5900 and order["payment"]["coins"] == 40
        and wallet["balance"] == 5, [order["payment"], wallet])
  call("a", "POST", f"/orders/{order['id']}/refund", {}, merchant=True)
  _, wallet = call("a", "GET", "/wallet?email=demo@example.com")
  check("shop a: a refund puts the coins back in the wallet",
        wallet["balance"] == 40, wallet)

  _, ledger = call("a", "GET", "/ledger", merchant=True)
  events = [e["event"] for e in ledger]
  # The checkout refused for want of a slot, the booked purchase, its move
  # and return, the merchant's cancellation, the fee, and the coins purchase.
  expected = ["CHECKOUT_CREATED", "CHECKOUT_CREATED", "PAYMENT_CONFIRMED",
              "VOUCHER_ISSUED", "BOOKING_CHANGED", "BOOKING_CHANGED",
              "MERCHANT_CANCELLED", "REFUND_AUTHORISED", "CHECKOUT_CREATED",
              "CHECKOUT_CHANGED", "CHECKOUT_CREATED", "PAYMENT_CONFIRMED",
              "VOUCHER_ISSUED", "REFUND_AUTHORISED"]
  check("ledger: this run's events, in order",
        events[-len(expected):] == expected, events)

  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
