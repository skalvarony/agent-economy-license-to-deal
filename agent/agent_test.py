"""Tests of the agent: what the script understands and picks, and that a
purchase only happens with the person's approval, at the approved total.

The shops here are fakes that answer like the real ones. `scripts/smoke.py`
and the browser tests cover the real shops.
"""

import asyncio
import copy
import json

from brain import Call, ScriptedBrain, Step, choose, money, parse
import httpx
from model_brain import ModelBrain, messages
import pytest
from session import Session
from shops import ShopError

SETTINGS = {
  "customer": {"full_name": "Test Buyer", "email": "buyer@example.com"},
  "card": {
    "label": "Visa •••• 4242",
    "handler": "mock_payment_handler",
    "token": "success_token",
  },
}


def product(deal_id, title, prices, refundability="refundable", sold_out=()):
  """Build a catalog product the way a shop returns it."""
  return {
    "id": deal_id,
    "title": title,
    "description": {"plain": title},
    "service": {"category": "wellness", "merchant": "A venue"},
    "cancellation": {"refundability": refundability},
    "rating": {"value": 4.5, "count": 10},
    "variants": [
      {
        "id": f"{deal_id}_{label}",
        "title": f"{title} · {label}",
        "price": {"amount": price, "currency": "USD"},
        "availability": {"available": label not in sold_out},
        "options": [{"name": "Option", "label": label}],
      }
      for label, price in prices.items()
    ],
  }


class FakeShop:
  """A shop that keeps its catalogue, wallets and checkouts in memory."""

  def __init__(self, shop_id, name, products, balance=0):
    self.id, self.name, self.color = shop_id, name, "#000"
    self.url = self.public_url = f"http://{shop_id}.test"
    self.agent_profile = "http://agent.test/profile.json"
    self.products = products
    self.balance = balance
    self.fee = 0
    self.refusal = None  # a ShopError to answer `complete` with
    self.risk = None  # the fraud check the shop keeps on its orders
    self.checkouts = {}
    self.completed = []
    self.told = []  # the agent_context of each completed checkout
    self.on_call = lambda call: None

  def for_people(self, url):
    return url

  async def search(self, query=None, category=None, max_price=None):
    return self.products

  async def wallet(self, email, note="Read the coin wallet"):
    return {
      "balance": self.balance,
      "coin_value": {"amount": 100, "currency": "USD"},
      "back_percent": 10,
    }

  def _checkout(self, checkout_id):
    stored = self.checkouts[checkout_id]
    total = stored["price"] - stored["coins"] * 100 + self.fee
    totals = [{"type": "subtotal", "amount": stored["price"]}]
    if stored["coins"]:
      totals.append({"type": "coins", "amount": -stored["coins"] * 100})
    if self.fee:
      totals.append({"type": "fee", "amount": self.fee})
    totals.append({"type": "total", "amount": total})
    return {
      "id": checkout_id,
      "currency": "USD",
      "line_items": [
        {
          "item": {"id": stored["item"], "title": stored["title"]},
          "quantity": 1,
        }
      ],
      "totals": totals,
      "coins": {"applied": stored["coins"]} if stored["coins"] else {},
    }

  async def open_checkout(self, item_id, quantity, buyer, coins=0, promo=None):
    variants = [v for p in self.products for v in p["variants"]]
    variant = next((v for v in variants if v["id"] == item_id), None)
    if not variant or not variant["availability"]["available"]:
      raise ShopError(400, "OUT_OF_STOCK", "Insufficient stock.")
    checkout_id = f"co_{len(self.checkouts) + 1}"
    self.checkouts[checkout_id] = {
      "item": item_id,
      "title": variant["title"],
      "price": variant["price"]["amount"],
      "coins": min(coins, self.balance),
    }
    shown = self._checkout(checkout_id)
    self.checkouts[checkout_id]["seen"] = shown["totals"][-1]["amount"]
    return shown

  async def checkout(self, checkout_id):
    shown = self._checkout(checkout_id)
    self.checkouts[checkout_id]["seen"] = shown["totals"][-1]["amount"]
    return shown

  async def complete(self, checkout_id, card, context=None):
    assert card["handler"] and card["token"], "the card is handler + token"
    if self.refusal:
      raise self.refusal
    now = self._checkout(checkout_id)["totals"][-1]["amount"]
    if now != self.checkouts[checkout_id]["seen"]:
      raise ShopError(409, "requires_consent", "The total changed.")
    self.completed.append((checkout_id, now))
    self.told.append(context)
    return {"order": {"id": f"order_{checkout_id}"}}

  async def order(self, order_id):
    done = [c for c in self.completed if f"order_{c[0]}" == order_id]
    if not done:
      raise ShopError(403, "forbidden", "Not your order")
    checkout_id, charged = done[-1]
    stored = self.checkouts[checkout_id]
    return {
      "id": order_id,
      "placed_at": "2026-10-03T15:00:00+00:00",
      "channel": "agent",
      "agent": "http://agent.test/profile.json",
      "signature": {"status": "verified", "keyid": "shopping-agent"},
      "buyer": {"email": "buyer@example.com", "full_name": "Test Buyer"},
      "line_items": [
        {
          "item": {"id": stored["item"], "title": stored["title"]},
          "voucher": {"codes": ["TST-0001"], "status": "issued"},
          "redemption": {"status": "unredeemed"},
        }
      ],
      "payment": {
        "amount": charged,
        "coins": stored["coins"],
        "rail": "mock",
        **({"risk": self.risk} if self.risk else {}),
      },
    }


SPA = product("spa", "Spa Day for Two", {"2h": 6900, "3h": 9900, "day": 13900})
SAVER = product("saver", "Spa Day Saver", {"std": 4900}, "non_refundable")
REQUEST = "A spa day for two, refundable, under $120"


def run(events):
  """Collect a stream of events."""

  async def collect():
    return [event async for event in events]

  return asyncio.run(collect())


@pytest.fixture
def shops():
  return [
    FakeShop("a", "Shop A", [SPA], balance=40),
    FakeShop("b", "Shop B", []),
    FakeShop("c", "Shop C", [SAVER]),
  ]


@pytest.fixture
def session(shops, tmp_path):
  return Session(SETTINGS, shops, ScriptedBrain(), tmp_path)


def proposal_of(session):
  return next(p for p in session.proposals.values() if p["status"] == "pending")


def test_parse_reads_category_budget_and_refund():
  ask = parse(REQUEST)
  assert ask.category == "wellness"
  assert ask.budget == 12000
  assert ask.refundable
  assert ask.use_coins
  assert {"spa", "two"} <= ask.words


def test_parse_keeps_coins_when_asked():
  assert not parse("a massage, but keep my coins").use_coins


def test_parse_does_not_understand_small_talk():
  assert not parse("can you find me something?").understood


def test_money():
  assert money(9900) == "$99"
  assert money(4910) == "$49.10"
  assert money(-4000) == "-$40"


def test_request_ends_in_a_proposal_and_no_purchase(session, shops):
  events = run(session.say(REQUEST))
  proposal = proposal_of(session)
  # The fullest option within $120, with the wallet's 40 coins.
  assert proposal["title"] == "Spa Day for Two · 3h"
  assert proposal["total"] == 5900
  assert proposal["coins"] == {"applied": 40}
  verdicts = {(r["shop"], r["verdict"]): r for r in proposal["considered"]}
  assert verdicts["c", "rejected"]["reason"] == "Non-refundable"
  assert verdicts["b", "rejected"]["reason"] == "No wellness deals"
  assert events[-1]["type"] == "agent"
  assert "won't pay until you approve" in events[-1]["text"]
  assert not shops[0].completed


def test_approval_buys_at_the_approved_total(session, shops, tmp_path):
  run(session.say(REQUEST))
  proposal = proposal_of(session)
  events = run(session.approve(proposal["id"]))
  receipt = next(e["receipt"] for e in events if e["type"] == "receipt")
  assert receipt["codes"] == ["TST-0001"]
  assert receipt["approved"] == receipt["charged"] == 5900
  assert shops[0].completed == [(proposal["checkout_id"], 5900)]
  record = [
    json.loads(line)
    for line in (tmp_path / "approvals.jsonl").read_text().splitlines()
  ]
  assert [line["event"] for line in record] == ["approved", "purchased"]
  assert record[0]["total"] == 5900
  assert record[0]["by"] == "buyer@example.com"


def test_changed_total_stops_the_purchase_and_asks_again(session, shops):
  run(session.say(REQUEST))
  first = proposal_of(session)
  shops[0].fee = 2500
  events = run(session.approve(first["id"]))
  assert not shops[0].completed
  assert first["status"] == "changed"
  second = proposal_of(session)
  assert second["previous_total"] == 5900
  assert second["total"] == 8400
  assert "didn't pay" in events[-1]["text"]
  # The new total needs its own approval.
  run(session.approve(second["id"]))
  assert shops[0].completed == [(second["checkout_id"], 8400)]


def test_evidence_puts_the_record_and_the_order_side_by_side(session, shops):
  run(session.say(REQUEST))
  first = proposal_of(session)
  shops[0].fee = 2500
  run(session.approve(first["id"]))
  second = proposal_of(session)
  run(session.approve(second["id"]))
  order_id = f"order_{second['checkout_id']}"

  evidence = asyncio.run(session.evidence(order_id))
  # The whole chain: first approval, the stop, second approval, purchase.
  assert [line["event"] for line in evidence["record"]] == [
    "approved",
    "stopped",
    "approved",
    "purchased",
  ]
  assert evidence["order"]["payment"]["amount"] == 8400
  texts = [f["text"] for f in evidence["findings"] if f["ok"]]
  assert any("for $84, by page" in t for t in texts)
  assert any("1 earlier attempt(s) stopped: total_changed" in t for t in texts)
  assert any("charged exactly the approved total, $84" in t for t in texts)
  assert any("verified the agent's signature" in t for t in texts)
  assert any("names this agent's profile" in t for t in texts)
  assert not [f for f in evidence["findings"] if f["ok"] is False]


def test_a_three_d_secure_refusal_reaches_the_person_in_plain_words(
  session, shops
):
  run(session.say(REQUEST))
  proposal = proposal_of(session)
  shops[0].refusal = ShopError(
    402,
    "SHOPPER_ACTION_REQUIRED",
    "The card's bank asks the person to confirm this payment (3-D Secure)."
    " An agent cannot do this step for the person.",
  )
  events = run(session.approve(proposal["id"]))
  assert not shops[0].completed
  assert proposal["status"] == "failed"
  text = events[-1]["text"]
  assert "(3-D Secure)" in text
  assert "An agent cannot do this step for the person." in text
  assert text.endswith("Nothing was paid.")


def test_evidence_tells_what_the_fraud_check_said(session, shops):
  run(session.say(REQUEST))
  proposal = proposal_of(session)
  shops[0].risk = {"level": "elevated", "review": True, "score": None}
  run(session.approve(proposal["id"]))

  evidence = asyncio.run(session.evidence(f"order_{proposal['checkout_id']}"))

  texts = [f["text"] for f in evidence["findings"]]
  assert any("fraud check flagged this payment" in t for t in texts)
  assert evidence["order"]["payment"]["risk"]["level"] == "elevated"


def test_the_conversation_behind_an_order_is_kept(session, shops):
  run(session.say(REQUEST))
  proposal = proposal_of(session)
  run(session.approve(proposal["id"]))
  order_id = f"order_{proposal['checkout_id']}"
  kept = session.conversation_of(order_id)
  assert kept["order_id"] == order_id
  assert kept["brain"] == "Scripted stand-in"
  kinds = [event["type"] for event in kept["events"]]
  assert kinds[0] == "person"
  assert "proposal" in kinds and "receipt" in kinds
  assert session.conversation_of("order_nobody") is None


def test_evidence_of_an_unknown_order_says_so(session):
  evidence = asyncio.run(session.evidence("order_nobody"))
  assert evidence["record"] == []
  assert evidence["order"] is None
  assert evidence["findings"][0] == {
    "ok": False,
    "text": "This agent has no approval on record for this order.",
  }


def test_a_proposal_is_bought_once(session, shops):
  run(session.say(REQUEST))
  proposal = proposal_of(session)
  run(session.approve(proposal["id"]))
  events = run(session.approve(proposal["id"]))
  assert len(shops[0].completed) == 1
  assert "no longer open" in events[-1]["text"]


def test_declined_proposal_is_not_bought(session, shops):
  run(session.say(REQUEST))
  proposal = proposal_of(session)
  run(session.decline(proposal["id"]))
  run(session.approve(proposal["id"]))
  assert proposal["status"] == "declined"
  assert not shops[0].completed


def test_a_new_proposal_withdraws_the_one_waiting(session):
  run(session.say(REQUEST))
  first = proposal_of(session)
  run(session.say(REQUEST))
  assert first["status"] == "withdrawn"
  assert proposal_of(session)["id"] != first["id"]


def test_nothing_fits_says_why(session, shops):
  shops[0].products = [copy.deepcopy(SPA)]
  for variant in shops[0].products[0]["variants"]:
    variant["availability"]["available"] = False
  events = run(session.say(REQUEST))
  assert not session.proposals
  assert "nothing that fits" in events[-1]["text"]
  assert "sold out" in events[-1]["text"]


def test_small_talk_gets_help_not_a_search_report(session, shops):
  for shop in shops:
    shop.products = []
  events = run(session.say("hello there"))
  assert "for example" in events[-1]["text"]


def test_a_shop_that_refuses_the_agent_is_named_with_its_reason(session, shops):
  async def refuse(*args, **kwargs):
    raise ShopError(401, "signature_invalid", "Signature check failed")

  shops[2].search = refuse
  run(session.say(REQUEST))
  rows = proposal_of(session)["considered"]
  assert {"Signature check failed"} == {
    row["reason"] for row in rows if row["shop"] == "c"
  }


def test_choose_without_budget_takes_the_cheapest_option():
  found = {
    "deals": [
      {
        "shop": "a",
        "title": "Spa Day for Two",
        "description": "",
        "merchant": "",
        "refundability": "refundable",
        "options": [
          {"id": "x", "label": "2h", "price": 6900, "available": True},
          {"id": "y", "label": "3h", "price": 9900, "available": True},
        ],
      }
    ],
    "searched": [{"shop": "a", "name": "Shop A", "found": 1}],
  }
  assert choose(parse("a spa day"), found, []).option["id"] == "x"


def test_the_brain_has_no_way_to_pay(session):
  assert {tool.name for tool in session.tools} == {
    "search_deals",
    "read_wallets",
    "recall",
    "remember",
    "propose_purchase",
  }


def test_a_brain_that_asks_for_an_unknown_tool_gets_an_error(shops, tmp_path):
  class Rogue:
    name, is_model = "Rogue", False

    async def step(self, turns, tools):
      if turns[-1]["role"] == "person":
        return Step(calls=[Call("complete_checkout", {"id": "co_1"})])
      return Step(text=turns[-1]["result"]["error"])

  session = Session(SETTINGS, shops, Rogue(), tmp_path)
  events = run(session.say("buy it now"))
  assert events[-1]["text"] == "unknown_tool"


# ---- The model-backed brain, against a fake of the model's API.


def model_reply(text=None, calls=()):
  """Build a chat completions answer."""
  message = {"role": "assistant", "content": text}
  if calls:
    message["tool_calls"] = [
      {
        "id": f"call_{i}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
      }
      for i, (name, args) in enumerate(calls)
    ]
  return httpx.Response(200, json={"choices": [{"message": message}]})


def model_brain_with(replies, seen):
  """Return a ModelBrain whose API gives `replies` in turn."""
  replies = iter(replies)

  def answer(request):
    seen.append(request)
    return next(replies)

  http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
  return ModelBrain("test-key", "Test Buyer", "test-model", http=http)


def test_model_proposes_through_the_tools_and_does_not_buy(shops, tmp_path):
  seen = []
  brain = model_brain_with(
    [
      model_reply(
        calls=[
          ("search_deals", {"category": "wellness"}),
          ("read_wallets", {}),
        ]
      ),
      model_reply(
        calls=[
          (
            "propose_purchase",
            {
              "shop": "a",
              "option_id": "spa_3h",
              "coins": 40,
              "reason": "It fits.",
              "considered": [{"shop": "a", "verdict": "chosen"}],
            },
          )
        ]
      ),
      model_reply("I'd buy the spa day. Your card pays $59."),
    ],
    seen,
  )
  session = Session(SETTINGS, shops, brain, tmp_path)
  events = run(session.say(REQUEST))
  assert proposal_of(session)["total"] == 5900
  assert events[-1]["text"] == "I'd buy the spa day. Your card pays $59."
  assert not shops[0].completed

  first = json.loads(seen[0].content)
  assert seen[0].headers["authorization"] == "Bearer test-key"
  assert first["model"] == "test-model"
  assert [tool["function"]["name"] for tool in first["tools"]] == [
    "search_deals",
    "read_wallets",
    "recall",
    "remember",
    "propose_purchase",
  ]
  assert first["messages"][-1] == {"role": "user", "content": REQUEST}
  # The memory comes right after the instructions, then the conversation.
  roles = [m["role"] for m in json.loads(seen[2].content)["messages"]]
  assert roles == [
    "system", "system", "user", "assistant", "tool", "tool", "assistant", "tool"
  ]  # fmt: skip
  assert (
    "What you know about the customer"
    in json.loads(seen[2].content)["messages"][1]["content"]
  )


def test_model_is_told_what_happened_to_its_proposal(session):
  run(session.say(REQUEST))
  run(session.approve(proposal_of(session)["id"]))
  last = messages(session.turns)[-1]
  assert last["role"] == "user"
  assert last["content"].startswith("[Note] The customer approved")


def test_model_api_failure_is_said_and_nothing_is_bought(shops, tmp_path):
  refusal = httpx.Response(401, json={"error": {"message": "Bad key"}})
  session = Session(SETTINGS, shops, model_brain_with([refusal], []), tmp_path)
  events = run(session.say(REQUEST))
  assert "401: Bad key" in events[-1]["text"]
  assert not session.proposals


def test_model_sending_broken_arguments_gets_an_error_back(shops, tmp_path):
  broken = httpx.Response(
    200,
    json={
      "choices": [
        {
          "message": {
            "content": None,
            "tool_calls": [
              {
                "id": "call_0",
                "type": "function",
                "function": {"name": "propose_purchase", "arguments": "{oops"},
              }
            ],
          }
        }
      ]
    },
  )
  seen = []
  brain = model_brain_with([broken, model_reply("Sorry, let me retry.")], seen)
  session = Session(SETTINGS, shops, brain, tmp_path)
  run(session.say(REQUEST))
  result = json.loads(json.loads(seen[1].content)["messages"][-1]["content"])
  assert result["error"] == "bad_arguments"
  assert not session.proposals


# ---- Memory and rules.


def test_the_agent_remembers_what_the_person_says(session, tmp_path):
  asyncio.run(session.remember("prefers mornings"))
  assert asyncio.run(session.remember("prefers mornings"))["kept"] is False
  again = Session(
    SETTINGS, list(session.shops.values()), ScriptedBrain(), tmp_path
  )
  assert again.memory.preferences == ["prefers mornings"]
  assert "prefers mornings" in again.memory.brief([])


def test_a_hard_rule_stops_a_proposal_before_any_checkout(session, shops):
  session.memory.update(None, {"max_total": 8000})
  run(session.say(REQUEST))
  # The request said "under $120", but the rule caps it at $80: the $69
  # option is proposed instead of the $99 one.
  assert proposal_of(session)["total"] == 6900 - 4000
  session.memory.update(None, {"avoid_categories": ["wellness"]})
  events = run(session.say(REQUEST))
  # Nothing is left to propose; the earlier proposal is still the only one.
  assert len(session.proposals) == 1
  assert "nothing that fits" in events[-1]["text"]
  assert "your rule: no wellness" in events[-1]["text"]


def test_rules_are_enforced_in_code_whatever_the_brain_says(shops, tmp_path):
  class Pushy:
    name, is_model = "Pushy", False

    async def step(self, turns, tools):
      done = [t for t in turns if t["role"] == "tool"]
      if not done:
        return Step(calls=[Call("search_deals", {"category": "wellness"})])
      if len(done) == 1:
        return Step(
          calls=[
            Call("propose_purchase", {"shop": "a", "option_id": "spa_day"})
          ]
        )
      return Step(text=turns[-1]["result"].get("message", "ok"))

  session = Session(SETTINGS, shops, Pushy(), tmp_path)
  session.memory.update(None, {"max_total": 10000})
  events = run(session.say("the full day, whatever it costs"))
  assert not session.proposals
  assert "breaks your rule: never above $100" in events[-1]["text"]
  assert not shops[0].checkouts


def test_history_is_told_in_words(session, shops):
  run(session.say(REQUEST))
  run(session.approve(proposal_of(session)["id"]))
  words = session.history_words()
  assert words == [
    f"bought Spa Day for Two · 3h at Shop A for $59 on {words[0][-10:]}"
  ]


def test_purchases_show_the_shop_s_view_today(session, shops):
  run(session.say(REQUEST))
  run(session.approve(proposal_of(session)["id"]))
  rows = asyncio.run(session.purchases())
  assert len(rows) == 1
  row = rows[0]
  assert row["shop_name"] == "Shop A"
  assert row["codes"] == ["TST-0001"]
  assert row["redemption"] == "unredeemed"
  assert row["approved"] == row["charged"] == 5900
  assert row["receipt_url"] == f"/receipts/{row['order_id']}"
  assert asyncio.run(session.purchase("order_nobody")) is None


def test_the_person_can_switch_to_an_alternative(session, shops):
  shops[0].products = [SPA, product("sauna", "Sauna Evening", {"2h": 9500})]
  run(session.say(REQUEST))
  first = proposal_of(session)
  assert first["title"] == "Spa Day for Two · 3h"
  assert first["deal"]["option"]["id"] == "spa_3h"
  assert [c["title"] for c in first["shortlist"]][0] == "Spa Day for Two"
  other = next(c for c in first["shortlist"] if c["title"] == "Sauna Evening")
  events = run(session.switch(first["id"], "a", other["option"]["id"]))
  assert first["status"] == "withdrawn"
  second = proposal_of(session)
  assert second["title"] == "Sauna Evening · 2h"
  assert second["reason"] == "You picked this one from the shortlist."
  assert "Switched to Sauna Evening" in events[-1]["text"]


def test_presentation_preferences_are_kept_and_told(session):
  session.memory.update(
    None,
    None,
    {"shortlist": "3", "lead": "price", "detail": "brief", "language": "es"},
  )
  assert session.memory.presentation == {
    "shortlist": 3,
    "lead": "price",
    "detail": "brief",
    "language": "es",
  }
  assert "Write to the customer in Spanish" in session.memory.brief([])
  run(session.say(REQUEST))
  assert proposal_of(session)["presentation"]["lead"] == "price"


def test_the_agent_speaks_spanish_when_asked(session, shops):
  session.memory.update(None, None, {"language": "es"})
  events = run(session.say(REQUEST))
  assert events[-1]["text"].startswith(
    "Compraría Spa Day for Two · 3h en Shop A."
  )
  run(session.decline(proposal_of(session)["id"]))
  assert (
    session.events[-1]["text"]
    == "No lo compro. Dime qué cambiarías y vuelvo a buscar."
  )


async def _drain(stream):
  """Run an event stream to the end."""
  async for _ in stream:
    pass


# ---- The agent as a tool server for another brain (MCP).


def test_mcp_lists_the_tools_and_runs_them(session, shops, tmp_path):
  import mcp

  async def rpc(method, params=None, id=1):
    return await mcp.handle(
      session,
      {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
      "http://agent.test",
    )

  init = asyncio.run(rpc("initialize", {"clientInfo": {"name": "ChatGPT"}}))
  assert init["result"]["serverInfo"]["name"] == "shopping-agent"
  # The server speaks the client's protocol version when it is newer than
  # its own, and declares the UI extension: hosts gate the card on both.
  assert init["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
  newer = asyncio.run(rpc("initialize", {"protocolVersion": "2026-07-28"}))
  assert newer["result"]["protocolVersion"] == "2026-07-28"
  older = asyncio.run(rpc("initialize", {"protocolVersion": "2025-03-26"}))
  assert older["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
  assert init["result"]["capabilities"]["extensions"][mcp.UI_EXTENSION] == {
    "mimeTypes": [mcp.CARD_MIME]
  }
  asyncio.run(rpc("initialize", {"clientInfo": {"name": "ChatGPT"}}))
  listed = asyncio.run(rpc("tools/list"))
  names = [t["name"] for t in listed["result"]["tools"]]
  assert names == [
    "search_deals",
    "read_wallets",
    "recall",
    "remember",
    "propose_purchase",
    "approve_from_card",
    "decline_from_card",
    "search",
    "fetch",
  ]
  tools = {t["name"]: t for t in listed["result"]["tools"]}
  # The card's buttons are tools only the card may call, never the model.
  assert tools["approve_from_card"]["_meta"]["ui"]["visibility"] == ["app"]
  assert tools["propose_purchase"]["_meta"]["openai/outputTemplate"] == (
    mcp.CARD_URI
  )
  assert "pay" not in " ".join(names)

  found = asyncio.run(
    rpc("tools/call", {"name": "search", "arguments": {"query": "spa"}})
  )
  results = found["result"]["structuredContent"]["results"]
  assert any(r["id"] == "a:spa_3h" for r in results)

  proposed = asyncio.run(
    rpc(
      "tools/call",
      {
        "name": "propose_purchase",
        "arguments": {"shop": "a", "option_id": "spa_3h", "coins": 40},
      },
    )
  )
  body = proposed["result"]["structuredContent"]
  assert body["total"] == 5900
  # The link lands on the approvals, on this very proposal.
  assert (
    f"http://agent.test/approvals/{body['proposal_id']}" in body["approval"]
  )
  # The proposal is on the person's page, waiting; nothing was bought.
  proposal = proposal_of(session)
  assert proposal["title"] == "Spa Day for Two · 3h"
  assert proposal["via"] == "ChatGPT"
  assert proposal["proposed_at"]
  assert not shops[0].completed
  assert session.events[-1]["type"] == "proposal"
  assert any(
    e["type"] == "external" and e["text"].startswith("ChatGPT (over MCP)")
    for e in session.events
  )
  # It waits in the approvals, and a new episode loses neither it nor the
  # thread the person sees: only the brain starts over.
  assert [p["id"] for p in session.approvals()["pending"]] == [proposal["id"]]
  shown = len(session.events)
  session.reset()
  assert len(session.events) == shown
  assert session.turns == []
  assert [p["id"] for p in session.approvals()["pending"]] == [proposal["id"]]
  # The card: the token travels in _meta, the model can't use the button
  # without it, and with it the person's yes goes through, marked as such.
  assert "card_token" in proposed["result"]["_meta"]
  token = proposed["result"]["_meta"]["card_token"]
  assert "card_token" not in json.dumps(proposed["result"]["structuredContent"])
  refused = asyncio.run(
    rpc(
      "tools/call",
      {
        "name": "approve_from_card",
        "arguments": {"proposal_id": proposal["id"], "token": "guess"},
      },
    )
  )
  assert refused["result"]["isError"] is True
  assert proposal_of(session)["status"] == "pending"
  declined = asyncio.run(
    rpc(
      "tools/call",
      {
        "name": "decline_from_card",
        "arguments": {"proposal_id": proposal["id"], "token": token},
      },
    )
  )
  assert declined["result"]["structuredContent"]["status"] == "declined"
  assert session.approvals()["pending"] == []
  assert session.approvals()["decided"][0]["status"] == "declined"
  record = (tmp_path / "approvals.jsonl").read_text()
  assert '"method": "card:ChatGPT"' in record
  # Every line of that decision names the channel, not only the first.
  assert '"method": "page"' not in record.split('"card:ChatGPT"', 1)[1]
  # The token is spent.
  again = asyncio.run(
    rpc(
      "tools/call",
      {
        "name": "approve_from_card",
        "arguments": {"proposal_id": proposal["id"], "token": token},
      },
    )
  )
  assert again["result"]["isError"] is True
  assert again["result"]["structuredContent"]["error"] == "not_open"
  # The card itself is a resource ChatGPT can read.
  resources = asyncio.run(rpc("resources/list"))["result"]["resources"]
  assert resources[0]["uri"] == mcp.CARD_URI
  assert resources[0]["mimeType"] == "text/html;profile=mcp-app"
  card = asyncio.run(rpc("resources/read", {"uri": mcp.CARD_URI}))
  assert "approve_from_card" in card["result"]["contents"][0]["text"]

  unknown = asyncio.run(rpc("tools/call", {"name": "pay_now", "arguments": {}}))
  assert unknown["error"]["code"] == -32602
  assert (
    asyncio.run(
      mcp.handle(
        session, {"jsonrpc": "2.0", "method": "notifications/initialized"}, "x"
      )
    )
    is None
  )


def test_the_journal_keeps_every_conversation_and_decision(shops, tmp_path):
  import mcp
  from brain import ScriptedBrain

  session = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  session.mcp_key = "k"
  # A chat on the page: a proposal, declined there.
  asyncio.run(_drain(session.say("a spa day for two")))
  first = proposal_of(session)
  asyncio.run(_drain(session.decline(first["id"])))
  first_conversation = session.conversation_id
  # New chat; another brain proposes over MCP and the card approves.
  session.reset()
  asyncio.run(
    mcp.handle(
      session,
      {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"clientInfo": {"name": "Claude"}},
      },
      "http://agent.test",
    )
  )
  proposed = asyncio.run(
    mcp.handle(
      session,
      {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
          "name": "propose_purchase",
          "arguments": {"shop": "a", "option_id": "spa_3h"},
        },
      },
      "http://agent.test",
    )
  )
  second = proposed["result"]["structuredContent"]["proposal_id"]
  asyncio.run(
    mcp.handle(
      session,
      {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
          "name": "approve_from_card",
          "arguments": {
            "proposal_id": second,
            "token": proposed["result"]["_meta"]["card_token"],
          },
        },
      },
      "http://agent.test",
    )
  )

  history = session.history()
  conversations = history["conversations"]
  assert [c["id"] for c in conversations][1] == first_conversation
  assert conversations[0]["current"] is True
  # The page's chat: what was said, and the proposal.
  older = conversations[1]
  assert older["messages"][0] == {
    "role": "person",
    "text": "a spa day for two",
    "at": older["messages"][0]["at"],
  }
  assert older["proposals"][0]["id"] == first["id"]
  assert older["proposals"][0]["via"] is None
  # The MCP chat: who used the tools, and the receipt.
  newer = conversations[0]
  assert newer["proposals"][0]["via"] == "Claude"
  assert any("Claude (over MCP)" in e["text"] for e in newer["external"])
  assert newer["receipts"][0]["proposal_id"] == second
  assert newer["receipts"][0]["codes"]
  # Decisions with their channel, newest first.
  decisions = history["decisions"]
  assert decisions[0]["event"] == "purchased"
  assert decisions[0]["method"] == "card:Claude"
  # The shop was told how the customer decided, so it can show it.
  told = shops[0].told[-1]
  assert told["proposed_by"] == "Claude"
  assert told["approved_via"] == "card:Claude"
  assert told["proposal_id"] == second
  assert told["agent_name"] == "Your agent"  # SETTINGS names none
  assert told["approved_at"] and "records_url" not in told  # no public URL
  assert decisions[-1]["event"] == "declined"
  assert decisions[-1]["method"] == "page"
  # Decided proposals say how they were decided, and survive a restart.
  again = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  decided = {p["id"]: p for p in again.approvals()["decided"]}
  assert decided[first["id"]]["decided_via"] == "page"
  assert decided[second]["decided_via"] == "card:Claude"
  assert decided[second]["status"] == "bought"
  assert again.history()["conversations"][0]["id"] == newer["id"]


def test_the_shortlist_fills_up_to_three(shops, tmp_path):
  from brain import ScriptedBrain

  session = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  asyncio.run(_drain(session.say("a spa day for two")))
  proposal = proposal_of(session)
  cards = proposal["shortlist"]
  assert len(cards) == 3
  # The proposed one first, then others the agent saw; no duplicates.
  assert cards[0]["option"]["id"] == proposal["deal"]["option"]["id"]
  assert len({(c["shop"], c["option"]["id"]) for c in cards}) == 3
  assert all(c["option"].get("available", True) for c in cards)


def test_waiting_proposals_and_their_tokens_survive_a_restart(shops, tmp_path):
  import mcp
  from brain import ScriptedBrain

  first = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  first.mcp_key = "k"
  asyncio.run(
    first._run(Call("propose_purchase", {"shop": "a", "option_id": "spa_3h"}))
  )
  proposal = proposal_of(first)
  token = mcp.issue_token(first, proposal["id"])

  # A new process: the agent restarts with the same run directory.
  second = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  second.mcp_key = "k"
  assert [p["id"] for p in second.approvals()["pending"]] == [proposal["id"]]
  assert mcp.token_ok(second, proposal["id"], token)
  assert not mcp.token_ok(second, proposal["id"], token + "x")
  assert not mcp.token_ok(second, "other", token)
  asyncio.run(_drain(second.decline(proposal["id"])))
  third = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  assert third.approvals()["pending"] == []


def test_mcp_key_is_made_once_and_kept(tmp_path):
  import mcp

  first = mcp.load_key(tmp_path / "mcp_key")
  assert len(first) > 20
  assert mcp.load_key(tmp_path / "mcp_key") == first


# ---- Telegram: the same agent on the phone.


class FakeTelegram:
  """Enough of the Bot API to see what the agent sends."""

  def __init__(self):
    self.sent = []

  def transport(self):
    import httpx

    def answer(request):
      method = request.url.path.rsplit("/", 1)[-1]
      body = json.loads(request.content or b"{}")
      self.sent.append((method, body))
      if method == "getMe":
        return httpx.Response(
          200, json={"ok": True, "result": {"username": "ltd_test_bot"}}
        )
      return httpx.Response(
        200, json={"ok": True, "result": {"message_id": len(self.sent)}}
      )

    return httpx.MockTransport(answer)

  def of(self, method):
    return [b for m, b in self.sent if m == method]


def test_telegram_links_a_chat_proposes_as_a_card_and_takes_the_tap(
  shops, tmp_path
):
  import httpx
  import telegram
  from brain import ScriptedBrain

  session = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  fake = FakeTelegram()
  bot = telegram.Telegram(
    "tok",
    session,
    tmp_path,
    "http://agent.test",
    http=httpx.AsyncClient(transport=fake.transport()),
  )
  bot.username = "ltd_test_bot"
  assert bot.status()["linked"] is False

  # A stranger's /start without the code is turned away; the code links.
  asyncio.run(bot.handle({"message": {"chat": {"id": 99}, "text": "/start"}}))
  assert "open the link" in fake.of("sendMessage")[-1]["text"]
  code = bot.link_code()
  assert bot.link_url() == f"https://t.me/ltd_test_bot?start={code}"
  asyncio.run(
    bot.handle(
      {
        "message": {
          "chat": {"id": 7, "username": "ana"},
          "text": f"/start {code}",
        }
      }
    )
  )
  assert bot.status() == {
    "configured": True,
    "bot": "ltd_test_bot",
    "linked": True,
    "chat_name": "ana",
  }
  assert json.loads((tmp_path / "telegram.json").read_text())["chat_id"] == 7
  # A message from another chat is refused.
  asyncio.run(bot.handle({"message": {"chat": {"id": 99}, "text": "a spa"}}))
  assert "belongs to someone else" in fake.of("sendMessage")[-1]["text"]

  # The person asks from the phone: the agent answers, the card arrives.
  async def ask():
    await bot.handle(
      {"message": {"chat": {"id": 7}, "text": "a spa day for two"}}
    )
    await asyncio.sleep(0.05)  # The card goes out as a background task.

  asyncio.run(ask())
  proposal = proposal_of(session)
  assert proposal["via"] == "Telegram"
  cards = fake.of("sendPhoto") + [
    b for b in fake.of("sendMessage") if "reply_markup" in b
  ]
  assert cards, "no card was sent"
  card = cards[-1]
  text = card.get("caption") or card.get("text")
  assert proposal["title"] in text and "Your card pays" in text
  buttons = card["reply_markup"]["inline_keyboard"]
  assert buttons[0][0]["callback_data"] == f"approve:{proposal['id']}"
  assert buttons[1][0]["url"] == f"http://agent.test/approvals/{proposal['id']}"

  # The tap approves: the shop is paid, the card is edited, a receipt follows.
  async def tap():
    await bot.handle(
      {
        "callback_query": {
          "id": "q1",
          "data": f"approve:{proposal['id']}",
          "message": {"chat": {"id": 7}, "message_id": 3},
        }
      }
    )
    await asyncio.sleep(0.05)

  asyncio.run(tap())
  assert shops[0].completed
  assert session.proposals[proposal["id"]]["status"] == "bought"
  record = (tmp_path / "approvals.jsonl").read_text()
  assert '"method": "telegram"' in record
  edited = fake.of("editMessageCaption") + fake.of("editMessageText")
  assert edited and "Bought" in (
    edited[-1].get("caption") or edited[-1].get("text")
  )
  receipts = [
    b for b in fake.of("sendMessage") if "Voucher:" in b.get("text", "")
  ]
  assert receipts and "receipts/" in receipts[-1]["text"]
  # A second tap on the same card: nothing more happens.
  asyncio.run(tap())
  assert "no longer waiting" in fake.of("answerCallbackQuery")[-1]["text"]


def test_the_thread_is_one_and_says_where_each_word_came_from(shops, tmp_path):
  """The page draws one thread from the journal: every channel, in order."""
  session = Session(SETTINGS, shops, ScriptedBrain(), tmp_path)
  asyncio.run(_drain(session.say("a spa day for two")))
  first = proposal_of(session)
  session.via = "Telegram"
  asyncio.run(_drain(session.decline(first["id"], method="telegram")))
  asyncio.run(_drain(session.say("something cheaper")))
  session.via = None
  second = proposal_of(session)
  asyncio.run(_drain(session.approve(second["id"], method="card:ChatGPT")))

  page = session.thread(limit=100)
  items = page["items"]
  kinds = [i["type"] for i in items]
  assert kinds[0] == "person" and items[0]["via"] is None
  assert "receipt" in kinds and "proposal" in kinds
  # The decline came from Telegram, and the words said there say so.
  declined = next(
    i
    for i in items
    if i["type"] == "proposal_status" and i["status"] == "declined"
  )
  assert declined["method"] == "telegram" and declined["via"] == "Telegram"
  from_phone = [
    i for i in items if i["type"] == "person" and i["via"] == "Telegram"
  ]
  assert [i["text"] for i in from_phone] == ["something cheaper"]
  bought = next(
    i
    for i in items
    if i["type"] == "proposal_status" and i["status"] == "bought"
  )
  assert bought["method"] == "card:ChatGPT"
  # A note about another brain carries its name, like the words said then.
  session.via = "Claude Desktop"
  session.note_external("Claude Desktop (over MCP) called search_deals.")
  session.via = None
  page = session.thread(limit=100)
  items = page["items"]
  assert items[-1]["type"] == "external"
  assert items[-1]["via"] == "Claude Desktop"
  # Proposals come whole while the agent has them; ids run with the file.
  assert all(not i.get("brief") for i in items if i["type"] == "proposal")
  assert [i["id"] for i in items] == sorted(i["id"] for i in items)
  assert page["seq_now"] == session._seq and not page["has_more"]
  # Paging back stops before the given line.
  older = session.thread(before=items[3]["id"], limit=2)
  assert [i["id"] for i in older["items"]] == [items[1]["id"], items[2]["id"]]
  assert older["has_more"]
  # A reset in the middle leaves the thread numbered as one.
  session.reset()
  asyncio.run(_drain(session.say("and a sauna")))
  again = session.thread(limit=100)["items"]
  assert len(again) > len(items)
  assert len({i["conversation"] for i in again}) == 2


def test_model_gets_one_more_try_after_a_rate_limit(shops, tmp_path):
  """A 429 is retried once; the second answer counts as the first."""
  import model_brain as mb

  mb.RETRY_SECONDS = 0
  seen = []
  limited = httpx.Response(429, json={"error": {"message": "slow down"}})
  brain = model_brain_with([limited, model_reply("Nothing fits today.")], seen)
  session = Session(SETTINGS, shops, brain, tmp_path)
  events = run(session.say(REQUEST))
  assert len(seen) == 2
  assert events[-1]["text"] == "Nothing fits today."


def test_reasoning_effort_goes_only_to_models_that_take_it():
  """GPT-5 and o-series get `reasoning_effort`; other models do not."""
  seen = []

  def answer(request):
    seen.append(json.loads(request.content))
    return model_reply("ok")

  for model, expected in (("gpt-5-mini", "low"), ("gpt-4.1", None)):
    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    brain = ModelBrain("k", "Test Buyer", model, http=http)
    asyncio.run(brain.step([{"role": "person", "text": "hi"}], []))
    assert seen[-1].get("reasoning_effort") == expected, model
