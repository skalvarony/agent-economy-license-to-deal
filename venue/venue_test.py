"""Tests of the venue simulator against a fake shop."""

import json

import app as venue
from fastapi.testclient import TestClient
import httpx
import pytest

DEALS = [
  {
    "id": "spa",
    "title": "Spa Day",
    "merchant": "Test Spa",
    "category": "wellness",
    "location": "Prague 7",
    "window": None,
    "refundability": "refundable",
    "options": [{"id": "spa-60", "title": "60 min", "price": 9900, "stock": 3}],
  }
]


def order(redemption="unredeemed", payment="captured"):
  return {
    "id": "o1",
    "placed_at": "2026-10-05T10:00:00+00:00",
    "channel": "agent",
    "signature": {"status": "verified"},
    "buyer": {"email": "ana@example.com", "full_name": "Ana"},
    "payment": {"amount": 4910, "coins": 40, "status": payment},
    "line_items": [
      {
        "item": {"id": "spa-60", "title": "Spa Day · 60 min"},
        "voucher": {"codes": ["DSK-1"], "status": "issued"},
        "redemption": {"status": redemption},
      }
    ],
  }


@pytest.fixture
def client(monkeypatch, tmp_path):
  calls = []
  state = {"order": order()}

  def answer(request: httpx.Request):
    path = request.url.path
    body = json.loads(request.content) if request.content else {}
    calls.append((request.method, path, body))
    if request.headers.get("simulation-secret") != "s3cret":
      return httpx.Response(403, json={"detail": "Invalid Simulation Secret"})
    if path == "/deals":
      return httpx.Response(200, json=DEALS)
    if path == "/orders":
      return httpx.Response(200, json=[state["order"]])
    if path == "/orders/o1/redeem":
      state["order"] = order(
        "redeemed" if body["honoured"] else "redemption_failed"
      )
      return httpx.Response(200, json=state["order"])
    if path == "/orders/o1/cancel":
      state["order"] = order("cancelled_by_merchant")
      return httpx.Response(200, json=state["order"])
    if path == "/orders/o1/refund":
      state["order"] = order(
        state["order"]["line_items"][0]["redemption"]["status"], "refunded"
      )
      return httpx.Response(200, json=state["order"])
    if path == "/wallets/grant":
      return httpx.Response(200, json={"balance": 45})
    return httpx.Response(404, json={"detail": f"no {path}"})

  monkeypatch.setattr(venue, "SECRET", "s3cret")
  monkeypatch.setattr(venue, "RUN_DIR", tmp_path)
  settings = {
    "name": "Venue",
    "shops": [
      {"id": "a", "name": "Shop A", "url": "http://a.test", "color": "#000"}
    ],
  }
  with TestClient(venue.app) as c:
    venue.app.state.settings = settings
    venue.app.state.shops = venue.Shops(
      settings, httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    c.calls = calls
    yield c


def test_the_world_groups_purchases_under_their_deals(client):
  world = client.get("/api/world").json()
  deal = world["shops"][0]["deals"][0]
  assert deal["merchant"] == "Test Spa"
  assert deal["purchases"][0]["codes"] == ["DSK-1"]
  assert deal["purchases"][0]["redemption"] == "unredeemed"


def test_a_customer_turned_away_is_refunded_with_goodwill(client):
  entry = client.post(
    "/api/a/orders/o1/happen", json={"what": "refused"}
  ).json()
  paths = [c[1] for c in client.calls if c[0] == "POST"]
  assert paths == ["/orders/o1/redeem", "/orders/o1/refund", "/wallets/grant"]
  assert [c[2] for c in client.calls if c[1] == "/orders/o1/redeem"][0] == {
    "honoured": False
  }
  assert any(
    "refunded the customer: $49.10" in c for c in entry["consequences"]
  )
  assert any("Goodwill: 5 coins" in c for c in entry["consequences"])
  # It is on the record, newest first.
  assert client.get("/api/log").json()[0]["what"] == "refused"


def test_an_honoured_visit_only_redeems(client):
  entry = client.post(
    "/api/a/orders/o1/happen", json={"what": "arrived"}
  ).json()
  assert [c[1] for c in client.calls if c[0] == "POST"] == ["/orders/o1/redeem"]
  assert "The sale is final" in entry["consequences"][0]


def test_a_no_show_changes_nothing_at_the_shop(client):
  entry = client.post(
    "/api/a/orders/o1/happen", json={"what": "no_show"}
  ).json()
  assert not [c for c in client.calls if c[0] == "POST"]
  assert "no refund" in entry["consequences"][0]


def test_unknown_happenings_and_orders_are_refused(client):
  assert (
    client.post("/api/a/orders/o1/happen", json={"what": "party"}).status_code
    == 400
  )
  assert (
    client.post("/api/a/orders/zz/happen", json={"what": "arrived"}).status_code
    == 404
  )
