"""One customer's conversation with their agent, and the purchases it leads to.

The brain (brain.py) decides what to look for and what to propose. It can't
pay. `propose_purchase` opens a checkout in a shop and shows it to the person,
and only the person's approval, which doesn't go through the brain, completes
it. An approval is for one checkout and one total: if the shop's total moved,
the purchase stops and the person is asked again.

Everything the page shows is an event in `Session.events`: what was said, each
call to a shop, each proposal and each receipt. Approvals and what came of
them are also written to `approvals.jsonl`, the agent's own record of what the
person agreed to.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Coroutine
import datetime
import json
import logging
import pathlib
from typing import Any
import time
import uuid

from brain import Brain, BrainError, Call, Tool, money
from memory import Memory
from texts import say
from shops import Shop, ShopError

logger = logging.getLogger(__name__)

# A turn that takes more steps than this is stopped: something is looping.
MAX_STEPS = 8
_TOTAL_LABELS = {
  "subtotal": "Price",
  "discount": "Promo code",
  "fee": "Booking fee",
  "total": "Your card pays",
}


def summarize(shop: Shop, product: dict[str, Any]) -> dict[str, Any]:
  """Reduce a UCP product to what choosing between deals needs."""
  service = product.get("service") or {}
  cancellation = product.get("cancellation") or {}
  promo = (product.get("promotions") or [None])[0]
  media = product.get("media") or [{}]
  return {
    "shop": shop.id,
    "shop_name": shop.name,
    "deal_id": product["id"],
    "title": product["title"],
    "description": (product.get("description") or {}).get("plain", ""),
    "category": service.get("category"),
    "merchant": service.get("merchant", ""),
    "location": service.get("location"),
    # A dated deal happens in this window; an open-dated one has none.
    "window": service.get("window"),
    "rating": product.get("rating"),
    "refundability": cancellation.get("refundability"),
    "refundable_until": cancellation.get("refundable_until"),
    "refund_days": cancellation.get("refund_days"),
    "appointment_required": (product.get("redemption") or {}).get(
      "appointment_required", False
    ),
    "promo": promo and {k: promo[k] for k in ("code", "description")},
    "coins_back_percent": (product.get("rewards") or {}).get(
      "coins_back_percent", 0
    ),
    "image": shop.for_people(media[0].get("url")),
    "gallery": [shop.for_people(m.get("url")) for m in media if m.get("url")],
    "highlights": product.get("highlights") or [],
    "about_merchant": (product.get("seller") or {}).get("description"),
    "reviews": product.get("reviews") or [],
    "options": [
      {
        # The ID to put in a purchase.
        "id": variant["id"],
        "label": (variant.get("options") or [{}])[0].get("label", ""),
        "price": variant["price"]["amount"],
        "list_price": (variant.get("list_price") or {}).get("amount"),
        "available": variant["availability"]["available"],
        "includes": variant.get("includes") or [],
        "coins_earned": variant.get("coins_earned", 0),
      }
      for variant in product["variants"]
    ],
  }


def _deal_card(
  deal: dict[str, Any] | None, option_id: str
) -> dict[str, Any] | None:
  """Return a deal with the one option the proposal is about, for the page."""
  if not deal:
    return None
  option = next((o for o in deal["options"] if o["id"] == option_id), None)
  return {**deal, "option": option}


def _total(checkout: dict[str, Any]) -> int:
  return next(t["amount"] for t in checkout["totals"] if t["type"] == "total")


class Session:
  """The conversation, the open proposal and the record of approvals."""

  def __init__(
    self,
    settings: dict[str, Any],
    shops: list[Shop],
    brain: Brain,
    run_dir: pathlib.Path | None = None,
  ) -> None:
    """Start an empty conversation for the customer in `settings`."""
    self.name = settings.get("name", "Your agent")
    self.customer = settings["customer"]
    self.card = settings["card"]
    self.shops = {shop.id: shop for shop in shops}
    self.profile = shops[0].agent_profile if shops else None
    self.memory = Memory.load(run_dir / "memory.json" if run_dir else None)
    self.brain = brain
    self.run_dir = run_dir
    for shop in shops:
      shop.on_call = self._on_call
    self.tools = self._tools()
    self._lock = asyncio.Lock()
    self._listener: asyncio.Queue | None = None
    # Other channels (Telegram…) listen to every event here.
    self._watchers: list = []
    # One thread, numbered from the start of the process: the page keeps
    # the number of the last event it drew, whichever episode it came from.
    self._seq = 0
    self.events: list[dict[str, Any]] = []
    self._last_at: float | None = None
    self.reset()
    self._load_pending()

  def reset(self) -> None:
    """Start a new episode: what the brain remembers of the conversation.

    The person sees one thread that never resets. The brain works on an
    episode of it: this is where one ends and the next begins, by hand or
    after a long silence (see `_turn`). Proposals, the record, the journal
    and the events already shown all stay.
    """
    self.turns: list[dict[str, Any]] = []
    # Each episode has an id, so the journal can tell them apart.
    self.conversation_id = uuid.uuid4().hex[:8]
    self.proposals: dict[str, dict[str, Any]] = getattr(self, "proposals", {})
    # Deals the agent has seen, by shop and option, for a proposal's photo.
    # Kept across resets: a waiting proposal may still switch between them.
    self._seen: dict[tuple[str, str], dict[str, Any]] = getattr(
      self, "_seen", {}
    )
    # Who is using the tools right now: None for this page's own brain, or
    # the name of another brain over MCP. Stamped on each proposal.
    self.via: str | None = None

  # ---- Events: what the page shows.

  def add_watcher(self, watcher) -> None:
    """Have `watcher(event)` called for every event, whichever channel."""
    self._watchers.append(watcher)

  # How many events `/api/state` still hands out; the thread endpoint has
  # the rest, from the journal.
  KEPT_EVENTS = 300
  # A silence this long ends the brain's episode; the next word starts one.
  EPISODE_GAP = 3 * 3600

  def emit(self, event: dict[str, Any]) -> None:
    """Add an event to the thread and to the open stream, if any."""
    event["seq"] = self._seq
    self._seq += 1
    # Where the person is: None for this page, "Telegram", or another
    # brain's name over MCP. The page shows it on each message.
    if event.get("type") in self.JOURNALED:
      event.setdefault("via", self.via)
      self._last_at = time.time()
    self.events.append(event)
    del self.events[: -self.KEPT_EVENTS]
    if self._listener:
      self._listener.put_nowait(event)
    self._journal(event)
    for watcher in self._watchers:
      try:
        watcher(event)
      except Exception:  # noqa: BLE001  A channel's trouble isn't the agent's.
        logging.getLogger("uvicorn.error").exception("watcher failed")

  # ---- The journal: everything that happened, kept on disk.
  #
  # One line per event in journal.jsonl, stamped with the conversation it
  # belongs to. Messages, the tools used (by this agent's brain or by another
  # over MCP), proposals and what became of them, receipts. The page's
  # The page's thread and its History read it; an episode ends, it forgets
  # none.

  JOURNALED = (
    "person",
    "agent",
    "activity",
    "proposal",
    "proposal_status",
    "receipt",
    "external",
  )

  def _journal(self, event: dict[str, Any]) -> None:
    if not self.run_dir or event.get("type") not in self.JOURNALED:
      return
    line: dict[str, Any] = {
      "at": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
      ),
      "conversation": self.conversation_id,
      "type": event["type"],
      "via": event.get("via"),
    }
    if event["type"] == "proposal":
      p = event["proposal"]
      line["proposal"] = {
        k: p.get(k)
        for k in (
          "id",
          "title",
          "shop",
          "shop_name",
          "total",
          "via",
          "image",
          "replaces",
        )
      }
    elif event["type"] == "activity":
      line.update(
        {k: event.get(k) for k in ("shop", "method", "path", "status", "note")}
      )
    elif event["type"] == "receipt":
      r = event["receipt"]
      line["receipt"] = {
        k: r.get(k)
        for k in (
          "proposal_id",
          "order_id",
          "shop",
          "shop_name",
          "title",
          "codes",
          "charged",
          "approved",
          "coins",
          "coins_earned",
          "rail",
          "url",
          "evidence_url",
          "expires_at",
        )
      }
    else:
      line.update({k: v for k, v in event.items() if k not in ("seq", "via")})
    self.run_dir.mkdir(parents=True, exist_ok=True)
    with (self.run_dir / "journal.jsonl").open("a") as journal:
      journal.write(json.dumps(line, ensure_ascii=False) + "\n")

  def journal(self, limit: int = 400) -> list[dict[str, Any]]:
    """Return the journal's last `limit` lines, oldest first."""
    return [line for _, line in self._journal_lines()[-limit:]]

  def _journal_lines(self) -> list[tuple[int, dict[str, Any]]]:
    """Return every journal line with its number, oldest first."""
    if not self.run_dir or not (self.run_dir / "journal.jsonl").is_file():
      return []
    out = []
    for n, raw in enumerate(
      (self.run_dir / "journal.jsonl").read_text().splitlines()
    ):
      try:
        out.append((n, json.loads(raw)))
      except ValueError:
        continue
    return out

  def thread(self, before: int | None = None, limit: int = 60) -> dict:
    """Return a page of the thread, oldest first, for the page to draw.

    The thread is the journal shaped like the live events: the same kinds,
    each with its number, time and channel, so the page draws past and
    present alike. Proposals come whole when the agent still has them, and
    `seq_now` says where the live events continue.
    """
    lines = self._journal_lines()
    if before is not None:
      lines = [pair for pair in lines if pair[0] < before]
    # A page is `limit` things said or shown; the calls to the shops in
    # between come along, they don't count (a purchase makes a dozen).
    page: list[tuple[int, dict[str, Any]]] = []
    counted = 0
    for pair in reversed(lines):
      if counted >= limit and pair[1]["type"] != "activity":
        break
      page.append(pair)
      counted += pair[1]["type"] != "activity"
    page.reverse()
    items = []
    for n, line in page:
      item: dict[str, Any] = {
        "id": n,
        "at": line["at"],
        "type": line["type"],
        "via": line.get("via"),
        "conversation": line.get("conversation"),
      }
      kind = line["type"]
      if kind in ("person", "agent", "external"):
        item["text"] = line.get("text", "")
      elif kind == "activity":
        item.update(
          {k: line.get(k) for k in ("shop", "method", "path", "status", "note")}
        )
      elif kind == "proposal":
        told = line.get("proposal") or {}
        whole = self.proposals.get(told.get("id") or "")
        item["proposal"] = whole or {**told, "status": "unknown"}
        item["brief"] = whole is None
      elif kind == "proposal_status":
        item["proposal_id"] = line.get("id")
        item.update({k: line.get(k) for k in ("status", "method")})
      elif kind == "receipt":
        item["receipt"] = line.get("receipt") or {}
      items.append(item)
    return {
      "items": items,
      "has_more": len(lines) > len(page),
      "seq_now": self._seq,
    }

  def history(self) -> dict[str, Any]:
    """Return what the page's History shows: conversations and the decisions.

    Conversations, newest first, each with its messages, the tools used
    and the proposals made in it; and every decision from the record, with
    the channel it came through.
    """
    by_conversation: dict[str, dict[str, Any]] = {}
    for line in self.journal():
      c = by_conversation.setdefault(
        line["conversation"],
        {
          "id": line["conversation"],
          "started_at": line["at"],
          "ended_at": line["at"],
          "current": line["conversation"] == self.conversation_id,
          "messages": [],
          "calls": 0,
          "external": [],
          "proposals": [],
          "receipts": [],
          # Everything but the calls, in the order it happened.
          "items": [],
        },
      )
      c["ended_at"] = line["at"]
      kind = line["type"]
      if kind in ("person", "agent"):
        item = {"role": kind, "text": line.get("text", ""), "at": line["at"]}
        c["messages"].append(item)
        c["items"].append({"kind": "message", **item})
      elif kind == "activity":
        c["calls"] += 1
      elif kind == "external":
        item = {"text": line.get("text", ""), "at": line["at"]}
        c["external"].append(item)
        c["items"].append({"kind": "external", **item})
      elif kind == "proposal":
        item = {**line["proposal"], "at": line["at"]}
        c["proposals"].append(item)
        c["items"].append({"kind": "proposal", **item})
      elif kind == "receipt":
        item = {**line["receipt"], "at": line["at"]}
        c["receipts"].append(item)
        c["items"].append({"kind": "receipt", **item})
    # Newest first, by order of appearance in the journal (timestamps are
    # to the second and two chats may start within one).
    conversations = list(reversed(list(by_conversation.values())))
    decisions = list(reversed(self._record_lines()))[:200]
    # The proposals behind the decisions, for their photo and their terms.
    proposals = {
      pid: {
        k: p.get(k)
        for k in (
          "id",
          "title",
          "shop",
          "shop_name",
          "total",
          "totals",
          "via",
          "image",
          "service",
          "cancellation",
          "status",
          "proposed_at",
          "decided_via",
          "decided_at",
          "reason",
          "coins",
        )
      }
      for pid, p in self.proposals.items()
    }
    return {
      "conversations": conversations,
      "decisions": decisions,
      "proposals": proposals,
    }

  def _on_call(self, call: dict[str, Any]) -> None:
    self.emit({"type": "activity", **call})

  def _say(self, text: str) -> None:
    self.emit({"type": "agent", "text": text})

  @property
  def lang(self) -> str:
    """Return the language the person wants the agent to write in."""
    return self.memory.presentation.get("language", "en")

  async def _stream(self, work: Coroutine) -> AsyncIterator[dict[str, Any]]:
    """Run `work`, yielding its events as they happen. One at a time."""
    async with self._lock:
      queue: asyncio.Queue = asyncio.Queue()
      self._listener = queue
      task = asyncio.create_task(self._guarded(work))
      task.add_done_callback(lambda _: queue.put_nowait(None))
      try:
        while (event := await queue.get()) is not None:
          yield event
      finally:
        self._listener = None
        # A page that went away mid-purchase doesn't stop the purchase.
        await asyncio.shield(task)

  async def _guarded(self, work: Coroutine) -> None:
    try:
      await work
    except Exception:
      logger.exception("The agent's turn failed")
      self._say(say("failed", self.lang))

  # ---- The conversation.

  def say(self, text: str) -> AsyncIterator[dict[str, Any]]:
    """Take what the person wrote and let the brain answer."""
    return self._stream(self._turn(text))

  def _refresh_memory_turn(self) -> None:
    """Keep the first turn as what the agent knows about the person."""
    turn = {
      "role": "memory",
      "text": self.memory.brief(self.history_words()),
      "memory": self.memory.as_dict(),
    }
    if self.turns and self.turns[0]["role"] == "memory":
      self.turns[0] = turn
    else:
      self.turns.insert(0, turn)

  async def _turn(self, text: str) -> None:
    # A long silence ends the brain's episode: the thread goes on, the
    # brain starts from what it knows about the person.
    if self._last_at and time.time() - self._last_at > self.EPISODE_GAP:
      self.reset()
    self.emit({"type": "person", "text": text})
    self.turns.append({"role": "person", "text": text})
    for _ in range(MAX_STEPS):
      self._refresh_memory_turn()
      try:
        step = await self.brain.step(self.turns, self.tools)
      except BrainError as error:
        self._say(say("cannot", self.lang, error=error))
        return
      self.turns.append(
        {"role": "agent", "text": step.text, "calls": step.calls}
      )
      if step.text:
        self._say(step.text)
      if not step.calls:
        return
      for call in step.calls:
        self.turns.append(
          {
            "role": "tool",
            "call_id": call.id,
            "name": call.name,
            "result": await self._run(call),
          }
        )
    self._say(say("too_many_steps", self.lang))

  async def _run(self, call: Call) -> Any:
    """Run one of the brain's calls. A failure is a result, not a crash."""
    tool = next((t for t in self.tools if t.name == call.name), None)
    if not tool:
      return {"error": "unknown_tool", "message": f"No tool {call.name}."}
    try:
      return await tool.run(**call.args)
    except ShopError as refusal:
      return {"error": refusal.code, "message": refusal.message}
    except (TypeError, KeyError, ValueError) as error:
      return {"error": "bad_arguments", "message": str(error)}

  # ---- Tools: all the brain can do.

  def _tools(self) -> list[Tool]:
    shop_ids = list(self.shops)
    return [
      Tool(
        name="search_deals",
        description=(
          "Search every shop for deals. Returns each deal with its shop, its"
          " terms (when, where, refund policy, promo code, coins given back)"
          " and its options. An option's `id` is what a purchase names."
          " Prices are in cents."
        ),
        parameters={
          "type": "object",
          "properties": {
            "query": {
              "type": "string",
              "description": "Words to look for in the deals.",
            },
            "category": {
              "type": "string",
              "enum": ["wellness", "food", "activities"],
            },
            "max_price": {
              "type": "integer",
              "description": "Leave out deals with no option at or under"
              " this price, in cents.",
            },
          },
        },
        run=self.search_deals,
      ),
      Tool(
        name="read_wallets",
        description=(
          "Read the customer's coins in each shop. A coin is worth $1 in its"
          " own shop only. `back_percent` is the share of a card payment the"
          " shop gives back in coins."
        ),
        parameters={"type": "object", "properties": {}},
        run=self.read_wallets,
      ),
      Tool(
        name="recall",
        description=(
          "What the agent knows about the customer: profile, preferences,"
          " the hard rules they set, and their recent purchases."
        ),
        parameters={"type": "object", "properties": {}},
        run=self.recall,
      ),
      Tool(
        name="remember",
        description=(
          "Keep a short fact the customer said about their tastes or"
          " situation, for next time ('prefers mornings', 'allergic to"
          " nuts'). Not for rules: only the customer sets those."
        ),
        parameters={
          "type": "object",
          "properties": {"fact": {"type": "string"}},
          "required": ["fact"],
        },
        run=self.remember,
      ),
      Tool(
        name="propose_purchase",
        description=(
          "Put one option in front of the customer as a proposal, with the"
          " exact price breakdown the shop quotes for it. Nothing is bought,"
          " reserved or paid by this: the customer decides later, on their"
          " own page or card, and you cannot decide for them. It replaces"
          " any proposal still waiting."
        ),
        parameters={
          "type": "object",
          "properties": {
            "shop": {"type": "string", "enum": shop_ids},
            "option_id": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1},
            "coins": {
              "type": "integer",
              "minimum": 0,
              "description": "Coins of that shop to pay with.",
            },
            "promo_code": {"type": "string"},
            "reason": {
              "type": "string",
              "description": "One sentence for the customer: why this one.",
            },
            "considered": {
              "type": "array",
              "description": "What was weighed, the chosen one first.",
              "items": {
                "type": "object",
                "properties": {
                  "shop": {"type": "string", "enum": shop_ids},
                  "title": {"type": "string"},
                  "price": {"type": "integer"},
                  "option_id": {
                    "type": "string",
                    "description": "So the customer can switch to it.",
                  },
                  "verdict": {
                    "type": "string",
                    "enum": ["chosen", "alternative", "rejected"],
                  },
                  "reason": {"type": "string"},
                },
                "required": ["shop", "verdict"],
              },
            },
          },
          "required": ["shop", "option_id"],
        },
        run=self.propose_purchase,
      ),
    ]

  async def recall(self) -> dict[str, Any]:
    """Return the memory and the recent history."""
    return {**self.memory.as_dict(), "history": self.history_words()}

  async def remember(self, fact: str) -> dict[str, Any]:
    """Keep a fact about the person."""
    kept = self.memory.remember(fact)
    if kept:
      self.emit({"type": "memory", "memory": self.memory.as_dict()})
    return {"kept": kept, "preferences": self.memory.preferences}

  def history_words(self) -> list[str]:
    """Say the recent record in words, oldest first."""
    words = []
    for line in self._record_lines()[-12:]:
      when = line["at"][:10]
      shop = self.shops.get(line["shop"])
      where = shop.name if shop else line["shop"]
      if line["event"] == "purchased":
        paid = money(line.get("charged") or line["total"])
        words.append(f"bought {line['item']} at {where} for {paid} on {when}")
      elif line["event"] == "declined":
        words.append(f"declined {line['item']} on {when}")
      elif line["event"] == "stopped" and line.get("why") == "total_changed":
        words.append(
          f"a purchase of {line['item']} stopped when the shop changed the"
          f" total on {when}"
        )
    return words

  def _record_lines(self) -> list[dict[str, Any]]:
    if not self.run_dir or not (self.run_dir / "approvals.jsonl").exists():
      return []
    return [
      json.loads(line)
      for line in (self.run_dir / "approvals.jsonl").read_text().splitlines()
      if line.strip()
    ]

  async def search_deals(
    self,
    query: str | None = None,
    category: str | None = None,
    max_price: int | None = None,
  ) -> dict[str, Any]:
    """Search every shop at once."""

    async def search(shop: Shop):
      try:
        return await shop.search(query, category, max_price)
      except ShopError as refusal:
        return refusal

    shops = list(self.shops.values())
    answers = await asyncio.gather(*(search(shop) for shop in shops))
    deals, searched = [], []
    for shop, answer in zip(shops, answers, strict=True):
      entry: dict[str, Any] = {"shop": shop.id, "name": shop.name, "found": 0}
      if isinstance(answer, ShopError):
        entry["error"] = answer.message
      else:
        entry["found"] = len(answer)
        for product in answer:
          deal = summarize(shop, product)
          deals.append(deal)
          for option in deal["options"]:
            self._seen[shop.id, option["id"]] = deal
      searched.append(entry)
    return {"deals": deals, "searched": searched}

  async def read_wallets(self, quiet: bool = False) -> list[dict[str, Any]]:
    """Read the customer's wallet in every shop.

    `quiet` reads for the page's sidebar without adding to the conversation.
    """

    async def read(shop: Shop):
      try:
        # Quiet: the sidebar's own refresh, not a step of the conversation.
        wallet = await shop.wallet(
          self.customer["email"], note=None if quiet else "Read the coin wallet"
        )
      except ShopError:
        return None
      return {
        "shop": shop.id,
        "shop_name": shop.name,
        "balance": wallet["balance"],
        "coin_value": wallet["coin_value"]["amount"],
        "back_percent": wallet["back_percent"],
      }

    wallets = await asyncio.gather(*map(read, self.shops.values()))
    wallets = [wallet for wallet in wallets if wallet]
    if not quiet:
      self.emit({"type": "wallets", "wallets": wallets})
    return wallets

  async def propose_purchase(
    self,
    shop: str,
    option_id: str,
    quantity: int = 1,
    coins: int = 0,
    promo_code: str | None = None,
    reason: str = "",
    considered: list[dict[str, Any]] | None = None,
  ) -> dict[str, Any]:
    """Open a checkout and put it in front of the person."""
    seller = self.shops[shop]
    deal = self._seen.get((shop, option_id))
    if not deal:
      # Another brain may propose an option it never searched for here:
      # read the shop's catalogue, so the card has its photo and terms and
      # the person's rules can be checked.
      with contextlib.suppress(ShopError):
        for product in await seller.search(None, None, None):
          found = summarize(seller, product)
          for option in found["options"]:
            self._seen[seller.id, option["id"]] = found
      deal = self._seen.get((shop, option_id))
    if deal:
      option = next(o for o in deal["options"] if o["id"] == option_id)
      broken = self.memory.breaks(deal, option)
      if broken:
        return {
          "error": "rule",
          "message": f"Not proposed: {deal['title']} breaks {broken}.",
          "rule": broken,
        }
    checkout = await seller.open_checkout(
      option_id, quantity, self.customer, coins, promo_code
    )
    self._close_open("withdrawn")
    proposal = self._propose(seller, checkout, reason, considered or [])
    return {
      "proposal_id": proposal["id"],
      "status": "waiting_for_the_customer",
      "shop": shop,
      "shop_name": seller.name,
      "title": proposal["title"],
      "image": proposal["image"],
      "service": proposal["service"],
      "cancellation": proposal["cancellation"],
      "totals": proposal["totals"],
      "total": proposal["total"],
      "currency": proposal["currency"],
      "coins": proposal["coins"],
      "reason": reason,
    }

  def _propose(
    self,
    shop: Shop,
    checkout: dict[str, Any],
    reason: str,
    considered: list[dict[str, Any]],
    replaces: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    line = checkout["line_items"][0]
    deal = self._seen.get((shop.id, line["item"]["id"]), {})
    codes = (checkout.get("discounts") or {}).get("codes") or []
    totals = []
    for total in checkout["totals"]:
      label = _TOTAL_LABELS.get(total["type"]) or total.get("display_text")
      if total["type"] == "coins":
        label = f"Paid with {total.get('display_text') or 'coins'}"
      if total["type"] == "discount" and codes:
        label = f"Promo code {', '.join(codes)}"
      totals.append(
        {
          "type": total["type"],
          "label": label or total["type"].capitalize(),
          "amount": total["amount"],
        }
      )
    proposal = {
      "id": uuid.uuid4().hex[:8],
      "status": "pending",
      "via": self.via,
      "proposed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
      ),
      "shop": shop.id,
      "shop_name": shop.name,
      "checkout_id": checkout["id"],
      "title": line["item"]["title"],
      "quantity": line["quantity"],
      "image": deal.get("image"),
      "service": line.get("service") or {},
      "cancellation": line.get("cancellation") or {},
      "voucher": line.get("voucher") or {},
      "redemption": line.get("redemption") or {},
      "totals": totals,
      "total": _total(checkout),
      "currency": checkout["currency"],
      "coins": checkout.get("coins") or {},
      "promo_codes": codes,
      "reason": reason,
      "considered": [
        {
          "shop": row["shop"],
          "shop_name": self.shops[row["shop"]].name,
          "title": row.get("title", ""),
          "price": row.get("price"),
          "option_id": row.get("option_id"),
          "verdict": row.get("verdict", "alternative"),
          "reason": row.get("reason", ""),
        }
        for row in considered
        if row.get("shop") in self.shops
      ],
      # The deal as the shop describes it, for the page to lay out the
      # person's way; and the alternatives the person may switch to.
      "deal": _deal_card(deal, line["item"]["id"]),
      "shortlist": self._shortlist(shop.id, line["item"]["id"], considered),
      "presentation": self.memory.presentation,
      # Set when the shop changed the total of a checkout already approved.
      "previous_total": replaces["total"] if replaces else None,
      "replaces": replaces["id"] if replaces else None,
    }
    self.proposals[proposal["id"]] = proposal
    self.emit({"type": "proposal", "proposal": proposal})
    self._save_pending()
    return proposal

  def _shortlist(
    self, shop_id: str, option_id: str, considered: list[dict[str, Any]]
  ) -> list[dict[str, Any]]:
    """Return the chosen option and two alternatives, with their deals.

    The brain's own alternatives come first; if it named fewer than two,
    the nearest other options the agent has seen fill the places: same
    category first, then closest price, never a sold-out one and never a
    rule-breaker.
    """
    picks = [(shop_id, option_id)]
    for row in considered:
      if row.get("verdict") == "alternative" and row.get("option_id"):
        pair = (row["shop"], row["option_id"])
        if pair not in picks and pair in self._seen:
          picks.append(pair)
    chosen = self._seen.get((shop_id, option_id)) or {}
    chosen_option = next(
      (o for o in chosen.get("options", []) if o["id"] == option_id), {}
    )
    if len(picks) < 3:
      candidates = []
      for (sid, oid), deal in self._seen.items():
        if (sid, oid) in picks:
          continue
        option = next((o for o in deal["options"] if o["id"] == oid), None)
        if not option or not option.get("available", True):
          continue
        if self.memory.breaks(deal, option):
          continue
        same_category = deal.get("category") == chosen.get("category")
        distance = abs(option.get("price", 0) - chosen_option.get("price", 0))
        candidates.append(((0 if same_category else 1, distance), (sid, oid)))
      for _, pair in sorted(candidates, key=lambda c: c[0]):
        if len(picks) >= 3:
          break
        picks.append(pair)
    cards = []
    for sid, oid in picks[:3]:
      deal = self._seen.get((sid, oid))
      if deal:
        cards.append(_deal_card(deal, oid))
    return cards

  def switch(
    self, proposal_id: str, shop: str, option_id: str
  ) -> AsyncIterator[dict[str, Any]]:
    """Switch to another option the person picked from the shortlist."""
    return self._stream(self._switch(proposal_id, shop, option_id))

  async def _switch(self, proposal_id: str, shop: str, option_id: str) -> None:
    proposal = self.proposals.get(proposal_id)
    if not proposal or proposal["status"] != "pending":
      self._say(say("not_open", self.lang))
      return
    if (shop, option_id) not in self._seen:
      self._say(say("unknown_option", self.lang))
      return
    deal = self._seen[(shop, option_id)]
    coins = proposal["coins"].get("use", 0) if shop == proposal["shop"] else 0
    result = await self.propose_purchase(
      shop,
      option_id,
      coins=coins,
      promo_code=(deal.get("promo") or {}).get("code"),
      reason="You picked this one from the shortlist.",
      considered=proposal["considered"],
    )
    if result.get("error"):
      self._say(result["message"])
      return
    self._note(
      f"The customer switched to {result['title']} at {result['shop_name']}."
    )
    self._say(
      say(
        "switched",
        self.lang,
        title=result["title"],
        shop=result["shop_name"],
        total=money(result["total"]),
      )
    )

  def _close(
    self, proposal: dict[str, Any], status: str, method: str | None = None
  ) -> None:
    proposal["status"] = status
    event = {"type": "proposal_status", "id": proposal["id"], "status": status}
    if method:
      event["method"] = method  # the channel the yes or no came through
    self.emit(event)
    self._save_pending()

  # Proposals still waiting are kept on disk, so a restart of the agent
  # (a deploy, say) doesn't lose what the person has yet to decide.

  KEPT_PROPOSALS = 60

  def _pending_path(self) -> pathlib.Path | None:
    return self.run_dir / "proposals.json" if self.run_dir else None

  def _save_pending(self) -> None:
    """Write the recent proposals, waiting or decided, to disk."""
    path = self._pending_path()
    if not path:
      return
    recent = list(self.proposals.values())[-self.KEPT_PROPOSALS :]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recent, ensure_ascii=False))

  def _load_pending(self) -> None:
    """Read the proposals back; the older pending.json is read once too."""
    if not self.run_dir:
      return
    for name in ("pending.json", "proposals.json"):
      path = self.run_dir / name
      if not path.is_file():
        continue
      try:
        kept = json.loads(path.read_text())
      except ValueError:
        continue
      for proposal in kept:
        if proposal.get("id"):
          self.proposals[proposal["id"]] = proposal

  def _close_open(self, status: str) -> None:
    for proposal in self.proposals.values():
      if proposal["status"] == "pending":
        self._close(proposal, status)

  # ---- The person's answer. None of this goes through the brain.

  def approve(
    self, proposal_id: str, method: str = "page"
  ) -> AsyncIterator[dict[str, Any]]:
    """Buy what the person approved, at the total they approved.

    `method` names the channel the yes came through (page, chatgpt-widget,
    telegram…) and goes into the record.
    """
    return self._stream(self._buy(proposal_id, method))

  def decline(
    self, proposal_id: str, method: str = "page"
  ) -> AsyncIterator[dict[str, Any]]:
    """Drop a proposal the person turned down."""
    return self._stream(self._decline(proposal_id, method))

  async def _decline(self, proposal_id: str, method: str = "page") -> None:
    proposal = self.proposals.get(proposal_id)
    if not proposal or proposal["status"] != "pending":
      return
    self._close(proposal, "declined", method)
    self._record("declined", proposal, method=method)
    self._note(f"The customer declined {proposal['title']}.")
    self._say(say("declined", self.lang))

  def _context(self, proposal: dict[str, Any], method: str) -> dict[str, Any]:
    """Tell the shop how the customer came to this purchase.

    The shop keeps it on the order and shows it to the customer next to the
    shop's own record: who proposed, through which channel the yes came
    (the page, a card in ChatGPT or Claude, Telegram), when, and where this
    agent keeps its records of the decision.
    """
    public = getattr(self, "public_url", None)
    told = {
      "agent_name": self.name,
      "proposed_by": proposal.get("via") or "agent",
      "approved_via": method,
      "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
      ),
      "proposal_id": proposal["id"],
    }
    if public:
      told["records_url"] = f"{public.rstrip('/')}/evidence"
    return told

  async def _buy(self, proposal_id: str, method: str = "page") -> None:
    proposal = self.proposals.get(proposal_id)
    if not proposal or proposal["status"] != "pending":
      self._say(say("not_open", self.lang))
      return
    shop = self.shops[proposal["shop"]]
    self._close(proposal, "approved", method)
    self._record("approved", proposal, method=method)
    try:
      done = await shop.complete(
        proposal["checkout_id"], self.card, self._context(proposal, method)
      )
    except ShopError as refusal:
      if refusal.code == "requires_consent":
        await self._ask_again(shop, proposal)
        return
      self._close(proposal, "failed", method)
      self._record("stopped", proposal, why=refusal.code, method=method)
      self._note(f"{shop.name} refused the purchase: {refusal.message}")
      self._say(
        say("refused", self.lang, shop=shop.name, message=refusal.message)
      )
      return

    order = await shop.order(done["order"]["id"])
    payment = order.get("payment") or {}
    line = order["line_items"][0]
    voucher = line.get("voucher") or {}
    self._close(proposal, "bought", method)
    self._record(
      "purchased",
      proposal,
      order_id=order["id"],
      charged=payment.get("amount"),
      method=method,
    )
    self.emit(
      {
        "type": "receipt",
        "receipt": {
          "proposal_id": proposal["id"],
          "shop": shop.id,
          "shop_name": shop.name,
          "order_id": order["id"],
          "title": line["item"]["title"],
          "codes": voucher.get("codes") or [],
          "expires_at": voucher.get("expires_at"),
          "cancellation": line.get("cancellation") or {},
          "redemption": line.get("redemption") or {},
          "service": line.get("service") or {},
          "approved": proposal["total"],
          "charged": payment.get("amount"),
          "coins": payment.get("coins", 0),
          "coins_earned": payment.get("coins_earned", 0),
          "rail": payment.get("rail"),
          "url": f"{shop.public_url}/vouchers/{order['id']}",
          "evidence_url": f"/evidence/{order['id']}",
        },
      }
    )
    self._note(
      f"The customer approved and {proposal['title']} was bought at"
      f" {shop.name}: order {order['id']}."
    )
    self._keep_conversation(order["id"])
    self._say(say("bought", self.lang, title=proposal["title"], shop=shop.name))
    if proposal["coins"]:
      await self.read_wallets()

  async def _ask_again(self, shop: Shop, proposal: dict[str, Any]) -> None:
    """Stop and ask again: the shop changed the total after the approval."""
    self._close(proposal, "changed")
    self._record("stopped", proposal, why="total_changed")
    checkout = await shop.checkout(proposal["checkout_id"])
    changed = self._propose(
      shop, checkout, proposal["reason"], [], replaces=proposal
    )
    self._note(
      f"{shop.name} changed the total of {proposal['title']} from"
      f" {money(proposal['total'])} to {money(changed['total'])} after the"
      " customer approved. Nothing was paid; the customer was asked again."
    )
    self._say(
      say(
        "changed",
        self.lang,
        shop=shop.name,
        old=money(proposal["total"]),
        new=money(changed["total"]),
      )
    )

  # ---- Purchases: everything this agent bought, with the shop's view today.

  async def purchases(self) -> list[dict[str, Any]]:
    """Return every purchase on record, newest first, as the shops see it."""
    bought = {}
    for line in self._record_lines():
      if line["event"] == "purchased" and line.get("order_id"):
        bought[line["order_id"]] = line

    async def look(line: dict[str, Any]) -> dict[str, Any]:
      shop = self.shops.get(line["shop"])
      proposal = self.proposals.get(line.get("proposal_id") or "") or {}
      row = {
        "order_id": line["order_id"],
        "proposal_id": line.get("proposal_id"),
        "at": line["at"],
        "shop": line["shop"],
        "shop_name": shop.name if shop else line["shop"],
        "title": line["item"],
        # How the yes came, and who proposed: for the page's purchase sheet.
        "method": line.get("method", "page"),
        "via": proposal.get("via"),
        "image": proposal.get("image"),
        "reason": proposal.get("reason"),
        "approved": line["total"],
        "charged": line.get("charged"),
        "coins": line.get("coins", 0),
        "codes": [],
        "voucher": None,
        "redemption": None,
        "payment": None,
        "service": {},
        "cancellation": {},
        "shop_url": f"{shop.public_url}/vouchers/{line['order_id']}"
        if shop
        else None,
        "evidence_url": f"/evidence/{line['order_id']}",
        "receipt_url": f"/receipts/{line['order_id']}",
        "error": None,
      }
      if not shop:
        return row
      try:
        order = await shop.order(line["order_id"])
      except ShopError as refusal:
        row["error"] = refusal.message
        return row
      item = (order.get("line_items") or [{}])[0]
      payment = order.get("payment") or {}
      row.update(
        {
          "codes": (item.get("voucher") or {}).get("codes") or [],
          "voucher": (item.get("voucher") or {}).get("status"),
          "redemption": (item.get("redemption") or {}).get("status"),
          "payment": payment.get("status"),
          "charged": payment.get("amount", row["charged"]),
          "coins": payment.get("coins", row["coins"]),
          "coins_earned": payment.get("coins_earned", 0),
          "payment_id": payment.get("payment_id"),
          "rail": payment.get("rail"),
          "service": item.get("service") or {},
          "cancellation": item.get("cancellation") or {},
          "voucher_terms": item.get("voucher") or {},
          "redemption_terms": item.get("redemption") or {},
          "quantity": (item.get("quantity") or {}).get("total", 1),
          "buyer": order.get("buyer") or {},
        }
      )
      return row

    rows = await asyncio.gather(*(look(line) for line in bought.values()))
    return sorted(rows, key=lambda row: row["at"], reverse=True)

  async def purchase(self, order_id: str) -> dict[str, Any] | None:
    """Return one purchase, or None if this agent didn't make it."""
    return next(
      (row for row in await self.purchases() if row["order_id"] == order_id),
      None,
    )

  # ---- Evidence: for "I never bought this".

  def record_of(self, order_id: str) -> list[dict[str, Any]]:
    """Return the record's lines behind an order, oldest first.

    The purchase line names the order; the lines of its proposal and of the
    proposals it replaced (a total that changed) come with it.
    """
    lines = self._record_lines()
    by_proposal: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
      by_proposal.setdefault(line["proposal_id"], []).append(line)
    wanted = {
      line["proposal_id"] for line in lines if line.get("order_id") == order_id
    }
    # Follow the chain of replaced proposals back.
    pending = list(wanted)
    while pending:
      for line in by_proposal.get(pending.pop(), []):
        earlier = line.get("replaces")
        if earlier and earlier not in wanted:
          wanted.add(earlier)
          pending.append(earlier)
    return sorted(
      (line for line in lines if line["proposal_id"] in wanted),
      key=lambda line: line["at"],
    )

  async def evidence(self, order_id: str) -> dict[str, Any]:
    """Put the agent's record and the shop's order side by side."""
    record = self.record_of(order_id)
    purchase = next(
      (line for line in record if line["event"] == "purchased"), None
    )
    approval = next(
      (
        line
        for line in record
        if line["event"] == "approved"
        and purchase
        and line["proposal_id"] == purchase["proposal_id"]
      ),
      None,
    )
    shop = self.shops.get(purchase["shop"]) if purchase else None
    order: dict[str, Any] | None = None
    refusal: str | None = None
    if shop:
      try:
        order = await shop.order(order_id)
      except ShopError as error:
        refusal = f"{error.status or 'no answer'}: {error.message}"
    else:
      # The agent has no purchase on record: ask every shop it knows.
      for candidate in self.shops.values():
        try:
          order = await candidate.order(order_id)
          shop = candidate
          break
        except ShopError as error:
          refusal = f"{error.status or 'no answer'}: {error.message}"

    findings = []

    def find(ok: bool | None, text: str) -> None:
      findings.append({"ok": ok, "text": text})

    if approval:
      when = approval["at"][:19].replace("T", " at ")
      find(
        True,
        f"Approved by {approval['by']} on {when} UTC, for"
        f" {money(approval['total'])}, by {approval['method']}.",
      )
    else:
      find(False, "This agent has no approval on record for this order.")
    stopped = [line for line in record if line["event"] == "stopped"]
    if stopped:
      whys = ", ".join(line.get("why", "") for line in stopped)
      find(
        True,
        f"{len(stopped)} earlier attempt(s) stopped: {whys}. Nothing was"
        " paid then.",
      )
    payment = (order or {}).get("payment") or {}
    if order:
      charged = payment.get("amount")
      if approval and charged == approval["total"]:
        find(
          True,
          f"The shop charged exactly the approved total, {money(charged)}.",
        )
      elif approval:
        find(
          False,
          f"The shop charged {money(charged or 0)}; the approval was for"
          f" {money(approval['total'])}.",
        )
      signature = order.get("signature") or {}
      if signature.get("status") == "verified":
        find(
          True,
          "The shop verified the agent's signature"
          f" (key {signature.get('keyid')}).",
        )
      elif signature:
        find(
          False,
          f"The shop did not verify a signature: {signature.get('status')}.",
        )
      if order.get("agent") == self.profile:
        find(True, "The order names this agent's profile.")
      elif order.get("agent"):
        find(False, f"The order names another agent: {order['agent']}.")
      else:
        find(False, "The order names no agent: it was placed on the web.")
      buyer = (order.get("buyer") or {}).get("email")
      if buyer == self.customer["email"]:
        find(True, f"The buyer is this agent's customer, {buyer}.")
      else:
        find(
          False,
          f"The buyer on the order is {buyer}, not {self.customer['email']}.",
        )
    else:
      find(
        None, f"The shop didn't give the order ({refusal or 'unknown shop'})."
      )
    find(
      None,
      "The approval is a button press this agent recorded. The shop can't"
      " verify it yet; a signed approval is what would close that gap.",
    )

    return {
      "order_id": order_id,
      "shop": shop
      and {"id": shop.id, "name": shop.name, "url": shop.public_url},
      "customer": self.customer,
      "agent_profile": self.profile,
      "record": record,
      "order": order
      and {
        "placed_at": order.get("placed_at"),
        "channel": order.get("channel"),
        "agent": order.get("agent"),
        "signature": order.get("signature"),
        "buyer": order.get("buyer"),
        "payment": payment,
        "visit": order.get("visit"),
        "items": [
          {
            "title": line["item"]["title"],
            "codes": (line.get("voucher") or {}).get("codes") or [],
            "voucher": (line.get("voucher") or {}).get("status"),
            "redemption": (line.get("redemption") or {}).get("status"),
          }
          for line in order.get("line_items") or []
        ],
      },
      "findings": findings,
    }

  def _keep_conversation(self, order_id: str) -> None:
    """Save the conversation that led to an order, for the shop's console.

    Everything the page showed: what the person asked, what the agent did and
    said, what it compared and proposed, the approval, the receipt.
    """
    if not self.run_dir:
      return
    folder = self.run_dir / "conversations"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{order_id}.json").write_text(
      json.dumps(
        {
          "order_id": order_id,
          "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "customer": self.customer,
          "brain": self.brain.name,
          "events": self.events,
        },
        ensure_ascii=False,
      )
    )

  def conversation_of(self, order_id: str) -> dict[str, Any] | None:
    """Return the saved conversation behind an order, if there is one."""
    if not self.run_dir:
      return None
    path = self.run_dir / "conversations" / f"{order_id}.json"
    if not path.is_file():
      return None
    return json.loads(path.read_text())

  def _note(self, text: str) -> None:
    """Tell the brain about something that happened outside it."""
    self.turns.append({"role": "event", "text": text})

  def note_external(self, text: str) -> None:
    """Record that another brain (over MCP) used the agent's tools."""
    self._note(text)
    self.emit({"type": "external", "text": text})

  def approvals(self) -> dict[str, list[dict[str, Any]]]:
    """Return the proposals still waiting, and the ones already decided."""
    waiting = [p for p in self.proposals.values() if p["status"] == "pending"]
    decided = [p for p in self.proposals.values() if p["status"] != "pending"]
    return {
      "pending": list(reversed(waiting)),
      "decided": list(reversed(decided))[:30],
    }

  def _record(self, event: str, proposal: dict[str, Any], **more: Any) -> None:
    """Write a line of the agent's record of what the person agreed to."""
    if not self.run_dir:
      return
    line = {
      "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
      "event": event,
      "by": self.customer["email"],
      # The channel the answer came through; overridden by `more`.
      "method": "page",
      "proposal_id": proposal["id"],
      "shop": proposal["shop"],
      "checkout_id": proposal["checkout_id"],
      "item": proposal["title"],
      "total": proposal["total"],
      "currency": proposal["currency"],
      "coins": proposal["coins"].get("applied", 0),
      "replaces": proposal.get("replaces"),
      **more,
    }
    self.run_dir.mkdir(parents=True, exist_ok=True)
    with (self.run_dir / "approvals.jsonl").open("a") as record:
      record.write(json.dumps(line) + "\n")
    # The proposal remembers how and when it was decided, for the page.
    if event in ("approved", "declined"):
      proposal["decided_via"] = line["method"]
      proposal["decided_at"] = line["at"]
      self._save_pending()
