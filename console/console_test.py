"""Tests of the console against fakes of the shops and the agent."""

import json

import app as console
from fastapi.testclient import TestClient
import httpx
import pytest

ORDER = {
  "id": "o1",
  "checkout_id": "c1",
  "placed_at": "2026-10-05T10:00:00+00:00",
  "channel": "agent",
  "agent": "https://agent.test/profile.json",
  "signature": {"status": "verified", "keyid": "k"},
  "buyer": {"email": "ana@example.com", "full_name": "Ana"},
  "payment": {
    "amount": 4910,
    "coins": 40,
    "coins_earned": 4,
    "status": "captured",
    "rail": "mock",
  },
  "line_items": [
    {
      "item": {"title": "Spa Day · 3 hours"},
      "quantity": {"total": 1},
      "voucher": {"codes": ["DSK-1"], "status": "issued"},
      "redemption": {"status": "unredeemed"},
    }
  ],
}
LEDGER = [
  {
    "at": "t1",
    "seller": "Shop A",
    "event": "CHECKOUT_CREATED",
    "checkout_id": "c1",
  },
  {
    "at": "t2",
    "seller": "Shop B",
    "event": "CHECKOUT_CREATED",
    "checkout_id": "zz",
  },
  {
    "at": "t3",
    "seller": "Shop A",
    "event": "PAYMENT_CONFIRMED",
    "checkout_id": "c1",
    "detail": {"order_id": "o1"},
  },
  {
    "at": "t4",
    "seller": "Shop A",
    "event": "PAYMENT_DECLINED",
    "checkout_id": "c2",
    "detail": {"total": 3900, "code": "GENERIC_DECLINE", "channel": "web"},
  },
]
PAYMENT_INFO = {
  "rail": "stripe",
  "id": "pi_1",
  "mode": "test",
  "status": "captured",
  "amount": 4910,
  "captured": 4910,
  "refunded": 0,
  "currency": "USD",
  "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2034},
  "created": "2026-10-05T10:00:00+00:00",
  "refunds": [],
  "url": "https://dashboard.stripe.com/test/payments/pi_1",
}


def fake_world(seen):
  """A transport that plays shop A and the agent."""

  def answer(request: httpx.Request):
    seen.append(
      (
        request.method,
        request.url.host,
        request.url.path,
        request.headers.get("simulation-secret"),
      )
    )
    path = request.url.path
    if request.url.host == "agent.test":
      if path == "/api/evidence/o1":
        return httpx.Response(
          200, json={"findings": [{"ok": True, "text": "fine"}]}
        )
      if path == "/api/conversations/o1":
        return httpx.Response(
          200,
          json={
            "brain": "Scripted",
            "events": [{"type": "person", "text": "a spa day"}],
          },
        )
      return httpx.Response(404, json={"detail": "no"})
    if request.headers.get("simulation-secret") != "s3cret":
      return httpx.Response(403, json={"detail": "Invalid Simulation Secret"})
    if path == "/orders":
      return httpx.Response(200, json=[ORDER])
    if path == "/ledger":
      return httpx.Response(200, json=LEDGER)
    if path == "/orders/o1/payment":
      return httpx.Response(200, json=PAYMENT_INFO)
    if path == "/orders/o1/cancel":
      return httpx.Response(200, json={"cancelled": True})
    if path == "/orders/o1/refund":
      return httpx.Response(
        200, json={"refunded": json.loads(request.content)["reason"]}
      )
    return httpx.Response(404, json={"detail": f"no {path}"})

  return httpx.MockTransport(answer)


@pytest.fixture
def seen():
  """The paths the fakes were asked for, in order."""
  return []


@pytest.fixture
def client(monkeypatch, tmp_path, seen):
  monkeypatch.setattr(console, "SECRET", "s3cret")
  settings = {
    "name": "Console",
    "shops": [
      {"id": "a", "name": "Shop A", "url": "http://a.test", "color": "#000"}
    ],
    "agents": {"https://agent.test/profile.json": "http://agent.test"},
  }
  # The app's startup reads console.json; the fakes go in after it.
  with TestClient(console.app) as c:
    console.app.state.settings = settings
    console.app.state.shops = console.Shops(
      settings,
      httpx.AsyncClient(transport=fake_world(seen)),
      console.Reviews(tmp_path / "reviews.json"),
    )
    c.seen = seen
    yield c


def test_orders_are_summarised_for_the_table(client):
  rows = client.get("/api/a/orders").json()
  assert rows[0]["buyer"]["email"] == "ana@example.com"
  assert rows[0]["signature"] == "verified"
  assert rows[0]["items"][0]["codes"] == ["DSK-1"]
  assert rows[0]["payment"]["coins"] == 40
  # Every call to the shop carried the merchant secret.
  assert all(s[3] == "s3cret" for s in client.seen if s[1] == "a.test")


def test_a_fraud_check_flagged_for_review_reaches_the_table(client):
  risk = {
    "level": "elevated",
    "score": 62,
    "outcome": "authorized",
    "reason": "elevated_risk_level",
    "seller_message": "Payment complete.",
    "review": True,
  }
  flagged = {**ORDER, "payment": {**ORDER["payment"], "risk": risk}}

  summary = console.summarize(flagged, {"id": "a", "name": "A", "color": "red"})

  assert summary["payment"]["risk"] == risk
  # An order without a fraud check (the mock rail) simply has none.
  assert client.get("/api/a/orders").json()[0]["payment"]["risk"] is None


def test_an_order_comes_with_its_events_and_the_agent_side(client):
  detail = client.get("/api/a/orders/o1").json()
  assert [e["event"] for e in detail["events"]] == [
    "CHECKOUT_CREATED",
    "PAYMENT_CONFIRMED",
  ]
  assert detail["payment"]["card"]["last4"] == "4242"
  assert detail["payment"]["url"].endswith("/payments/pi_1")
  assert detail["summary"]["shop_name"] == "Shop A"
  assert detail["agent"]["evidence"]["findings"][0]["ok"] is True
  assert detail["agent"]["conversation"]["events"][0]["text"] == "a spa day"


def test_events_keep_only_this_shop(client):
  events = client.get("/api/a/events").json()
  assert [e["at"] for e in events] == ["t4", "t3", "t1"]


def test_stats_count_money_doors_and_declines(client):
  numbers = client.get("/api/a/stats").json()
  assert numbers["orders"] == 1
  assert (numbers["gross"], numbers["refunded"], numbers["net"]) == (
    4910,
    0,
    4910,
  )
  assert numbers["coins_spent"] == 40
  assert numbers["doors"] == {"agent": 1}
  assert numbers["signatures"] == {"verified": 1}
  assert numbers["rails"] == {"mock": 1}
  assert numbers["redemption"] == {"unredeemed": 1}
  assert numbers["declines"] == 1
  assert numbers["decline_codes"] == {"GENERIC_DECLINE": 1}
  assert (numbers["checkouts_opened"], numbers["checkouts_paid"]) == (1, 1)
  assert len(numbers["days"]) == 14


def test_orders_move_from_new_to_seen_to_handled(client):
  assert client.get("/api/a/orders").json()[0]["attention"] == "new"
  assert client.get("/api/a/stats").json()["attention"] == {
    "new": 1,
    "seen": 0,
    "handled": 0,
  }
  # Opening the order is seeing it.
  client.get("/api/a/orders/o1")
  assert client.get("/api/a/orders").json()[0]["attention"] == "seen"
  # Marking it by hand, and reopening it.
  marked = client.post("/api/a/orders/o1/review", json={"state": "handled"})
  assert marked.json()["attention"] == "handled"
  assert client.get("/api/a/orders").json()[0]["attention"] == "handled"
  client.post("/api/a/orders/o1/review", json={"state": "open"})
  assert client.get("/api/a/orders").json()[0]["attention"] == "seen"
  assert (
    client.post("/api/a/orders/o1/review", json={"state": "x"}).status_code
    == 422
  )
  # An action on the order handles it.
  client.post("/api/a/orders/o1/cancel")
  assert client.get("/api/a/orders").json()[0]["attention"] == "handled"


def test_an_order_already_acted_on_counts_as_handled(client):
  acted = {**ORDER, "payment": {**ORDER["payment"], "status": "refunded"}}
  assert console.attention(acted, None) == "handled"
  assert console.attention(ORDER, None) == "new"
  assert console.attention(ORDER, {"state": "seen"}) == "seen"


def test_all_shops_lists_every_shop_s_orders(client):
  rows = client.get("/api/all/orders").json()
  assert [r["shop"] for r in rows] == ["a"]
  assert client.get("/api/all/stats").json()["orders"] == 1


def test_actions_reach_the_shop_and_refusals_come_back(client, seen):
  # The one action cancels the service at the shop, then refunds it.
  done = client.post("/api/a/orders/o1/cancel")
  assert done.json() == {"refunded": "cancelled by the marketplace"}
  assert [call[2] for call in seen[-2:]] == [
    "/orders/o1/cancel",
    "/orders/o1/refund",
  ]
  # There is no other action: no bare refund, no redemption.
  assert client.post("/api/a/orders/o1/refund", json={}).status_code == 404
  assert client.post("/api/a/orders/o1/redeem", json={}).status_code == 404
  missing = client.get("/api/a/orders/missing")
  assert missing.status_code == 404
  assert missing.json()["detail"] == "Order not found"
  assert client.get("/api/zz/orders").status_code == 404


def test_the_page_and_the_shops_list(client):
  assert client.get("/").status_code == 200
  listed = client.get("/api/shops").json()
  assert listed["shops"][0]["name"] == "Shop A"
  assert listed["agents"] == ["https://agent.test/profile.json"]
