"""Tests for what the shop adds to the sample: vouchers, catalog and rail."""

import asyncio
from collections.abc import AsyncGenerator
import copy
import datetime
import os
from pathlib import Path
import shutil
import tempfile
from unittest import mock
from urllib.parse import parse_qs
import uuid

from absl import flags
from absl.testing import absltest
import config
import db
import dependencies
from fastapi.testclient import TestClient
import httpx
from server.server import app
from services import booking_service
from services import payment_rail
from services import visitor
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

FLAGS = flags.FLAGS
SECRET = {"Simulation-Secret": "test-secret"}
# A password for the accounts the tests create; nothing real uses it.
PASSWORD = "correct horse battery"
PAYMENT = {
  "payment": {
    "instruments": [
      {
        "id": "instr_1",
        "handler_id": "mock_payment_handler",
        "type": "card",
        "credential": {"type": "token", "token": "success_token"},
      }
    ]
  },
  "risk_signals": {},
}


def _deal(
  deal_id: str, title: str, category: str, refundability: str
) -> db.Deal:
  return db.Deal(
    id=deal_id,
    title=title,
    option_label="Duration",
    merchant="Test Spa",
    category=category,
    description="A relaxing test",
    location="Prague 7",
    service_not_before="2026-10-10T10:00:00+02:00",
    service_not_after="2026-10-10T20:00:00+02:00",
    refundability=refundability,
    refundable_until="2026-10-09T10:00:00+02:00",
    voucher_expires_at="2026-12-31T23:59:00+01:00",
    redemption_method="code_at_venue",
    redemption_instructions="Show the code",
    highlights=["Quiet rooms", "Towels included"],
    about_merchant="A small test spa by the river.",
    rating=4.7,
    reviews=1284,
    bought=5200,
  )


class ShopTestCase(absltest.TestCase):
  """A shop with two deals and one shipped product, in temporary databases."""

  def setUp(self) -> None:
    """Create temporary databases with two deals and one shipped product."""
    flags.FLAGS(["test"])
    super().setUp()
    self.test_dir = Path(tempfile.mkdtemp())
    FLAGS.simulation_secret = SECRET["Simulation-Secret"]
    FLAGS.ledger_path = str(self.test_dir / "ledger.jsonl")
    # A shop folder with a name and one deal photo.
    (self.test_dir / "images").mkdir()
    (self.test_dir / "images" / "spa.jpg").write_bytes(b"jpeg")
    (self.test_dir / "images" / "spa_2.jpg").write_bytes(b"jpeg")
    (self.test_dir / "shop.json").write_text(
      '{"name": "Test Deals", "promo_code": "TEST10", "font": "fraunces"}'
    )
    FLAGS.shop_dir = str(self.test_dir)
    config._SHOP_CACHE = None

    self.products_engine = create_async_engine(
      f"sqlite+aiosqlite:///{self.test_dir / 'products.db'}"
    )
    self.products_session_factory = sessionmaker(
      self.products_engine, expire_on_commit=False, class_=AsyncSession
    )
    self.transactions_engine = create_async_engine(
      f"sqlite+aiosqlite:///{self.test_dir / 'transactions.db'}"
    )
    self.transactions_session_factory = sessionmaker(
      self.transactions_engine, expire_on_commit=False, class_=AsyncSession
    )

    async def override_get_products_db() -> AsyncGenerator[AsyncSession, None]:
      async with self.products_session_factory() as session:
        yield session

    async def override_get_transactions_db() -> AsyncGenerator[
      AsyncSession, None
    ]:
      async with self.transactions_session_factory() as session:
        yield session

    app.dependency_overrides[dependencies.get_products_db] = (
      override_get_products_db
    )
    app.dependency_overrides[dependencies.get_transactions_db] = (
      override_get_transactions_db
    )
    self.client = TestClient(app)
    asyncio.run(self._seed())

  def tearDown(self) -> None:
    """Clean up the test environment."""
    app.dependency_overrides.clear()
    FLAGS.ledger_path = None
    FLAGS.shop_dir = None
    config._SHOP_CACHE = None

    async def dispose_engines() -> None:
      await self.products_engine.dispose()
      await self.transactions_engine.dispose()

    asyncio.run(dispose_engines())
    shutil.rmtree(self.test_dir)
    super().tearDown()

  async def _seed(self) -> None:
    async with self.products_engine.begin() as conn:
      await conn.run_sync(db.ProductBase.metadata.create_all)
    async with self.transactions_engine.begin() as conn:
      await conn.run_sync(db.TransactionBase.metadata.create_all)

    async with self.products_session_factory() as session:
      session.add_all(
        [
          _deal("spa", "Spa Day for Two", "wellness", "refundable"),
          _deal("cruise", "Dinner Cruise", "food", "non_refundable"),
          # Each option is a product with its own price.
          db.Product(id="spa-60", title="Spa Day for Two · 60 min", price=9900),
          db.Product(
            id="spa-90", title="Spa Day for Two · 90 min", price=13900
          ),
          db.Product(id="cruise-std", title="Dinner Cruise · Seat", price=8900),
          db.DealOption(
            product_id="spa-60",
            deal_id="spa",
            title="60 min",
            list_price=18000,
            position=0,
          ),
          db.DealOption(
            product_id="spa-90",
            deal_id="spa",
            title="90 min",
            list_price=20000,
            position=1,
          ),
          db.DealOption(
            product_id="cruise-std", deal_id="cruise", title="Seat", position=0
          ),
          db.Product(id="rose", title="Red Rose", price=1000),
          db.DealReview(
            deal_id="spa",
            author="Marta K.",
            rating=5,
            date="2026-09-21",
            text="Just right.",
            reply="Thank you, Marta.",
          ),
        ]
      )
      await session.commit()
    async with self.transactions_session_factory() as session:
      session.add_all(
        [
          # An option's codes are its inventory: two, five and none.
          db.VoucherCode(code="SPA60-A", product_id="spa-60"),
          db.VoucherCode(code="SPA60-B", product_id="spa-60"),
          *(
            db.VoucherCode(code=f"SPA90-{n}", product_id="spa-90")
            for n in range(5)
          ),
          db.Inventory(product_id="rose", quantity=5),
          db.Discount(
            code="TEST10", type="percentage", value=10, description="10% off"
          ),
        ]
      )
      await session.commit()

  def _signup(
    self, email: str = "ada@example.com", full_name: str = "Ada Buyer"
  ):
    """Create an account as the shop and sign the client in as it."""
    created = self.client.post(
      "/accounts",
      headers=SECRET,
      json={"full_name": full_name, "email": email, "password": PASSWORD},
    )
    self.assertEqual(created.status_code, 200, created.text)
    response = self.client.post(
      "/login",
      data={"email": email, "password": PASSWORD},
      follow_redirects=False,
    )
    self.assertEqual(response.status_code, 303, response.text)
    return response

  def _headers(self) -> dict[str, str]:
    return {
      "UCP-Agent": 'profile="https://agent.example/profile"',
      "idempotency-key": str(uuid.uuid4()),
      "request-id": str(uuid.uuid4()),
    }

  def _create(self, product_id: str = "spa-60", quantity: int = 1) -> dict:
    response = self.client.post(
      "/checkout-sessions",
      headers=self._headers(),
      json={"line_items": [{"item": {"id": product_id}, "quantity": quantity}]},
    )
    self.assertEqual(response.status_code, 201, response.text)
    return response.json()

  def _complete(self, checkout_id: str, **extra):
    return self.client.post(
      f"/checkout-sessions/{checkout_id}/complete",
      headers=self._headers(),
      json={**PAYMENT, **extra},
    )

  def _buy(self) -> dict:
    """Buy the spa deal and return the order."""
    response = self._complete(self._create()["id"])
    self.assertEqual(response.status_code, 200, response.text)
    return self._order(response.json()["order"]["id"])

  def _order(self, order_id: str) -> dict:
    """Read an order as the shop, which may read any order."""
    response = self.client.get(
      f"/orders/{order_id}", headers={**self._headers(), **SECRET}
    )
    self.assertEqual(response.status_code, 200, response.text)
    return response.json()

  def _stock(self, product_id: str) -> int:
    async def read() -> int:
      async with self.transactions_session_factory() as session:
        return await db.get_inventory(session, product_id)

    return asyncio.run(read())

  def _events(self) -> list[str]:
    return [
      e["event"] for e in self.client.get("/ledger", headers=SECRET).json()
    ]


class VoucherTest(ShopTestCase):
  """The shop sells vouchers end to end over its API."""

  def test_checkout_line_items_carry_voucher_terms(self) -> None:
    """The buyer sees the service date and refund terms before paying."""
    line = self._create()["line_items"][0]

    self.assertEqual(line["service"]["category"], "wellness")
    self.assertEqual(
      line["service"]["window"]["not_before"], "2026-10-10T10:00:00+02:00"
    )
    self.assertEqual(line["cancellation"]["refundability"], "refundable")
    self.assertEqual(line["redemption"]["method"], "code_at_venue")
    self.assertEqual(line["service"]["deal_id"], "spa")
    self.assertEqual(line["service"]["option"], "60 min")
    self.assertEqual(line["item"]["title"], "Spa Day for Two · 60 min")
    # Codes are only handed out once the order is paid.
    self.assertNotIn("codes", line["voucher"])

  def test_buying_a_deal_issues_a_voucher_without_shipping(self) -> None:
    """A voucher checkout completes with no fulfillment and takes a place."""
    order = self._buy()
    line = order["line_items"][0]

    self.assertEqual(line["voucher"]["status"], "issued")
    self.assertEqual(line["voucher"]["codes"], ["SPA60-A"])
    self.assertEqual(line["redemption"]["status"], "unredeemed")
    self.assertEqual(order["payment"]["status"], "captured")
    self.assertEqual(order["payment"]["rail"], "mock")
    self.assertEqual(order["payment"]["amount"], 9900)
    self.assertEqual(self._stock("spa-60"), 1)
    self.assertEqual(
      self._events(),
      ["CHECKOUT_CREATED", "PAYMENT_CONFIRMED", "VOUCHER_ISSUED"],
    )

  def test_each_unit_takes_a_code_from_its_option_pool(self) -> None:
    """Two units get two codes; an empty pool means the option is sold out."""
    checkout = self._create(quantity=2)
    order_id = self._complete(checkout["id"]).json()["order"]["id"]

    line = self._order(order_id)["line_items"][0]
    self.assertEqual(line["voucher"]["codes"], ["SPA60-A", "SPA60-B"])
    self.assertEqual(self._stock("spa-60"), 0)
    # The deal's other option has its own pool and is untouched.
    self.assertEqual(self._stock("spa-90"), 5)

    sold_out = self.client.post(
      "/checkout-sessions",
      headers=self._headers(),
      json={"line_items": [{"item": {"id": "spa-60"}, "quantity": 1}]},
    )
    self.assertEqual(sold_out.json()["messages"][0]["code"], "OUT_OF_STOCK")
    self.assertEqual(self._create("spa-90")["status"], "ready_for_complete")

  def test_an_order_is_read_by_its_agent_or_the_shop_only(self) -> None:
    """Another agent can't read an order; its own agent and the shop can."""
    order_id = self._buy()["id"]
    mine = self.client.get(f"/orders/{order_id}", headers=self._headers())
    self.assertEqual(mine.status_code, 200)

    other = self.client.get(
      f"/orders/{order_id}",
      headers={
        **self._headers(),
        "UCP-Agent": 'profile="https://other.example/profile"',
      },
    )
    self.assertEqual(other.status_code, 403)

    shop = self.client.get(
      f"/orders/{order_id}",
      headers={**self._headers(), **SECRET},
    )
    self.assertEqual(shop.status_code, 200)
    # The ledger is the shop's too.
    self.assertEqual(self.client.get("/ledger").status_code, 403)

  def test_the_catalog_gives_an_agent_what_the_page_shows(self) -> None:
    """Highlights, the merchant's blurb and top reviews come as data."""
    found = self.client.post(
      "/catalog/search", headers=self._headers(), json={"query": "spa"}
    ).json()
    spa = next(p for p in found["products"] if p["id"] == "spa")
    # Every photo the shop's page shows, the main one first.
    self.assertEqual(
      [m["url"].split("/images/")[1] for m in spa["media"]],
      ["spa.jpg", "spa_2.jpg"],
    )
    self.assertEqual(spa["highlights"], ["Quiet rooms", "Towels included"])
    self.assertEqual(
      spa["seller"]["description"], "A small test spa by the river."
    )
    self.assertEqual(
      spa["reviews"],
      [
        {
          "author": "Marta K.",
          "rating": 5,
          "date": "2026-09-21",
          "text": "Just right.",
          "reply": "Thank you, Marta.",
        }
      ],
    )

  def test_the_shop_lists_its_deals_for_its_tools(self) -> None:
    """Deals come with merchant, options, prices and stock."""
    deals = self.client.get("/deals", headers=SECRET).json()
    spa = next(d for d in deals if d["id"] == "spa")
    self.assertEqual(spa["merchant"], "Test Spa")
    self.assertEqual(spa["options"][0]["id"], "spa-60")
    self.assertEqual(spa["options"][0]["stock"], 2)
    self.assertEqual(self.client.get("/deals").status_code, 403)

  def test_the_shop_lists_every_order_newest_first(self) -> None:
    """The console reads all orders with the merchant secret."""
    first = self._buy()["id"]
    second = self._buy()["id"]
    refused = self.client.get("/orders", headers=self._headers())
    self.assertEqual(refused.status_code, 403)
    listed = self.client.get("/orders", headers=SECRET).json()
    self.assertEqual([o["id"] for o in listed][:2], [second, first])
    self.assertEqual(listed[0]["channel"], "agent")

  def test_an_order_keeps_when_and_how_signed_it_was_placed(self) -> None:
    """An agent order records the signature check's outcome and its time."""
    order = self._buy()
    # The tests don't sign, so the shop records that.
    self.assertEqual(order["signature"], {"status": "missing"})
    self.assertEqual(order["channel"], "agent")
    self.assertIn("T", order["placed_at"])

  def test_two_buyers_cannot_get_the_last_code(self) -> None:
    """Whoever pays second for the last unit is refused and not charged."""
    self._complete(self._create()["id"])
    first = self._create()
    second = self._create()

    self.assertEqual(self._complete(first["id"]).status_code, 200)
    refused = self._complete(second["id"])

    self.assertEqual(refused.status_code, 409)
    self.assertEqual(refused.json()["messages"][0]["code"], "OUT_OF_STOCK")
    self.assertEqual(self._events().count("PAYMENT_CONFIRMED"), 2)

  def test_inventory_shows_each_option_pool_and_takes_new_codes(self) -> None:
    """The shop sees its codes by status and can add more to a pool."""
    order_id = self._buy()["id"]
    self.client.post(f"/orders/{order_id}/redeem", headers=SECRET)

    rows = self.client.get("/inventory", headers=SECRET).json()
    pools = {row["option_id"]: row["codes"] for row in rows}
    self.assertEqual(pools["spa-60"]["total"], 2)
    self.assertEqual(pools["spa-60"]["available"], 1)
    self.assertEqual(pools["spa-60"]["redeemed"], 1)
    self.assertEqual(pools["spa-90"]["available"], 5)
    self.assertEqual(pools["cruise-std"]["total"], 0)
    self.assertEqual(rows[0]["deal"], "Dinner Cruise")

    codes = self.client.get("/inventory/spa-60/codes", headers=SECRET).json()
    self.assertEqual(
      codes[0], {"code": "SPA60-A", "status": "redeemed", "order_id": order_id}
    )

    # New codes put a sold-out option back on sale; known codes are skipped.
    added = self.client.post(
      "/inventory/cruise-std/codes",
      headers=SECRET,
      json={"codes": ["CRUISE-1", "CRUISE-2", "SPA60-A"]},
    )
    self.assertEqual(added.json(), {"added": 2, "skipped": 1})
    self.assertEqual(self._stock("cruise-std"), 2)

    self.assertEqual(self.client.get("/inventory").status_code, 403)
    unknown = self.client.get("/inventory/nope/codes", headers=SECRET)
    self.assertEqual(unknown.status_code, 404)

  def test_shipped_products_still_need_fulfillment(self) -> None:
    """The sample's rule for goods is untouched."""
    response = self._complete(self._create("rose")["id"])

    self.assertEqual(response.status_code, 400)
    self.assertIn("Fulfillment", response.json()["messages"][0]["content"])

  def test_fee_added_after_checkout_stops_the_purchase(self) -> None:
    """A total that moved since the buyer saw it is not charged."""
    checkout = self._create()
    self.client.post(
      "/testing/booking-fee", headers=SECRET, json={"amount": 2500}
    )

    response = self._complete(checkout["id"])

    self.assertEqual(response.status_code, 409)
    message = response.json()["messages"][0]
    self.assertEqual(message["code"], "requires_consent")
    self.assertEqual(message["severity"], "requires_buyer_review")
    self.assertEqual(self._stock("spa-60"), 2)
    self.assertEqual(self._events(), ["CHECKOUT_CREATED", "CHECKOUT_CHANGED"])
    # The checkout the agent fetches next shows the fee and the new total.
    totals = {
      t["type"]: t["amount"]
      for t in self.client.get(
        f"/checkout-sessions/{checkout['id']}", headers=self._headers()
      ).json()["totals"]
    }
    self.assertEqual(totals, {"subtotal": 9900, "fee": 2500, "total": 12400})

  def test_merchant_cancel_then_refund(self) -> None:
    """A cancelled service is recorded and refunded once; its code is void."""
    order_id = self._buy()["id"]

    cancelled = self.client.post(f"/orders/{order_id}/cancel", headers=SECRET)
    self.assertEqual(cancelled.status_code, 200, cancelled.text)
    self.assertEqual(
      cancelled.json()["line_items"][0]["redemption"]["status"],
      "cancelled_by_merchant",
    )

    refunded = self.client.post(
      f"/orders/{order_id}/refund", headers=SECRET, json={"reason": "D1"}
    )
    self.assertEqual(refunded.status_code, 200, refunded.text)
    order = self._order(order_id)
    self.assertEqual(order["payment"]["status"], "refunded")
    self.assertEqual(order["line_items"][0]["voucher"]["status"], "refunded")
    # The code was handed out, so it is voided, not sold again.
    codes = self.client.get("/inventory/spa-60/codes", headers=SECRET).json()
    self.assertEqual([code["status"] for code in codes], ["void", "available"])
    self.assertEqual(self._stock("spa-60"), 1)
    self.assertEqual(
      self._events()[-2:], ["MERCHANT_CANCELLED", "REFUND_AUTHORISED"]
    )

    again = self.client.post(f"/orders/{order_id}/refund", headers=SECRET)
    self.assertEqual(again.status_code, 409)

  def test_redeemed_voucher_cannot_be_cancelled(self) -> None:
    """Once the service is delivered the merchant can't cancel it."""
    order_id = self._buy()["id"]

    redeemed = self.client.post(f"/orders/{order_id}/redeem", headers=SECRET)
    self.assertEqual(redeemed.status_code, 200, redeemed.text)
    line = redeemed.json()["line_items"][0]
    self.assertEqual(line["redemption"]["status"], "redeemed")
    self.assertEqual(line["voucher"]["status"], "redeemed")

    cancelled = self.client.post(f"/orders/{order_id}/cancel", headers=SECRET)
    self.assertEqual(cancelled.status_code, 409)
    self.assertEqual(
      cancelled.json()["messages"][0]["code"], "VOUCHER_STATE_CONFLICT"
    )

  def test_failed_redemption_is_recorded(self) -> None:
    """The venue couldn't honour the voucher; it can still be tried again."""
    order_id = self._buy()["id"]

    failed = self.client.post(
      f"/orders/{order_id}/redeem", headers=SECRET, json={"honoured": False}
    )

    line = failed.json()["line_items"][0]
    self.assertEqual(line["redemption"]["status"], "redemption_failed")
    self.assertEqual(line["voucher"]["status"], "issued")
    self.assertEqual(self._events()[-1], "REDEMPTION_FAILED")

  def test_merchant_actions_need_the_secret(self) -> None:
    """The buyer's agent can't cancel, redeem or refund."""
    order_id = self._buy()["id"]

    for action in ("cancel", "redeem", "refund"):
      response = self.client.post(f"/orders/{order_id}/{action}")
      self.assertEqual(response.status_code, 403, action)

  def test_catalog_search_filters_and_reports_availability(self) -> None:
    """An agent finds deals by text, category and price."""

    def search(body: dict) -> list[dict]:
      response = self.client.post(
        "/catalog/search", headers=self._headers(), json=body
      )
      self.assertEqual(response.status_code, 200, response.text)
      return response.json()["products"]

    self.assertLen(search({}), 3)
    self.assertEqual([p["id"] for p in search({"query": "day"})], ["spa"])
    # Both mention "spa" (the cruise through its merchant); the better match
    # comes first.
    self.assertEqual(
      [p["id"] for p in search({"query": "spa day"})], ["spa", "cruise"]
    )
    self.assertEqual(
      [p["id"] for p in search({"filters": {"categories": ["Food"]}})],
      ["cruise"],
    )
    self.assertEqual(
      [p["id"] for p in search({"filters": {"price": {"max": 5000}}})],
      ["rose"],
    )
    # A deal matches when any of its options is within the price.
    self.assertEqual(
      [p["id"] for p in search({"filters": {"price": {"max": 10000}}})],
      ["cruise", "rose", "spa"],
    )
    self.assertEqual(
      [p["id"] for p in search({"filters": {"price": {"min": 12000}}})],
      ["spa"],
    )

    cruise = search({"query": "cruise"})[0]
    self.assertFalse(cruise["variants"][0]["availability"]["available"])
    self.assertEqual(cruise["cancellation"]["refundability"], "non_refundable")
    self.assertEqual(cruise["variants"][0]["seller"]["name"], "Test Spa")

  def test_catalog_deal_lists_its_options_as_variants(self) -> None:
    """An agent sees each option's own price and availability."""
    spa = self.client.post(
      "/catalog/search", headers=self._headers(), json={"query": "day"}
    ).json()["products"][0]

    self.assertEqual(spa["title"], "Spa Day for Two")
    self.assertEqual(
      spa["options"],
      [
        {
          "name": "Duration",
          "values": [{"label": "60 min"}, {"label": "90 min"}],
        }
      ],
    )
    self.assertEqual(spa["price_range"]["min"]["amount"], 9900)
    self.assertEqual(spa["price_range"]["max"]["amount"], 13900)
    variants = {v["id"]: v for v in spa["variants"]}
    self.assertEqual(list(variants), ["spa-60", "spa-90"])
    self.assertEqual(variants["spa-90"]["price"]["amount"], 13900)
    self.assertEqual(
      variants["spa-90"]["options"], [{"name": "Duration", "label": "90 min"}]
    )
    self.assertTrue(variants["spa-90"]["availability"]["available"])
    self.assertEqual(spa["service"]["deal_id"], "spa")

  def test_catalog_search_paginates(self) -> None:
    """The cursor continues where the last page ended."""
    first = self.client.post(
      "/catalog/search",
      headers=self._headers(),
      json={"pagination": {"limit": 2}},
    ).json()
    self.assertLen(first["products"], 2)
    self.assertTrue(first["pagination"]["has_next_page"])
    self.assertEqual(first["pagination"]["total_count"], 3)

    second = self.client.post(
      "/catalog/search",
      headers=self._headers(),
      json={
        "pagination": {"limit": 2, "cursor": first["pagination"]["cursor"]}
      },
    ).json()
    self.assertLen(second["products"], 1)
    self.assertFalse(second["pagination"]["has_next_page"])

  def test_catalog_lookup_returns_known_ids(self) -> None:
    """An option ID finds that option; a deal ID finds all its options."""

    def lookup(ids: list[str]) -> list[dict]:
      response = self.client.post(
        "/catalog/lookup", headers=self._headers(), json={"ids": ids}
      )
      self.assertEqual(response.status_code, 200, response.text)
      return response.json()["products"]

    by_option = lookup(["spa-90", "nope"])
    self.assertEqual([p["id"] for p in by_option], ["spa"])
    self.assertEqual(
      [(v["id"], v["inputs"]) for v in by_option[0]["variants"]],
      [("spa-90", [{"id": "spa-90", "match": "exact"}])],
    )

    by_deal = lookup(["spa"])
    self.assertEqual(
      [(v["id"], v["inputs"][0]["match"]) for v in by_deal[0]["variants"]],
      [("spa-60", "featured"), ("spa-90", "featured")],
    )

  def test_profile_declares_catalog_and_voucher_capabilities(self) -> None:
    """Agents discover the catalog and the voucher extension."""
    capabilities = self.client.get("/.well-known/ucp").json()["ucp"][
      "capabilities"
    ]

    self.assertIn("dev.ucp.shopping.catalog.search", capabilities)
    self.assertIn("dev.ucp.shopping.catalog.lookup", capabilities)
    schema_url = capabilities["com.example.service_voucher"][0]["schema"]
    schema = self.client.get(schema_url).json()
    self.assertEqual(schema["title"], "Service voucher")

  def test_catalog_products_carry_price_rating_and_photo(self) -> None:
    """An agent can compare the saving, the rating and see the photo."""
    spa = self.client.post(
      "/catalog/lookup", headers=self._headers(), json={"ids": ["spa"]}
    ).json()["products"][0]

    self.assertEqual(spa["list_price_range"]["min"]["amount"], 18000)
    self.assertEqual(spa["list_price_range"]["max"]["amount"], 20000)
    self.assertEqual(spa["variants"][0]["list_price"]["amount"], 18000)
    self.assertEqual(
      spa["rating"],
      {"value": 4.7, "scale_min": 1, "scale_max": 5, "count": 1284},
    )
    photo = self.client.get(spa["media"][0]["url"])
    self.assertEqual(photo.status_code, 200)
    self.assertEqual(photo.headers["content-type"], "image/jpeg")

  def test_storefront_lists_the_deals(self) -> None:
    """A person can browse the catalog in a browser."""
    response = self.client.get("/")

    self.assertEqual(response.status_code, 200)
    self.assertIn("Test Deals", response.text)
    self.assertIn("Spa Day for Two", response.text)
    self.assertIn('href="/deals/spa"', response.text)
    self.assertIn("-45%", response.text)
    self.assertIn(
      "<small>From</small> <s>$180</s> <strong>$99</strong>", response.text
    )
    self.assertIn("Choose an option", response.text)
    # Seven codes are left across the options: plenty, so no count is shown.
    self.assertNotIn("left</span>", response.text)
    self.assertIn("Sold out", response.text)
    self.assertIn("Free cancellation until Fri 9 Oct", response.text)
    self.assertIn("Final sale, non-refundable", response.text)

  def test_deal_page_shows_fine_print_and_agent_request(self) -> None:
    """A deal page says what you get and how to buy it through an agent."""
    response = self.client.get("/deals/spa")

    self.assertEqual(response.status_code, 200)
    self.assertIn("Sat 10 Oct, 10:00 to 20:00", response.text)
    self.assertIn("Cancel by Fri 9 Oct, 10:00", response.text)
    self.assertIn("Show the code", response.text)
    # Each option shows its own price; only the scarce one says how many.
    self.assertIn('value="spa-60"', response.text)
    self.assertIn('value="spa-90"', response.text)
    self.assertIn("<s>$200</s> <strong>$139</strong>", response.text)
    self.assertIn("Only 2 left", response.text)
    self.assertNotIn("Only 5 left", response.text)
    # The cheapest option on sale is the one picked to start with.
    self.assertIn(
      "Buy &quot;Spa Day for Two · 60 min&quot; from Test Deals", response.text
    )
    self.assertIn("item spa-60.", response.text)

  def test_deal_page_has_gallery_highlights_merchant_and_reviews(self) -> None:
    """A deal page gives enough to decide: photos, points, who, and reviews."""
    page = self.client.get("/deals/spa").text

    self.assertIn('data-photo="/images/spa.jpg" aria-current=true', page)
    self.assertIn('data-photo="/images/spa_2.jpg"', page)
    self.assertIn("view-transition-name: photo-spa", page)
    self.assertIn("<li>Quiet rooms</li>", page)
    self.assertIn("About Test Spa", page)
    self.assertIn("A small test spa by the river.", page)
    self.assertIn("1,284 ratings", page)
    self.assertIn("Marta K.", page)
    self.assertIn("21 Sep 2026", page)
    self.assertIn("Test Spa replied", page)
    # Other deals that can be bought are offered below; sold-out ones aren't.
    more = page.split("More from Test Deals")[1]
    self.assertIn("Red Rose", more)
    self.assertNotIn("Dinner Cruise", more)

  def test_pages_load_the_shops_font_and_the_animation_library(self) -> None:
    """Each shop names its display font; assets come from the shop itself."""
    home = self.client.get("/").text
    self.assertIn('--display: "Fraunces", Georgia, serif;', home)
    self.assertIn("url(/assets/fonts/fraunces.woff2)", home)
    self.assertIn('src="/assets/vendor/motion.js"', home)

    font = self.client.get("/assets/fonts/fraunces.woff2")
    self.assertEqual(font.headers["content-type"], "font/woff2")
    library = self.client.get("/assets/vendor/motion.js")
    self.assertIn("javascript", library.headers["content-type"])
    self.assertIn("max-age", library.headers["cache-control"])
    self.assertEqual(
      self.client.get("/assets/fonts/nope.woff2").status_code, 404
    )
    self.assertEqual(self.client.get("/assets/../server.py").status_code, 404)

  def test_missing_deal_gets_the_shops_own_not_found_page(self) -> None:
    """A wrong link lands on a page that leads back to the deals."""
    response = self.client.get("/deals/nope")

    self.assertEqual(response.status_code, 404)
    self.assertIn("We can't find that deal", response.text)
    self.assertIn('href="/">Browse deals</a>', response.text)

  def test_sold_out_deal_page_offers_no_request(self) -> None:
    """Nothing to hand to an agent when there are no places left."""
    response = self.client.get("/deals/cruise")

    self.assertEqual(response.status_code, 200)
    self.assertNotIn('id="ask"', response.text)

  def test_unknown_deal_and_image_are_not_found(self) -> None:
    """Missing pages and photos answer 404, including path tricks."""
    self.assertEqual(self.client.get("/deals/nope").status_code, 404)
    self.assertEqual(self.client.get("/images/nope.jpg").status_code, 404)
    self.assertEqual(self.client.get("/images/..%2Fshop.json").status_code, 404)


class DealRulesTest(ShopTestCase):
  """An open-dated deal with a limit per person, and promo codes."""

  BUYER = {"email": "ada@example.com", "full_name": "Ada Buyer"}

  async def _seed(self) -> None:
    await super()._seed()
    float_deal = _deal("float", "Float Session", "wellness", "refundable")
    # Open-dated: booked after buying, deadlines counted from the purchase.
    float_deal.service_not_before = float_deal.service_not_after = None
    float_deal.refundable_until = float_deal.voucher_expires_at = None
    float_deal.voucher_valid_days = 180
    float_deal.refund_days = 14
    float_deal.appointment_required = True
    float_deal.booking_contact = "+420 555 0100"
    float_deal.limit_per_person = 1
    float_deal.repurchase_days = 90
    async with self.products_session_factory() as session:
      session.add_all(
        [
          float_deal,
          db.Product(id="float-60", title="Float Session · 60 min", price=5900),
          db.DealOption(
            product_id="float-60",
            deal_id="float",
            title="60 min",
            description="An hour afloat.",
            includes=["Private room", "Earplugs"],
            list_price=8500,
            position=0,
          ),
        ]
      )
      await session.commit()
    async with self.transactions_session_factory() as session:
      session.add_all(
        db.VoucherCode(code=f"FLOAT-{n}", product_id="float-60")
        for n in range(3)
      )
      await session.commit()

  def _open(self, quantity: int = 1, buyer: dict | None = None, **extra):
    body = {
      "line_items": [{"item": {"id": "float-60"}, "quantity": quantity}],
      **extra,
    }
    if buyer:
      body["buyer"] = buyer
    return self.client.post(
      "/checkout-sessions", headers=self._headers(), json=body
    )

  def _buy_float(self, buyer: dict | None = None):
    return self._complete(self._open(buyer=buyer or self.BUYER).json()["id"])

  def test_open_dated_terms_count_days_from_the_purchase(self) -> None:
    """Before buying there are no dates, only how long things last."""
    line = self._open().json()["line_items"][0]

    self.assertNotIn("window", line["service"])
    self.assertEqual(line["service"]["includes"], ["Private room", "Earplugs"])
    self.assertEqual(
      line["voucher"], {"valid_days": 180, "amount_paid_expires": False}
    )
    self.assertEqual(
      line["cancellation"], {"refundability": "refundable", "refund_days": 14}
    )
    self.assertEqual(
      line["purchase"], {"limit_per_person": 1, "repurchase_days": 90}
    )
    self.assertTrue(line["redemption"]["appointment_required"])
    # How to book is only told to someone who bought.
    self.assertNotIn("booking_contact", line["redemption"])

  def test_order_turns_the_days_into_dates_and_reveals_the_contact(
    self,
  ) -> None:
    """The voucher says exactly when it expires and how to book."""
    order_id = self._buy_float().json()["order"]["id"]
    line = self._order(order_id)["line_items"][0]

    today = datetime.datetime.now().astimezone()
    expires = datetime.datetime.fromisoformat(line["voucher"]["expires_at"])
    refundable = datetime.datetime.fromisoformat(
      line["cancellation"]["refundable_until"]
    )
    self.assertEqual((expires - today).days, 179)
    self.assertEqual((refundable - today).days, 13)
    self.assertEqual(line["redemption"]["booking_contact"], "+420 555 0100")
    self.assertEqual(line["voucher"]["codes"], ["FLOAT-0"])

  def test_one_checkout_cannot_ask_for_more_than_the_limit(self) -> None:
    """Two units of a one-per-person deal are refused straight away."""
    response = self._open(quantity=2)

    self.assertEqual(response.status_code, 409)
    message = response.json()["messages"][0]
    self.assertEqual(message["code"], "PURCHASE_LIMIT_REACHED")
    self.assertIn("Limit 1 per person for Float Session", message["content"])

  def test_same_buyer_cannot_buy_again_before_the_repurchase_period(
    self,
  ) -> None:
    """The limit follows the buyer's email across orders; others can buy."""
    self.assertEqual(self._buy_float().status_code, 200)

    again = self._buy_float({"email": "ADA@example.com", "full_name": "Ada"})
    self.assertEqual(again.status_code, 409)
    message = again.json()["messages"][0]
    self.assertEqual(message["code"], "PURCHASE_LIMIT_REACHED")
    self.assertIn("It can be bought again from", message["content"])
    # Nothing was charged and no code left the pool for the refused order.
    self.assertEqual(self._stock("float-60"), 2)
    self.assertEqual(self._events().count("PAYMENT_CONFIRMED"), 1)

    other = self._buy_float({"email": "bob@example.com", "full_name": "Bob"})
    self.assertEqual(other.status_code, 200)

  def test_limited_deal_needs_the_buyer_email_to_pay(self) -> None:
    """Without knowing who buys, the limit can't be kept."""
    response = self._complete(self._open().json()["id"])

    self.assertEqual(response.status_code, 400)
    self.assertIn("email is needed", response.json()["messages"][0]["content"])

  def test_refunded_purchase_no_longer_counts_towards_the_limit(self) -> None:
    """After a refund the buyer may buy the deal again."""
    order_id = self._buy_float().json()["order"]["id"]
    self.client.post(f"/orders/{order_id}/refund", headers=SECRET)

    self.assertEqual(self._buy_float().status_code, 200)

  def test_promo_code_lowers_the_total_for_an_agent(self) -> None:
    """An agent sends the code with UCP's discount extension."""
    checkout = self._open(buyer=self.BUYER, discounts={"codes": ["test10"]})
    totals = {t["type"]: t["amount"] for t in checkout.json()["totals"]}

    self.assertEqual(
      totals, {"subtotal": 5900, "discount": -590, "total": 5310}
    )
    order_id = self._complete(checkout.json()["id"]).json()["order"]["id"]
    self.assertEqual(self._order(order_id)["payment"]["amount"], 5310)

  def test_catalog_tells_agents_the_rules_the_option_and_the_promo(
    self,
  ) -> None:
    """Everything a person reads on the deal page, as fields."""
    product = self.client.post(
      "/catalog/lookup", headers=self._headers(), json={"ids": ["float"]}
    ).json()["products"][0]

    self.assertEqual(
      product["purchase"], {"limit_per_person": 1, "repurchase_days": 90}
    )
    self.assertEqual(product["voucher"]["valid_days"], 180)
    self.assertEqual(
      product["promotions"],
      [
        {
          "code": "TEST10",
          "type": "percentage",
          "value": 10,
          "description": "10% off",
        }
      ],
    )
    variant = product["variants"][0]
    self.assertEqual(variant["description"], {"plain": "An hour afloat."})
    self.assertEqual(variant["includes"], ["Private room", "Earplugs"])

  def test_deal_page_spells_out_what_you_need_to_know(self) -> None:
    """The rules, what the option includes and the price with the code."""
    page = self.client.get("/deals/float").text

    self.assertIn("Any day you book, within 180 days of buying", page)
    self.assertIn(
      "Appointment required. The contact to book comes with your voucher.",
      page,
    )
    self.assertNotIn("+420 555 0100", page)
    self.assertIn("Cancel within 14 days of buying.", page)
    self.assertIn(
      "Promotional value expires 180 days after purchase. Amount paid never"
      " expires.",
      page,
    )
    self.assertIn(
      "Limit 1 per person. May be bought again every 90 days.", page
    )
    self.assertIn("<li>Private room</li>", page)
    self.assertIn("<b>$53.10</b> with code <code>TEST10</code>", page)
    # One per person: the quantity can't go above it.
    self.assertIn('max="1"', page)
    self.assertIn("Free cancellation for 14 days", self.client.get("/").text)

  def test_web_cart_keeps_to_the_limit_and_takes_a_promo_code(self) -> None:
    """Adding too many is trimmed; a valid code shows as a discount."""
    json_headers = {"Accept": "application/json"}
    self._signup()

    def post(path: str, **form: str) -> dict:
      return self.client.post(path, data=form, headers=json_headers).json()

    post("/cart/add", product_id="float-60", quantity="1")
    second = post("/cart/add", product_id="float-60", quantity="1")
    self.assertEqual(second["count"], 1)
    self.assertTrue(second["bad"])
    self.assertIn("limit per person", second["message"])

    wrong = post("/cart/promo", code="NOPE")
    self.assertTrue(wrong["bad"])
    self.assertEqual(wrong["promo_code"], "")

    applied = post("/cart/promo", code="test10")
    self.assertEqual(applied["promo_code"], "TEST10")
    self.assertIn("Discount (TEST10)", applied["summary_html"])
    self.assertIn("-$5.90", applied["summary_html"])
    self.assertIn("<strong>$53.10</strong>", applied["summary_html"])

    # The code and the terms follow the cart into the checkout and the voucher.
    started = self.client.post("/checkout", follow_redirects=False)
    review = self.client.get(started.headers["location"]).text
    self.assertIn("Pay $53.10", review)
    self.assertIn("Includes</dt><dd>Private room, Earplugs", review)
    paid = self.client.post(
      f"{started.headers['location']}/pay",
      data={
        "full_name": "Ada Buyer",
        "email": "ada@example.com",
        "card": "ok",
        "seen_total": "5310",
      },
      headers=json_headers,
    ).json()
    self.assertEqual(paid["code"], "paid")
    voucher = self.client.get(paid["redirect"]).text
    self.assertIn("Appointment required. Book at +420 555 0100.", voucher)

    # The same person can't buy it again on the web either.
    post("/cart/add", product_id="float-60", quantity="1")
    started = self.client.post("/checkout", follow_redirects=False)
    refused = self.client.post(
      f"{started.headers['location']}/pay",
      data={
        "full_name": "Ada Buyer",
        "email": "ada@example.com",
        "card": "ok",
        "seen_total": "5900",
      },
      headers=json_headers,
    ).json()
    self.assertEqual(refused["code"], "limit")
    self.assertIn("You already bought it", refused["message"])


class AccountTest(ShopTestCase):
  """People register, sign in and out, and only see their own vouchers."""

  def _login(self, email: str, password: str, **extra: str):
    return self.client.post(
      "/login",
      data={"email": email, "password": password, **extra},
      follow_redirects=False,
    )

  def _buy_on_the_web(self) -> str:
    """Buy the spa deal as the signed-in account; return the order's path."""
    self.client.post("/cart/add", data={"product_id": "spa-60"})
    started = self.client.post("/checkout", follow_redirects=False)
    paid = self.client.post(
      f"{started.headers['location']}/pay",
      data={"card": "ok", "seen_total": "9900"},
      follow_redirects=False,
    )
    return paid.headers["location"].split("?")[0]

  def test_signing_up_creates_the_account_and_signs_in(self) -> None:
    """The new account is signed in at once, and its password isn't kept."""
    response = self._signup()

    self.assertEqual(response.headers["location"], "/")
    cookie = response.headers["set-cookie"]
    self.assertIn("session_80=", cookie)
    self.assertIn("HttpOnly", cookie)
    self.assertIn("SameSite=lax", cookie)
    home = self.client.get("/").text
    self.assertIn('href="/account">Ada</a>', home)
    self.assertNotIn('href="/login"', home)
    account = self.client.get("/account").text
    self.assertIn("ada@example.com", account)

    async def stored() -> db.User:
      async with self.transactions_session_factory() as session:
        return await db.get_user_by_email(session, "ADA@example.com")

    user = asyncio.run(stored())
    self.assertNotIn(PASSWORD, user.password_hash)
    self.assertEqual(user.full_name, "Ada Buyer")

  def test_people_cannot_register_themselves(self) -> None:
    """There is no public sign-up; the shop creates the accounts."""
    self.assertEqual(self.client.get("/signup").status_code, 404)
    self.assertEqual(self.client.post("/signup", data={}).status_code, 404)
    self.assertNotIn("/signup", self.client.get("/login").text)
    self.assertIn(
      "Accounts are set up by the shop", self.client.get("/login").text
    )
    # Creating one needs the merchant secret.
    unauthorised = self.client.post(
      "/accounts",
      json={"full_name": "Bo", "email": "bo@example.com", "password": PASSWORD},
    )
    self.assertEqual(unauthorised.status_code, 403)

  def test_the_shop_creates_accounts_and_refuses_bad_details(self) -> None:
    """Each problem is explained; passwords can be set again later."""

    def create(**fields: str):
      data = {
        "full_name": "Bo",
        "email": "bo@example.com",
        "password": PASSWORD,
      }
      return self.client.post(
        "/accounts", headers=SECRET, json={**data, **fields}
      )

    self.assertIn("at least 8 characters", create(password="short").text)
    self.assertIn("valid email", create(email="not-an-email").text)
    self.assertIn("Enter your name", create(full_name=" ").text)
    self.assertEqual(create().status_code, 200)
    self.assertIn("already an account", create(email="Bo@Example.com").text)

    changed = self.client.put(
      "/accounts/bo@example.com/password",
      headers=SECRET,
      json={"password": "another good one"},
    )
    self.assertEqual(changed.status_code, 200, changed.text)
    old = self.client.post(
      "/login",
      data={"email": "bo@example.com", "password": PASSWORD},
      follow_redirects=False,
    )
    self.assertEqual(old.status_code, 401)
    new = self.client.post(
      "/login",
      data={"email": "bo@example.com", "password": "another good one"},
      follow_redirects=False,
    )
    self.assertEqual(new.status_code, 303)

  def test_signing_in_needs_the_right_password(self) -> None:
    """A wrong password and an unknown email get the same answer."""
    self._signup()
    self.client.cookies.clear()

    wrong = self._login("ada@example.com", "not the password")
    unknown = self._login("nobody@example.com", PASSWORD)
    self.assertEqual(wrong.status_code, 401)
    self.assertEqual(unknown.status_code, 401)
    message = "That email or password isn&#x27;t right."
    self.assertIn(message, wrong.text)
    self.assertIn(message, unknown.text)
    self.assertNotIn("set-cookie", wrong.headers)

    right = self._login("ADA@example.com", PASSWORD)
    self.assertEqual(right.status_code, 303)
    self.assertIn("Ada Buyer", self.client.get("/account").text)

  def test_signing_out_ends_the_session(self) -> None:
    """The old cookie stops working, even if it is sent again."""
    self._signup()
    token = self.client.cookies["session_80"]

    self.assertEqual(self.client.post("/logout").status_code, 200)
    self.client.cookies.set("session_80", token)

    account = self.client.get("/account", follow_redirects=False)
    self.assertEqual(account.headers["location"], "/login?next=%2Faccount")

  def test_checking_out_needs_an_account_and_comes_back_after(self) -> None:
    """A signed-out shopper is sent to sign in, then back to the cart."""
    self.client.post("/cart/add", data={"product_id": "spa-60"})
    stopped = self.client.post("/checkout", follow_redirects=False)
    self.assertEqual(stopped.headers["location"], "/login?next=%2Fcart")
    self.assertIn(
      'name="next" value="/cart"', self.client.get("/login?next=/cart").text
    )

    self.client.post(
      "/accounts",
      headers=SECRET,
      json={
        "full_name": "Ada Buyer",
        "email": "ada@example.com",
        "password": PASSWORD,
      },
    )
    signed_in = self.client.post(
      "/login",
      data={"email": "ada@example.com", "password": PASSWORD, "next": "/cart"},
      follow_redirects=False,
    )
    self.assertEqual(signed_in.headers["location"], "/cart")
    # The cart filled before signing in is still there.
    self.assertIn("Spa Day for Two", self.client.get("/cart").text)
    started = self.client.post("/checkout", follow_redirects=False)
    review = self.client.get(started.headers["location"]).text
    self.assertIn("Buying as <b>Ada Buyer</b>", review)

  def test_sign_in_never_sends_the_browser_to_another_site(self) -> None:
    """A `next` that isn't a path of this shop is ignored."""
    self._signup()
    self.client.cookies.clear()

    for elsewhere in ("//evil.example", "https://evil.example", "/\\evil"):
      response = self._login("ada@example.com", PASSWORD, next=elsewhere)
      self.assertEqual(response.headers["location"], "/", elsewhere)
      self.client.cookies.clear()

  def test_vouchers_are_only_shown_to_the_account_that_bought_them(
    self,
  ) -> None:
    """Not to a signed-out visitor, and not to another account."""
    self._signup()
    order_path = self._buy_on_the_web()
    self.assertIn("SPA60-A", self.client.get(order_path).text)
    self.assertIn("Spa Day for Two", self.client.get("/vouchers").text)

    self.client.cookies.clear()
    visitor = self.client.get(order_path, follow_redirects=False)
    self.assertEqual(visitor.status_code, 303)
    self.assertIn("/login?next=", visitor.headers["location"])
    polled = self.client.get(order_path, headers={"Accept": "application/json"})
    self.assertEqual(polled.status_code, 401)
    listed = self.client.get("/vouchers", follow_redirects=False)
    self.assertEqual(listed.headers["location"], "/login?next=%2Fvouchers")

    self._signup("bob@example.com", "Bob Other")
    self.assertEqual(self.client.get(order_path).status_code, 404)
    self.assertIn("Nothing bought yet", self.client.get("/vouchers").text)

  def _agent_buys_for(self, email: str, **extra) -> str:
    """Have an agent buy the spa deal over UCP for a buyer; return the order."""
    checkout = self.client.post(
      "/checkout-sessions",
      headers=self._headers(),
      json={
        "line_items": [{"item": {"id": "spa-60"}, "quantity": 1}],
        "buyer": {"email": email, "full_name": "Ada Buyer"},
      },
    ).json()
    return self._complete(checkout["id"], **extra).json()["order"]["id"]

  def test_agent_purchase_shows_in_the_buyers_account(self) -> None:
    """What an agent buys with a person's email is theirs to see and act on."""
    self._signup()
    order_id = self._agent_buys_for("ADA@example.com")

    order = self._order(order_id)
    self.assertEqual(order["channel"], "agent")
    self.assertEqual(order["agent"], "https://agent.example/profile")
    self.assertEqual(order["buyer"]["email"], "ada@example.com")

    listed = self.client.get("/vouchers").text
    self.assertIn(f'href="/vouchers/{order_id}"', listed)
    self.assertIn("Your AI agent", listed)
    page = self.client.get(f"/vouchers/{order_id}").text
    self.assertIn("SPA60-A", page)
    self.assertIn("https://agent.example/profile", page)
    # Without the agent's account of the decision, the shop says so.
    self.assertIn("The agent did not say how you decided", page)
    self.assertIn("Paid $99 by your AI agent", page)

  def test_the_agent_can_say_how_its_customer_decided(self) -> None:
    """What an agent tells at payment time is kept and shown to the buyer."""
    self._signup()
    order_id = self._agent_buys_for(
      "ada@example.com",
      agent_context={
        "agent_name": "Ada's agent",
        "proposed_by": "ChatGPT",
        "approved_via": "card:ChatGPT",
        "approved_at": "2026-10-08T17:30:00+02:00",
        "proposal_id": "p_123",
        "records_url": "https://agent.example/evidence",
        "secret_note": "never kept",
        "approved_by": 12345,
      },
    )
    order = self._order(order_id)
    self.assertEqual(
      order["agent_context"],
      {
        "agent_name": "Ada's agent",
        "proposed_by": "ChatGPT",
        "approved_via": "card:ChatGPT",
        "approved_at": "2026-10-08T17:30:00+02:00",
        "proposal_id": "p_123",
        "records_url": "https://agent.example/evidence",
      },
    )
    listed = self.client.get("/vouchers").text
    self.assertIn("Your AI agent · approved in ChatGPT", listed)
    self.assertIn(
      "1 order · 1 ready to use · $99 spent · 1 by your AI agent", listed
    )
    # The filters narrow the list by state and by who placed the order.
    self.assertIn("SPA60-A", self.client.get("/vouchers?state=ready").text)
    self.assertIn(
      "No orders match", self.client.get("/vouchers?state=used").text
    )
    self.assertIn("No orders match", self.client.get("/vouchers?who=me").text)
    self.assertIn("SPA60-A", self.client.get("/vouchers?who=agent").text)
    page = self.client.get(f"/vouchers/{order_id}").text
    self.assertIn("Ada&#x27;s agent", page)
    self.assertIn("in ChatGPT", page)
    self.assertIn(f"https://agent.example/evidence/{order_id}", page)
    self.assertIn("Paid $99 by your AI agent, approved in ChatGPT", page)
    self.assertNotIn("never kept", page)

  def test_agent_purchase_made_before_signing_up_is_waiting(self) -> None:
    """Registering with the email an agent used brings those orders along."""
    order_id = self._agent_buys_for("cy@example.com")

    self._signup("bob@example.com", "Bob Other")
    self.assertEqual(self.client.get(f"/vouchers/{order_id}").status_code, 404)
    self.client.cookies.clear()

    self._signup("cy@example.com", "Cy Later")
    self.assertIn(
      f'href="/vouchers/{order_id}"', self.client.get("/vouchers").text
    )

  def test_web_purchase_is_recorded_as_such(self) -> None:
    """The order says it was the person, not an agent, who bought."""
    self._signup()
    order_path = self._buy_on_the_web()

    order = self._order(order_path.rsplit("/", 1)[-1])
    self.assertEqual(order["channel"], "web")
    self.assertIsNone(order["agent"])
    self.assertIn("You, on the web", self.client.get(order_path).text)

  def test_paying_needs_a_session_too(self) -> None:
    """A pay request without a session charges nothing."""
    self._signup()
    self.client.post("/cart/add", data={"product_id": "spa-60"})
    started = self.client.post("/checkout", follow_redirects=False)
    self.client.cookies.clear()

    answer = self.client.post(
      f"{started.headers['location']}/pay",
      data={"card": "ok", "seen_total": "9900"},
      headers={"Accept": "application/json"},
    ).json()

    self.assertEqual(answer["code"], "login")
    self.assertIn("/login?next=", answer["redirect"])
    self.assertEqual(self._stock("spa-60"), 2)


class CoinsTest(ShopTestCase):
  """A wallet of coins per customer: earned, spent with a card, refunded."""

  ADA = {"email": "ada@example.com", "full_name": "Ada Buyer"}

  def setUp(self) -> None:
    """Make the shop give 10% back in coins, and give Ada 40 to start with."""
    super().setUp()
    (self.test_dir / "shop.json").write_text(
      '{"name": "Test Deals", "coins_back_percent": 10}'
    )
    config._SHOP_CACHE = None
    self._grant(40)

  def _grant(self, coins: int, email: str = "ada@example.com") -> None:
    response = self.client.post(
      "/wallets/grant", headers=SECRET, json={"email": email, "coins": coins}
    )
    self.assertEqual(response.status_code, 200, response.text)

  def _balance(self, email: str = "ada@example.com") -> int:
    return self.client.get(
      "/wallet", headers=self._headers(), params={"email": email}
    ).json()["balance"]

  def _open(self, use: int, buyer: dict | None = None) -> dict:
    response = self.client.post(
      "/checkout-sessions",
      headers=self._headers(),
      json={
        "line_items": [{"item": {"id": "spa-60"}, "quantity": 1}],
        "buyer": buyer or self.ADA,
        "coins": {"use": use},
      },
    )
    self.assertEqual(response.status_code, 201, response.text)
    return response.json()

  def _totals(self, checkout: dict) -> dict[str, int]:
    return {t["type"]: t["amount"] for t in checkout["totals"]}

  def test_agent_sees_the_wallet_and_what_the_shop_gives_back(self) -> None:
    """Enough for an agent to weigh this shop against another."""
    wallet = self.client.get(
      "/wallet", headers=self._headers(), params={"email": "ADA@example.com"}
    ).json()
    self.assertEqual(wallet["balance"], 40)
    self.assertEqual(wallet["back_percent"], 10)
    self.assertEqual(wallet["coin_value"], {"amount": 100, "currency": "USD"})

    spa = self.client.post(
      "/catalog/lookup", headers=self._headers(), json={"ids": ["spa-60"]}
    ).json()["products"][0]
    self.assertEqual(spa["rewards"]["coins_back_percent"], 10)
    self.assertEqual(spa["variants"][0]["coins_earned"], 9)

  def test_paying_with_coins_and_card_shows_the_exact_split(self) -> None:
    """40 coins and $59 on the card, as the buyer approved."""
    checkout = self._open(use=40)

    self.assertEqual(
      self._totals(checkout), {"subtotal": 9900, "coins": -4000, "total": 5900}
    )
    self.assertEqual(
      checkout["coins"], {"use": 40, "applied": 40, "balance": 40, "earns": 5}
    )

    order_id = self._complete(checkout["id"]).json()["order"]["id"]
    payment = self._order(order_id)["payment"]
    self.assertEqual(payment["amount"], 5900)
    self.assertEqual(payment["coins"], 40)
    self.assertEqual(payment["coins_earned"], 5)
    # 40 spent, 5 earned on the $59 the card paid.
    self.assertEqual(self._balance(), 5)
    history = self.client.get("/wallets/ada@example.com", headers=SECRET).json()
    self.assertEqual(
      [(e["reason"], e["coins"]) for e in history["entries"]],
      [("earned", 5), ("spent", -40), ("granted", 40)],
    )
    self.assertEqual(history["entries"][0]["order_id"], order_id)

  def test_coins_never_exceed_the_wallet_or_the_price(self) -> None:
    """Asking for more than there is uses what there is."""
    self.assertEqual(self._open(use=500)["coins"]["applied"], 40)

    self._grant(200)
    checkout = self._open(use=500)
    self.assertEqual(checkout["coins"]["applied"], 99)
    self.assertEqual(self._totals(checkout)["total"], 0)

  def test_coins_alone_can_pay_and_no_card_is_charged(self) -> None:
    """With enough coins there is nothing left for the card."""
    self._grant(200)
    checkout = self._open(use=99)

    response = self.client.post(
      f"/checkout-sessions/{checkout['id']}/complete",
      headers=self._headers(),
      json={"payment": {"instruments": []}, "risk_signals": {}},
    )

    self.assertEqual(response.status_code, 200, response.text)
    payment = self._order(response.json()["order"]["id"])["payment"]
    self.assertEqual(payment["amount"], 0)
    self.assertIsNone(payment["payment_id"])
    self.assertEqual(payment["coins"], 99)
    self.assertEqual(payment["coins_earned"], 0)
    self.assertEqual(self._balance(), 141)

  def test_a_wallet_only_pays_for_its_own_buyer(self) -> None:
    """Another buyer asking for coins has none to use."""
    bob = {"email": "bob@example.com", "full_name": "Bob"}
    checkout = self._open(use=40, buyer=bob)

    self.assertNotIn("coins", self._totals(checkout))
    self.assertEqual(checkout["coins"]["applied"], 0)
    self.assertEqual(self._balance(), 40)

  def test_refund_goes_back_the_way_it_was_paid(self) -> None:
    """Coins return to the wallet; the coins the order earned are taken back."""
    order_id = self._complete(self._open(use=40)["id"]).json()["order"]["id"]
    self.assertEqual(self._balance(), 5)

    self.client.post(f"/orders/{order_id}/refund", headers=SECRET)

    self.assertEqual(self._balance(), 40)
    refund = self.client.get("/ledger", headers=SECRET).json()[-1]
    self.assertEqual(refund["event"], "REFUND_AUTHORISED")
    self.assertEqual(refund["detail"]["amount"], 5900)
    self.assertEqual(refund["detail"]["coins"], 40)

  def test_wallet_changing_after_the_checkout_was_seen_stops_the_purchase(
    self,
  ) -> None:
    """If the coins are gone by the time of paying, nothing is charged."""
    first = self._open(use=40)
    second = self._open(use=40)
    self.assertEqual(self._complete(second["id"]).status_code, 200)

    refused = self._complete(first["id"])

    self.assertEqual(refused.status_code, 409)
    self.assertEqual(refused.json()["messages"][0]["code"], "requires_consent")
    # The buyer now has the 5 coins the other order earned, and no more.
    updated = self.client.get(
      f"/checkout-sessions/{first['id']}", headers=self._headers()
    ).json()
    self.assertEqual(updated["coins"]["applied"], 5)
    self.assertEqual(self._totals(updated)["total"], 9400)

  def test_web_checkout_pays_with_coins_and_card(self) -> None:
    """A person chooses how many coins to use and sees the split."""
    self._signup()
    self.assertIn("40 coins</a>", self.client.get("/").text)
    self.assertIn("Earn 9 coins back", self.client.get("/deals/spa").text)

    self.client.post("/cart/add", data={"product_id": "spa-60"})
    started = self.client.post("/checkout", follow_redirects=False)
    here = started.headers["location"]
    review = self.client.get(here).text
    self.assertIn("You have <b>40 coins</b>", review)
    self.assertIn("Pay $99", review)

    self.client.post(f"{here}/coins", data={"use": "40"})
    review = self.client.get(here).text
    self.assertIn("<dt>40 coins</dt><dd>-$40</dd>", review)
    self.assertIn("To pay by card", review)
    self.assertIn("Pay $59", review)
    self.assertIn("earn 5 coins back", review)

    paid = self.client.post(
      f"{here}/pay",
      data={"card": "ok", "seen_total": "5900"},
      follow_redirects=False,
    )
    order = self.client.get(paid.headers["location"]).text
    self.assertIn("<dt>With coins</dt><dd>40 coins</dd>", order)
    self.assertIn("<dt>Coins earned</dt><dd>+5</dd>", order)
    account = self.client.get("/account").text
    self.assertIn("<strong>5</strong>\n        coins", account)
    self.assertIn("Worth $5 at checkout", account)
    self.assertIn("Paid with coins", account)

  def test_web_checkout_can_be_paid_with_coins_alone(self) -> None:
    """The pay button says so, and no card is asked for."""
    self._grant(200)
    self._signup()
    self.client.post("/cart/add", data={"product_id": "spa-60"})
    here = self.client.post("/checkout", follow_redirects=False).headers[
      "location"
    ]

    self.client.post(f"{here}/coins", data={"all": "99", "use": "0"})
    review = self.client.get(here).text
    self.assertIn("Pay with 99 coins", review)
    self.assertNotIn("Pay with a test card", review)

    paid = self.client.post(
      f"{here}/pay", data={"seen_total": "0"}, follow_redirects=False
    )
    self.assertIn("/vouchers/", paid.headers["location"])
    self.assertEqual(self._balance(), 141)

  def test_only_the_shop_grants_coins(self) -> None:
    """Granting and reading a wallet's history need the shop's secret."""
    grant = self.client.post(
      "/wallets/grant", json={"email": "ada@example.com", "coins": 5}
    )
    self.assertEqual(grant.status_code, 403)
    history = self.client.get("/wallets/ada@example.com")
    self.assertEqual(history.status_code, 403)
    self.assertEqual(self._balance(), 40)


class WebCheckoutTest(ShopTestCase):
  """A person buys in a browser with a cart, a checkout and a voucher page."""

  def setUp(self) -> None:
    """Sign in: checking out needs an account."""
    super().setUp()
    self._signup()

  def _post(self, path: str, **form: str):
    return self.client.post(path, data=form, follow_redirects=False)

  def _checkout_from_cart(self) -> str:
    """Add the spa deal to the cart and open a checkout; return its path."""
    added = self._post("/cart/add", product_id="spa-60", quantity="1")
    self.assertEqual(added.status_code, 303)
    self.assertEqual(added.headers["location"], "/cart")
    started = self._post("/checkout")
    self.assertEqual(started.status_code, 303)
    return started.headers["location"]

  def _assert_cart_is_empty(self) -> None:
    # The empty state is always in the page; it is hidden while there are lines.
    self.assertIn(
      '<section class="blank" data-cart-empty>', self.client.get("/cart").text
    )

  def _pay(self, checkout_path: str, card: str = "ok", total: str = "9900"):
    return self._post(
      f"{checkout_path}/pay",
      full_name="Ada Buyer",
      email="ada@example.com",
      card=card,
      seen_total=total,
    )

  def test_cart_adds_merges_and_removes(self) -> None:
    """Adding twice raises the quantity, capped at the places left."""
    self._post("/cart/add", product_id="spa-60", quantity="1")
    self._post("/cart/add", product_id="spa-60", quantity="5")

    cart = self.client.get("/cart")
    self.assertIn("Spa Day for Two", cart.text)
    # Two places are left, so six requested become two.
    self.assertIn("$198", cart.text)
    self.assertIn('id="cart-badge">2</span>', cart.text)

    line_id = cart.text.split('name="line_id" value="')[1].split('"')[0]
    self._post("/cart/update", line_id=line_id, quantity="0")
    self._assert_cart_is_empty()

  def test_sold_out_deal_cannot_be_added(self) -> None:
    """A deal with no places sends the shopper back to its page."""
    response = self._post("/cart/add", product_id="cruise-std", quantity="1")

    self.assertEqual(response.headers["location"], "/deals/cruise")
    self._assert_cart_is_empty()

  def test_web_purchase_ends_in_the_same_order_as_an_agent_purchase(
    self,
  ) -> None:
    """Paying on the web issues a voucher, takes a place, empties the cart."""
    checkout_path = self._checkout_from_cart()
    review = self.client.get(checkout_path)
    self.assertIn("Sat 10 Oct, 10:00 to 20:00", review.text)
    self.assertIn("Pay $99", review.text)

    paid = self._pay(checkout_path)

    self.assertEqual(paid.status_code, 303)
    order_path = paid.headers["location"].split("?")[0]
    order = self._order(order_path.rsplit("/", 1)[-1])
    line = order["line_items"][0]
    self.assertEqual(line["voucher"]["status"], "issued")
    self.assertEqual(order["payment"]["status"], "captured")
    self.assertEqual(self._stock("spa-60"), 1)
    self.assertEqual(
      self._events(),
      ["CHECKOUT_CREATED", "PAYMENT_CONFIRMED", "VOUCHER_ISSUED"],
    )

    voucher = self.client.get(paid.headers["location"])
    self.assertIn(line["voucher"]["codes"][0], voucher.text)
    self.assertIn("Ready to use", voucher.text)
    self.assertIn("Payment received", voucher.text)
    self._assert_cart_is_empty()
    self.assertIn("Spa Day for Two", self.client.get("/vouchers").text)

  def test_fee_added_after_the_page_was_shown_is_not_charged(self) -> None:
    """The form carries the total the person saw; a new total stops it."""
    checkout_path = self._checkout_from_cart()
    self.client.post(
      "/testing/booking-fee", headers=SECRET, json={"amount": 2500}
    )

    refused = self._pay(checkout_path)

    self.assertEqual(
      refused.headers["location"], f"{checkout_path}?notice=changed"
    )
    self.assertEqual(self._stock("spa-60"), 2)
    self.assertEqual(self._events(), ["CHECKOUT_CREATED", "CHECKOUT_CHANGED"])
    review = self.client.get(refused.headers["location"])
    self.assertIn("The total changed", review.text)
    self.assertIn("Booking fee", review.text)
    self.assertIn("Pay $124", review.text)

    # Having seen the new total, the person can accept it.
    accepted = self._pay(checkout_path, total="12400")
    self.assertIn("/vouchers/", accepted.headers["location"])

  def test_declined_card_charges_nothing(self) -> None:
    """A declined card leaves the checkout open and the place free."""
    checkout_path = self._checkout_from_cart()

    declined = self._pay(checkout_path, card="declined")

    self.assertEqual(
      declined.headers["location"], f"{checkout_path}?notice=declined"
    )
    self.assertEqual(self._stock("spa-60"), 2)
    self.assertIn(
      "declined", self.client.get(declined.headers["location"]).text
    )

  def test_voucher_page_follows_the_merchant_and_the_refund(self) -> None:
    """The person sees a cancellation and a refund on their voucher."""
    paid = self._pay(self._checkout_from_cart())
    order_path = paid.headers["location"].split("?")[0]
    order_id = order_path.rsplit("/", 1)[-1]

    self.client.post(f"/orders/{order_id}/cancel", headers=SECRET)
    self.assertIn("Cancelled by the marketplace", self.client.get(order_path).text)

    self.client.post(f"/orders/{order_id}/refund", headers=SECRET)
    page = self.client.get(order_path).text
    self.assertIn("Refunded", page)
    self.assertNotIn("Ready to use", page)


class InPageUpdateTest(WebCheckoutTest):
  """The page's script sends the same forms and gets JSON to update in place."""

  JSON = {"Accept": "application/json"}

  def _post(self, path: str, **form: str):
    return self.client.post(
      path, data=form, headers=self.JSON, follow_redirects=False
    )

  # The inherited purchases run again through the JSON answers below.
  def _checkout_from_cart(self) -> str:
    added = self._post("/cart/add", product_id="spa-60", quantity="1")
    self.assertEqual(added.status_code, 200)
    # Opening the checkout is a page change, so it stays a plain form.
    started = self.client.post("/checkout", follow_redirects=False)
    return started.headers["location"]

  def _pay(self, checkout_path: str, card: str = "ok", total: str = "9900"):
    """Pay through the script and present the answer as the form's redirect."""
    answer = super()._pay(checkout_path, card, total).json()
    location = (
      answer.get("redirect") or f"{checkout_path}?notice={answer['code']}"
    )
    self.last_answer = answer
    return _Redirect(location)

  def test_cart_adds_merges_and_removes(self) -> None:
    """Each change answers with the cart to show: count, total and lines."""
    self._post("/cart/add", product_id="spa-60", quantity="1")
    cart = self._post("/cart/add", product_id="spa-60", quantity="5").json()

    self.assertEqual(cart["count"], 2)
    self.assertIn("<strong>$198</strong>", cart["summary_html"])
    self.assertEqual(
      cart["message"], "Added Spa Day for Two · 60 min to your cart"
    )
    self.assertIn("Spa Day for Two", cart["lines_html"])
    self.assertEqual(
      self.client.get("/cart", headers=self.JSON).json()["count"], 2
    )

    line_id = (
      cart["lines_html"].split('name="line_id" value="')[1].split('"')[0]
    )
    emptied = self._post("/cart/update", line_id=line_id, quantity="0").json()
    self.assertEqual(emptied["count"], 0)
    self.assertEqual(emptied["lines_html"], "")

  def test_sold_out_deal_cannot_be_added(self) -> None:
    """The script is told why, to show it without leaving the page."""
    response = self._post("/cart/add", product_id="cruise-std", quantity="1")

    self.assertEqual(response.status_code, 409)
    self.assertIn("places left", response.json()["message"])
    self._assert_cart_is_empty()

  def test_changed_total_answer_says_what_changed(self) -> None:
    """The dialog gets the old total, the new one and the new breakdown."""
    checkout_path = self._checkout_from_cart()
    self.client.post(
      "/testing/booking-fee", headers=SECRET, json={"amount": 2500}
    )

    self._pay(checkout_path)

    self.assertEqual(self.last_answer["seen_total"], "$99")
    self.assertEqual(self.last_answer["new_total"], "$124")
    self.assertEqual(self.last_answer["new_total_cents"], 12400)
    self.assertEqual(
      self.last_answer["breakdown"],
      [["Subtotal", "$99"], ["Booking fee", "$25"]],
    )

  def test_order_page_reports_voucher_state_for_polling(self) -> None:
    """The order page asks for the voucher's state and sees it change."""
    paid = self._pay(self._checkout_from_cart())
    order_path = paid.headers["location"].split("?")[0]
    order_id = order_path.rsplit("/", 1)[-1]

    before = self.client.get(order_path, headers=self.JSON).json()
    self.assertEqual(before["paid"], "Paid")
    self.assertEqual(
      [v["state"] for v in before["vouchers"].values()], ["Ready to use"]
    )

    self.client.post(f"/orders/{order_id}/cancel", headers=SECRET)
    self.client.post(f"/orders/{order_id}/refund", headers=SECRET)
    after = self.client.get(order_path, headers=self.JSON).json()
    self.assertEqual(after["paid"], "Refunded")
    self.assertEqual(
      [v["state"] for v in after["vouchers"].values()], ["Refunded"]
    )

  def test_search_suggests_deals_while_typing(self) -> None:
    """Part of a word is enough, and the answer says what is sold out."""
    found = self.client.get("/search", params={"q": "cru"}).json()

    self.assertEqual([item["title"] for item in found], ["Dinner Cruise"])
    self.assertEqual(found[0]["url"], "/deals/cruise")
    self.assertTrue(found[0]["sold_out"])
    self.assertEqual(self.client.get("/search", params={"q": "zzz"}).json(), [])

  def test_stylesheet_and_script_are_served(self) -> None:
    """The pages load their assets; nothing else is exposed there."""
    self.assertIn(
      "text/css", self.client.get("/assets/shop.css").headers["content-type"]
    )
    self.assertEqual(self.client.get("/assets/shop.js").status_code, 200)
    self.assertEqual(self.client.get("/assets/storefront.py").status_code, 404)


class _Redirect:
  """A JSON payment answer, shaped like the redirect a plain form gets."""

  status_code = 303

  def __init__(self, location: str):
    """Initialize _Redirect."""
    self.headers = {"location": location}


class BookingTest(ShopTestCase):
  """A deal booked for a date and time when bought: slots, room, release."""

  def setUp(self) -> None:
    """Add a sauna open every evening, two cabins per hour."""
    super().setUp()
    asyncio.run(self._seed_bookable())
    # A day two days from now: always open, always in the future.
    self.day = (
      datetime.datetime.now(booking_service.timezone())
      + datetime.timedelta(days=2)
    ).date().isoformat()

  async def _seed_bookable(self) -> None:
    sauna = _deal("sauna", "Private Sauna", "wellness", "refundable")
    sauna.service_not_before = sauna.service_not_after = None
    sauna.refundable_until = None
    sauna.refund_days = 14
    sauna.voucher_expires_at = None
    sauna.voucher_valid_days = 90
    sauna.slot_days = "mon,tue,wed,thu,fri,sat,sun"
    sauna.slot_hours = "17:00-21:00"
    sauna.slot_minutes = 60
    sauna.slot_capacity = 2
    sauna.booking_days_ahead = 30
    async with self.products_session_factory() as session:
      session.add_all(
        [
          sauna,
          db.Product(id="sauna-60", title="Private Sauna · 60 min", price=4900),
          db.DealOption(
            product_id="sauna-60", deal_id="sauna", title="60 min", position=0
          ),
        ]
      )
      await session.commit()
    async with self.transactions_session_factory() as session:
      session.add_all(
        db.VoucherCode(code=f"SAUNA-{n}", product_id="sauna-60")
        for n in range(6)
      )
      await session.commit()

  def _availability(self, deal: str = "sauna", **params: str) -> dict:
    response = self.client.get(f"/deals/{deal}/availability", params=params)
    self.assertEqual(response.status_code, 200, response.text)
    return response.json()

  def _slot(self, index: int = 0) -> dict:
    """The slots of the test day, fresh from the shop."""
    listed = self._availability(**{"from": self.day, "days": 1})
    day = next(d for d in listed["days"] if d["date"] == self.day)
    return day["slots"][index]

  def _create_booked(
    self, starts_at: str, quantity: int = 1, product: str = "sauna-60"
  ):
    return self.client.post(
      "/checkout-sessions",
      headers=self._headers(),
      json={
        "line_items": [{"item": {"id": product}, "quantity": quantity}],
        "bookings": {product: starts_at},
      },
    )

  def _code(self, response) -> str:
    return response.json()["messages"][0]["code"]

  def test_availability_lists_the_calendar(self) -> None:
    """The open slots, by day, with the places left; the catalog says so."""
    listed = self._availability(**{"from": self.day, "days": 2})
    self.assertEqual(listed["timezone"], "Europe/Prague")
    self.assertEqual(listed["capacity_per_slot"], 2)
    self.assertEqual(len(listed["days"]), 2)
    day = listed["days"][0]
    self.assertEqual(day["date"], self.day)
    self.assertEqual(
      [s["starts_at"][11:16] for s in day["slots"]],
      ["17:00", "18:00", "19:00", "20:00", "21:00"],
    )
    self.assertTrue(all(s["left"] == 2 for s in day["slots"]))
    self.assertEqual(day["slots"][0]["ends_at"][11:16], "18:00")
    # A deal without a calendar needs no booking.
    self.assertFalse(self._availability("spa")["required"])
    # The catalog carries the calendar, so an agent knows before proposing.
    found = self.client.post(
      "/catalog/search", headers=self._headers(), json={"query": "sauna"}
    ).json()
    product = next(p for p in found["products"] if p["id"] == "sauna")
    booking = product["service"]["booking"]
    self.assertTrue(booking["required"])
    self.assertEqual(booking["slot_minutes"], 60)
    self.assertEqual(booking["hours"], "17:00-21:00")

  def test_a_slot_is_booked_with_the_purchase(self) -> None:
    """The checkout echoes the slot; the order keeps it; the room shrinks."""
    slot = self._slot()
    created = self._create_booked(slot["starts_at"])
    self.assertEqual(created.status_code, 201, created.text)
    checkout = created.json()
    booking = checkout["line_items"][0]["service"]["booking"]
    self.assertEqual(booking["starts_at"], slot["starts_at"])
    self.assertEqual(booking["ends_at"], slot["ends_at"])
    done = self._complete(checkout["id"])
    self.assertEqual(done.status_code, 200, done.text)
    order = self._order(done.json()["order"]["id"])
    booked = order["line_items"][0]["service"]["booking"]
    self.assertEqual(booked["status"], "booked")
    self.assertEqual(booked["starts_at"], slot["starts_at"])
    self.assertEqual(self._slot()["left"], 1)

  def test_no_slot_means_no_charge(self) -> None:
    """A deal booked at purchase is not paid until its slot is chosen."""
    checkout = self._create("sauna-60")
    booking = checkout["line_items"][0]["service"]["booking"]
    self.assertTrue(booking["required"])
    self.assertNotIn("starts_at", booking)
    refused = self._complete(checkout["id"])
    self.assertEqual(refused.status_code, 400, refused.text)
    self.assertEqual(self._code(refused), "BOOKING_REQUIRED")
    self.assertEqual(self._slot()["left"], 2)
    self.assertNotIn("PAYMENT_CAPTURED", self._events())

  def test_a_full_slot_is_refused(self) -> None:
    """Two cabins an hour: the third booking, or three at once, is refused."""
    slot = self._slot()
    for _ in range(2):
      done = self._complete(self._create_booked(slot["starts_at"]).json()["id"])
      self.assertEqual(done.status_code, 200, done.text)
    self.assertEqual(self._slot()["left"], 0)
    refused = self._create_booked(slot["starts_at"])
    self.assertEqual(refused.status_code, 409, refused.text)
    self.assertEqual(self._code(refused), "SLOT_UNAVAILABLE")
    self.assertIn("full", refused.json()["messages"][0]["content"])
    too_many = self._create_booked(self._slot(1)["starts_at"], quantity=3)
    self.assertEqual(too_many.status_code, 409, too_many.text)
    # A slot that fills up after the checkout opened is caught at payment.
    other = self._slot(2)
    waiting = self._create_booked(other["starts_at"]).json()
    for _ in range(2):
      self._complete(self._create_booked(other["starts_at"]).json()["id"])
    late = self._complete(waiting["id"])
    self.assertEqual(late.status_code, 409, late.text)
    self.assertEqual(self._code(late), "SLOT_UNAVAILABLE")
    self.assertEqual(self._slot(2)["left"], 0)

  def test_a_time_the_deal_does_not_offer_is_refused(self) -> None:
    """Off hours, closed days, the past and nonsense are all turned down."""
    off_hours = self._create_booked(f"{self.day}T09:00:00+02:00")
    self.assertEqual(off_hours.status_code, 409, off_hours.text)
    self.assertEqual(self._code(off_hours), "SLOT_UNAVAILABLE")
    past = self._create_booked("2020-01-01T17:00:00+01:00")
    self.assertEqual(past.status_code, 409, past.text)
    nonsense = self._create_booked("tomorrow at five")
    self.assertEqual(nonsense.status_code, 400, nonsense.text)
    self.assertEqual(self._code(nonsense), "INVALID_REQUEST")
    # An hour given without an offset is read in the shop's timezone.
    naive = self._create_booked(f"{self.day}T18:00")
    self.assertEqual(naive.status_code, 201, naive.text)
    starts_at = naive.json()["line_items"][0]["service"]["booking"]["starts_at"]
    self.assertTrue(starts_at.startswith(f"{self.day}T18:00:00+0"), starts_at)

  def test_a_refund_or_a_cancellation_releases_the_slot(self) -> None:
    """The places go back to the calendar; the order says released."""
    slot = self._slot()
    done = self._complete(self._create_booked(slot["starts_at"]).json()["id"])
    order_id = done.json()["order"]["id"]
    self.assertEqual(self._slot()["left"], 1)
    refunded = self.client.post(
      f"/orders/{order_id}/refund", headers=SECRET, json={"reason": "D1"}
    )
    self.assertEqual(refunded.status_code, 200, refunded.text)
    self.assertEqual(self._slot()["left"], 2)
    booking = self._order(order_id)["line_items"][0]["service"]["booking"]
    self.assertEqual(booking["status"], "released")
    done = self._complete(self._create_booked(slot["starts_at"]).json()["id"])
    cancelled = self.client.post(
      f"/orders/{done.json()['order']['id']}/cancel", headers=SECRET
    )
    self.assertEqual(cancelled.status_code, 200, cancelled.text)
    self.assertEqual(self._slot()["left"], 2)

  def test_the_web_checkout_asks_for_the_slot(self) -> None:
    """The page offers the open slots; paying waits for the choice."""
    self._signup()
    added = self.client.post(
      "/cart/add",
      data={"product_id": "sauna-60", "quantity": "1"},
      follow_redirects=False,
    )
    self.assertEqual(added.status_code, 303, added.text)
    started = self.client.post("/checkout", data={}, follow_redirects=False)
    path = started.headers["location"]
    page = self.client.get(path).text
    first, second = self._slot(0), self._slot(1)
    self.assertIn('<select name="starts_at"', page)
    self.assertIn(first["starts_at"], page)
    self.assertIn("Choose a date and time first", page)
    refused = self.client.post(
      f"{path}/pay",
      data={"card": "ok", "seen_total": "4900"},
      follow_redirects=False,
    )
    self.assertEqual(refused.status_code, 303, refused.text)
    self.assertIn("notice=booking", refused.headers["location"])
    self.assertEqual(self._slot()["left"], 2)
    chosen = self.client.post(
      f"{path}/booking",
      data={"item_id": "sauna-60", "starts_at": first["starts_at"]},
      follow_redirects=False,
    )
    self.assertIn("notice=booked", chosen.headers["location"])
    page = self.client.get(path).text
    self.assertIn("Booked for", page)
    self.assertIn("Pay $49", page)
    # Changing one's mind before paying is free.
    self.client.post(
      f"{path}/booking",
      data={"item_id": "sauna-60", "starts_at": second["starts_at"]},
      follow_redirects=False,
    )
    page = self.client.get(path).text
    self.assertIn(f'value="{second["starts_at"]}" selected', page)
    paid = self.client.post(
      f"{path}/pay",
      data={"card": "ok", "seen_total": "4900"},
      follow_redirects=False,
    )
    self.assertEqual(paid.status_code, 303, paid.text)
    self.assertIn("/vouchers/", paid.headers["location"])
    voucher = self.client.get(paid.headers["location"]).text
    self.assertIn("Booked for", voucher)
    self.assertEqual(self._slot(1)["left"], 1)
    self.assertEqual(self._slot(0)["left"], 2)


class ShopperCancelTest(ShopTestCase):
  """The customer cancels an order for a refund from its page, in time."""

  def setUp(self) -> None:
    """Sign in and buy the spa day as this account."""
    super().setUp()
    self._signup()

  def _set_deal(self, **fields) -> None:
    async def change() -> None:
      async with self.products_session_factory() as session:
        await session.execute(
          update(db.Deal).where(db.Deal.id == "spa").values(**fields)
        )
        await session.commit()

    asyncio.run(change())

  def _buy_as_ada(self) -> str:
    created = self.client.post(
      "/checkout-sessions",
      headers=self._headers(),
      json={
        "line_items": [{"item": {"id": "spa-60"}, "quantity": 1}],
        "buyer": {"email": "ada@example.com", "full_name": "Ada Buyer"},
        "coins": {"use": 0},
      },
    )
    self.assertEqual(created.status_code, 201, created.text)
    done = self._complete(created.json()["id"])
    self.assertEqual(done.status_code, 200, done.text)
    return done.json()["order"]["id"]

  def test_a_refundable_order_is_cancelled_and_refunded(self) -> None:
    """The page offers it; the money goes back; the voucher is void."""
    self._set_deal(refundable_until="2030-01-01T10:00:00+01:00")
    order_id = self._buy_as_ada()
    page = self.client.get(f"/vouchers/{order_id}").text
    self.assertIn("Cancel and get a refund", page)
    cancelled = self.client.post(
      f"/vouchers/{order_id}/cancel", follow_redirects=False
    )
    self.assertEqual(cancelled.status_code, 303, cancelled.text)
    self.assertIn("notice=refunded", cancelled.headers["location"])
    order = self._order(order_id)
    line = order["line_items"][0]
    self.assertEqual(order["payment"]["status"], "refunded")
    self.assertEqual(line["voucher"]["status"], "refunded")
    self.assertEqual(line["redemption"]["status"], "cancelled_by_shopper")
    events = self._events()
    self.assertIn("SHOPPER_CANCELLED", events)
    self.assertIn("REFUND_AUTHORISED", events)
    page = self.client.get(f"/vouchers/{order_id}").text
    self.assertNotIn("Cancel and get a refund", page)
    self.assertIn("Refunded", page)
    self.assertIn("Cancelled by you", page)
    # Once is enough.
    again = self.client.post(f"/vouchers/{order_id}/cancel", follow_redirects=False)
    self.assertIn("notice=norefund", again.headers["location"])

  def test_a_non_refundable_or_expired_order_is_refused(self) -> None:
    """No button, and the route says no, when the terms don't allow it."""
    self._set_deal(refundability="non_refundable", refundable_until=None)
    final = self._buy_as_ada()
    page = self.client.get(f"/vouchers/{final}").text
    self.assertNotIn("Cancel and get a refund", page)
    self.assertIn("This deal is not refundable.", page)
    refused = self.client.post(f"/vouchers/{final}/cancel", follow_redirects=False)
    self.assertIn("notice=norefund", refused.headers["location"])
    self.assertEqual(self._order(final)["payment"]["status"], "captured")
    self._set_deal(
      refundability="refundable", refundable_until="2020-01-01T10:00:00+01:00"
    )
    late = self._buy_as_ada()
    page = self.client.get(f"/vouchers/{late}").text
    self.assertIn("The refund period ended on 2020-01-01.", page)
    refused = self.client.post(f"/vouchers/{late}/cancel", follow_redirects=False)
    self.assertIn("notice=norefund", refused.headers["location"])

  def test_someone_else_s_order_cannot_be_cancelled(self) -> None:
    """Another account's order looks like one that doesn't exist."""
    self._set_deal(refundable_until="2030-01-01T10:00:00+01:00")
    order_id = self._buy_as_ada()
    self.client.post("/logout")
    self._signup("bob@example.com", "Bob Buyer")
    refused = self.client.post(f"/vouchers/{order_id}/cancel", follow_redirects=False)
    self.assertEqual(refused.status_code, 404, refused.text)
    self.assertEqual(self._order(order_id)["payment"]["status"], "captured")


if __name__ == "__main__":
  absltest.main()


CHROME = (
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
  " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CHATGPT = (
  "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible;"
  " ChatGPT-User/1.0; +https://openai.com/bot"
)


class VisitorTest(ShopTestCase):
  """The web door tells declared agents apart from people, and keeps hints."""

  def _checkout(self) -> str:
    self._signup()
    self.client.post("/cart/add", data={"product_id": "spa-60"})
    started = self.client.post("/checkout", follow_redirects=False)
    return started.headers["location"]

  def test_a_browser_is_a_person_with_nothing_to_note(self) -> None:
    """A normal browser declares nothing and leaves no hint."""
    who = visitor.classify({"user-agent": CHROME})
    self.assertFalse(who.declared)
    self.assertEqual(who.hints, [])

  def test_declared_agents_are_recognised_by_name_or_signature(self) -> None:
    """An agent is known by its published name or by signing its requests."""
    by_name = visitor.classify({"user-agent": CHATGPT})
    self.assertEqual((by_name.agent, by_name.declared_by),
                     ("OpenAI ChatGPT", "user-agent"))  # fmt: skip

    signed = visitor.classify(
      {
        "user-agent": CHROME,
        "signature-agent": '"https://agent.example/directory"',
        "signature-input": 'sig1=("@authority");created=1',
      }
    )
    self.assertEqual((signed.agent, signed.declared_by),
                     ("agent.example", "web-bot-auth"))  # fmt: skip

  def test_programs_leave_hints_but_are_not_declared_agents(self) -> None:
    """Headless browsers and HTTP clients are hints, not declarations."""
    headless = visitor.classify(
      {"user-agent": CHROME.replace("Chrome", "HeadlessChrome")}
    )
    self.assertFalse(headless.declared)
    self.assertEqual(headless.hints, ["headless browser"])
    self.assertEqual(
      visitor.classify({"user-agent": "python-requests/2.32"}).hints,
      ["HTTP client, not a browser"],
    )

  def test_declared_agent_may_browse_but_not_check_out(self) -> None:
    """A declared agent sees the deals and is sent to UCP to buy."""
    here = self._checkout()
    agent = {"User-Agent": CHATGPT}

    home = self.client.get("/", headers=agent)
    self.assertEqual(home.status_code, 200)
    self.assertIn("You're browsing as <b>OpenAI ChatGPT</b>", home.text)
    self.assertNotIn("You're browsing as", self.client.get("/").text)

    review = self.client.get(here, headers=agent)
    self.assertEqual(review.status_code, 403)
    self.assertIn("/.well-known/ucp", review.text)

    paid = self.client.post(
      f"{here}/pay",
      data={"card": "ok", "seen_total": "9900"},
      headers={**agent, "Accept": "application/json"},
    )
    self.assertEqual(paid.status_code, 403)
    self.assertEqual(paid.json()["code"], "agent_door")
    # Nothing was sold.
    self.assertEqual(
      self.client.get("/vouchers").text.count('<li class="line'), 0
    )

  def test_a_web_order_keeps_what_the_request_told(self) -> None:
    """A web order records how its payment request looked."""
    here = self._checkout()
    # A plain form post from a program: no page script, not a browser.
    paid = self.client.post(
      f"{here}/pay",
      data={"card": "ok", "seen_total": "9900", "shown_at": "0"},
      follow_redirects=False,
    )
    order_id = paid.headers["location"].split("/vouchers/")[1].split("?")[0]

    order = self._order(order_id)
    self.assertEqual(order["channel"], "web")
    visit = order["visit"]
    self.assertIsNone(visit["agent"])
    self.assertFalse(visit["page_script"])
    self.assertIn("not a browser", visit["hints"])
    self.assertIn("payment sent without the page's script", visit["hints"])
    self.assertGreater(visit["seconds_on_checkout"], 1000)

    page = self.client.get(f"/vouchers/{order_id}").text
    self.assertIn("You, signed in on this site", page)
    self.assertIn("not a browser, payment sent without the page", page)


class FakeStripe:
  """Enough of Stripe's API to pay, capture, cancel and refund, in memory.

  `pm_card_visa_chargeDeclined` declines, as Stripe's own test method does.
  """

  def __init__(self) -> None:
    """Start with no payments."""
    self.intents: dict[str, dict] = {}
    self.refunds: list[dict] = []
    self.calls: list[str] = []

  def transport(self) -> httpx.MockTransport:
    """Return the transport the rail's client sends to."""
    return httpx.MockTransport(self)

  @staticmethod
  def _plain(intent: dict) -> dict:
    """Return the intent unexpanded, as Stripe does: the charge is an id."""
    return {**intent, "latest_charge": intent["latest_charge"]["id"]}

  def __call__(self, request: httpx.Request) -> httpx.Response:
    """Answer one request to api.stripe.com."""
    path = request.url.path.removeprefix("/v1")
    self.calls.append(f"{request.method} {path}")
    assert request.headers["authorization"].startswith("Basic ")
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    if request.method == "POST" and path == "/payment_intents":
      if form["payment_method"] == "pm_card_visa_chargeDeclined":
        return httpx.Response(
          402,
          json={
            "error": {
              "code": "card_declined",
              "decline_code": "generic_decline",
              "message": "Your card was declined.",
            }
          },
        )
      intent = {
        "id": f"pi_{len(self.intents) + 1}",
        "amount": int(form["amount"]),
        "currency": form["currency"],
        "created": 1_790_000_000,
        "status": "requires_capture"
        if form.get("capture_method") == "manual"
        else "succeeded",
        "metadata": {"checkout_id": form.get("metadata[checkout_id]")},
        "latest_charge": {
          "id": f"ch_{len(self.intents) + 1}",
          "amount_captured": 0,
          "amount_refunded": 0,
          "payment_method_details": {
            "card": {
              "brand": "visa",
              "last4": "4242",
              "exp_month": 12,
              "exp_year": 2034,
            }
          },
          "refunds": {"data": []},
        },
      }
      self.intents[intent["id"]] = intent
      return httpx.Response(200, json=self._plain(intent))
    if path.startswith("/payment_intents/"):
      intent_id, _, action = path.removeprefix("/payment_intents/").partition(
        "/"
      )
      intent = self.intents[intent_id]
      if action == "capture":
        intent["status"] = "succeeded"
        intent["latest_charge"]["amount_captured"] = intent["amount"]
      elif action == "cancel":
        intent["status"] = "canceled"
      expanded = "expand[]=latest_charge" in str(request.url.query)
      return httpx.Response(
        200, json=intent if expanded else self._plain(intent)
      )
    if request.method == "POST" and path == "/refunds":
      refund = {
        "id": f"re_{len(self.refunds) + 1}",
        "payment_intent": form["payment_intent"],
        "amount": int(form["amount"]),
        "status": "succeeded",
        "reason": None,
        "created": 1_790_000_600,
      }
      self.refunds.append(
        {"payment_intent": refund["payment_intent"], "amount": refund["amount"]}
      )
      charge = self.intents[form["payment_intent"]]["latest_charge"]
      charge["amount_refunded"] += refund["amount"]
      charge["refunds"]["data"].append(refund)
      return httpx.Response(200, json=refund)
    return httpx.Response(404, json={"error": {"message": "no such route"}})


class StripeRailTest(ShopTestCase):
  """With --payment_rail=stripe, money is locked, taken and given back there."""

  STRIPE_PAYMENT = {
    "payment": {
      "instruments": [
        {
          "id": "instr_1",
          "handler_id": "stripe",
          "type": "card",
          "credential": {"type": "token", "token": "pm_card_visa"},
        }
      ]
    },
    "risk_signals": {},
  }

  def setUp(self) -> None:
    """Run the shop on the stripe rail, against a fake Stripe."""
    super().setUp()
    self.stripe = FakeStripe()
    payment_rail.TRANSPORT = self.stripe.transport()
    payment_rail._http = None
    FLAGS.payment_rail = "stripe"
    self.env = mock.patch.dict(
      os.environ,
      {
        "STRIPE_SECRET_KEY": "sk_test_fake",
        "STRIPE_PUBLISHABLE_KEY": "pk_test_fake",
      },
    )
    self.env.start()

  def tearDown(self) -> None:
    """Back to the mock rail."""
    self.env.stop()
    FLAGS.payment_rail = "mock"
    payment_rail.TRANSPORT = None
    payment_rail._http = None
    super().tearDown()

  def _complete(self, checkout_id: str, token: str = "pm_card_visa"):
    payment = copy.deepcopy(self.STRIPE_PAYMENT)
    payment["payment"]["instruments"][0]["credential"]["token"] = token
    return self.client.post(
      f"/checkout-sessions/{checkout_id}/complete",
      headers=self._headers(),
      json=payment,
    )

  def test_profile_announces_the_stripe_handler(self) -> None:
    """Agents learn from the profile that the shop takes Stripe, in test."""
    handlers = self.client.get("/.well-known/ucp").json()["ucp"][
      "payment_handlers"
    ]
    stripe = handlers["com.stripe"][0]
    self.assertEqual(stripe["id"], "stripe")
    self.assertEqual(stripe["config"]["publishable_key"], "pk_test_fake")
    self.assertEqual(stripe["config"]["mode"], "test")
    # The other handlers are still there.
    self.assertIn("dev.mock.payment_handler", handlers)

  def test_agent_pays_with_a_payment_method(self) -> None:
    """The total is authorised with manual capture, then captured."""
    order = self._buy()

    self.assertEqual(order["payment"]["rail"], "stripe")
    self.assertEqual(order["payment"]["status"], "captured")
    self.assertEqual(order["payment"]["amount"], 9900)
    intent = self.stripe.intents["pi_1"]
    self.assertEqual(intent["amount"], 9900)
    self.assertEqual(intent["currency"], "usd")
    self.assertEqual(intent["status"], "succeeded")
    self.assertEqual(intent["metadata"]["checkout_id"], order["checkout_id"])
    self.assertEqual(
      self.stripe.calls,
      ["POST /payment_intents", "POST /payment_intents/pi_1/capture"],
    )

  def test_a_declined_card_sells_nothing(self) -> None:
    """Stripe's refusal comes back as a 402 with its reason; stock stays."""
    response = self._complete(
      self._create()["id"], token="pm_card_visa_chargeDeclined"
    )

    self.assertEqual(response.status_code, 402, response.text)
    self.assertIn("Your card was declined", response.text)
    self.assertEqual(self._stock("spa-60"), 2)
    self.assertNotIn("VOUCHER_ISSUED", self._events())

  def test_a_mock_token_is_not_a_card_for_stripe(self) -> None:
    """The stripe handler wants a PaymentMethod; anything else is refused."""
    response = self._complete(self._create()["id"], token="success_token")

    self.assertEqual(response.status_code, 402, response.text)
    self.assertEqual(self.stripe.calls, [])

  def test_a_refund_goes_back_through_stripe(self) -> None:
    """Refunding an order refunds the captured PaymentIntent for its amount."""
    order_id = self._buy()["id"]
    self.client.post(f"/orders/{order_id}/cancel", headers=SECRET)

    refunded = self.client.post(
      f"/orders/{order_id}/refund", headers=SECRET, json={"reason": "D1"}
    )

    self.assertEqual(refunded.status_code, 200, refunded.text)
    self.assertEqual(self._order(order_id)["payment"]["status"], "refunded")
    self.assertEqual(
      self.stripe.refunds, [{"payment_intent": "pi_1", "amount": 9900}]
    )

  def test_payment_endpoint_tells_the_card_and_the_refunds(self) -> None:
    """The console reads card, amounts, refunds and the dashboard link."""
    order_id = self._buy()["id"]

    paid = self.client.get(f"/orders/{order_id}/payment", headers=SECRET)
    self.assertEqual(paid.status_code, 200, paid.text)
    info = paid.json()
    self.assertEqual(info["rail"], "stripe")
    self.assertEqual(info["mode"], "test")
    self.assertEqual(info["status"], "captured")
    self.assertEqual(
      (info["amount"], info["captured"], info["refunded"]), (9900, 9900, 0)
    )
    self.assertEqual(info["currency"], "USD")
    self.assertEqual(info["card"]["brand"], "visa")
    self.assertEqual(info["card"]["last4"], "4242")
    self.assertEqual(info["created"], "2026-09-21T14:13:20+00:00")
    self.assertEqual(
      info["url"], "https://dashboard.stripe.com/test/payments/pi_1"
    )

    self.client.post(f"/orders/{order_id}/cancel", headers=SECRET)
    self.client.post(
      f"/orders/{order_id}/refund", headers=SECRET, json={"reason": "D1"}
    )
    info = self.client.get(f"/orders/{order_id}/payment", headers=SECRET).json()
    self.assertEqual(info["status"], "refunded")
    self.assertEqual(info["refunded"], 9900)
    self.assertEqual(
      [(r["id"], r["amount"], r["status"]) for r in info["refunds"]],
      [("re_1", 9900, "succeeded")],
    )

  def test_a_decline_is_a_ledger_line(self) -> None:
    """The console counts declines: each one is recorded with its reason."""
    self._complete(self._create()["id"], token="pm_card_visa_chargeDeclined")

    declines = [
      e
      for e in self.client.get("/ledger", headers=SECRET).json()
      if e["event"] == "PAYMENT_DECLINED"
    ]
    self.assertEqual(len(declines), 1)
    self.assertEqual(declines[0]["detail"]["code"], "GENERIC_DECLINE")
    self.assertEqual(declines[0]["detail"]["total"], 9900)
    self.assertEqual(declines[0]["detail"]["channel"], "agent")
    self.assertIn("declined", declines[0]["detail"]["message"])

  def test_web_checkout_uses_stripes_card_form(self) -> None:
    """The page loads Stripe's fields; the pay form sends the PaymentMethod."""
    self._signup()
    self.client.post(
      "/cart/add", data={"product_id": "spa-60", "quantity": "1"}
    )
    here = self.client.post("/checkout", follow_redirects=False).headers[
      "location"
    ]

    page = self.client.get(here).text
    self.assertIn("https://js.stripe.com/v3/", page)
    self.assertIn('data-stripe="pk_test_fake"', page)
    self.assertNotIn('name="card"', page)
    self.assertIn("Stripe, test mode: no real money moves.", page)
    self.assertIn("payments run through Stripe in test mode", page)

    # Without a PaymentMethod there is nothing to charge.
    empty = self.client.post(
      f"{here}/pay",
      data={"seen_total": "9900"},
      headers={"Accept": "application/json"},
    ).json()
    self.assertEqual(empty["code"], "declined")
    self.assertEqual(empty["message"], "Enter your card details first.")

    paid = self.client.post(
      f"{here}/pay",
      data={"seen_total": "9900", "payment_method": "pm_card_visa"},
      follow_redirects=False,
    )
    self.assertEqual(paid.status_code, 303, paid.text)
    order_id = paid.headers["location"].split("/vouchers/")[1].split("?")[0]
    order = self._order(order_id)
    self.assertEqual(order["channel"], "web")
    self.assertEqual(order["payment"]["rail"], "stripe")
    self.assertEqual(self.stripe.intents["pi_1"]["status"], "succeeded")

  def test_web_checkout_tells_why_a_card_was_declined(self) -> None:
    """The page's script gets Stripe's reason to show in place."""
    self._signup()
    self.client.post(
      "/cart/add", data={"product_id": "spa-60", "quantity": "1"}
    )
    here = self.client.post("/checkout", follow_redirects=False).headers[
      "location"
    ]

    answer = self.client.post(
      f"{here}/pay",
      data={
        "seen_total": "9900",
        "payment_method": "pm_card_visa_chargeDeclined",
      },
      headers={"Accept": "application/json"},
    ).json()

    self.assertEqual(answer["code"], "declined")
    self.assertIn("Your card was declined", answer["message"])


class PaymentEndpointOnMockTest(ShopTestCase):
  """The payment endpoint on the mock rail, and for coins-only orders."""

  def test_mock_payment_says_it_is_simulated(self) -> None:
    """A mock payment has nothing behind it, and the console is told so."""
    order_id = self._buy()["id"]

    info = self.client.get(f"/orders/{order_id}/payment", headers=SECRET).json()

    self.assertEqual(info["rail"], "mock")
    self.assertEqual(info["mode"], "simulated")
    self.assertTrue(info["id"].startswith("mock_pay_"))
    self.assertIsNone(info["card"])
    self.assertIsNone(info["url"])

  def test_mock_decline_is_a_ledger_line(self) -> None:
    """The mock handler's refusal is recorded like a real one."""
    payment = copy.deepcopy(PAYMENT)
    payment["payment"]["instruments"][0]["credential"]["token"] = "fail_token"
    checkout_id = self._create()["id"]
    response = self.client.post(
      f"/checkout-sessions/{checkout_id}/complete",
      headers=self._headers(),
      json=payment,
    )

    self.assertEqual(response.status_code, 402)
    declines = [
      e
      for e in self.client.get("/ledger", headers=SECRET).json()
      if e["event"] == "PAYMENT_DECLINED"
    ]
    self.assertEqual(
      [d["detail"]["code"] for d in declines], ["INSUFFICIENT_FUNDS"]
    )

  def test_payment_endpoint_needs_the_secret(self) -> None:
    """Payment details are the merchant's."""
    order_id = self._buy()["id"]
    self.assertEqual(
      self.client.get(f"/orders/{order_id}/payment").status_code, 403
    )
