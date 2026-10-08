"""Telegram as a channel of the agent: the same agent, on the person's phone.

One bot per agent (Telegram lets one reader poll a bot). The person links
their chat once from the agent's page: the page makes a code, the person
opens `t.me/<bot>?start=<code>`, and from then on only that chat may talk to
the agent. The link is kept in `telegram.json` in the run directory.

What travels:

- A message from the linked chat is a turn of the conversation, as if typed
  on the page. The agent's words come back as messages.
- Every proposal, wherever it was made (this chat, the page, ChatGPT or
  Claude over MCP), arrives as a card: photo, the terms, the total, and the
  buttons Approve / Not this one / Open in my agent. A tap is the person's
  decision, recorded with `method: telegram`; the card is edited to show
  what became of it. A receipt follows a purchase, with the voucher codes.

Long polling (getUpdates), so nothing needs a public webhook. The token
comes from TELEGRAM_BOT_TOKEN; without it this module does nothing.
"""

import asyncio
import contextlib
import datetime
import html
import json
import logging
import pathlib
import secrets
import time
from typing import Any

import httpx

log = logging.getLogger("uvicorn.error")

API = "https://api.telegram.org"
POLL_SECONDS = 25
# A link code is good for this long.
CODE_SECONDS = 15 * 60


def _money(cents: int) -> str:
  """Format cents as dollars, with a leading minus for discounts."""
  sign = "−" if cents < 0 else ""
  cents = abs(cents)
  return f"{sign}${cents // 100}" + (
    f".{cents % 100:02d}" if cents % 100 else ""
  )


def _when(iso: str | None) -> str:
  """Format an ISO time the way a person reads it: Sat 10 Oct, 10:00."""
  if not iso:
    return ""
  try:
    moment = datetime.datetime.fromisoformat(iso)
  except ValueError:
    return iso[:16].replace("T", " ")
  return moment.strftime("%a %-d %b, %H:%M")


class Telegram:
  """The bot: polls Telegram, talks to the session, keeps the link."""

  def __init__(
    self,
    token: str,
    session: Any,
    run_dir: pathlib.Path,
    page_url: str,
    http: httpx.AsyncClient | None = None,
  ) -> None:
    """Prepare the bot; `start()` begins polling."""
    self.token = token
    self.session = session
    self.run_dir = run_dir
    self.page_url = page_url.rstrip("/")
    self.http = http or httpx.AsyncClient(timeout=POLL_SECONDS + 10)
    self.username: str | None = None
    self.chat_id: int | None = None
    self.chat_name: str | None = None
    # Proposal id -> (chat id, message id) of the card we sent for it.
    self.cards: dict[str, tuple[int, int]] = {}
    self.code: tuple[str, float] | None = None
    self._task: asyncio.Task | None = None
    self._load()
    session.add_watcher(self._on_event)

  # ---- The link to the person's chat

  def _path(self) -> pathlib.Path:
    return self.run_dir / "telegram.json"

  def _load(self) -> None:
    if self._path().is_file():
      with contextlib.suppress(ValueError):
        data = json.loads(self._path().read_text())
        self.chat_id = data.get("chat_id")
        self.chat_name = data.get("chat_name")

  def _save(self) -> None:
    self.run_dir.mkdir(parents=True, exist_ok=True)
    self._path().write_text(
      json.dumps({"chat_id": self.chat_id, "chat_name": self.chat_name})
    )

  def link_code(self) -> str:
    """Make the one-time code the person sends the bot with /start."""
    code = secrets.token_urlsafe(9)
    self.code = (code, time.time() + CODE_SECONDS)
    return code

  def link_url(self) -> str | None:
    """Return the t.me address that links this chat, once there is a code."""
    if not self.username or not self.code:
      return None
    return f"https://t.me/{self.username}?start={self.code[0]}"

  def unlink(self) -> None:
    """Forget the chat."""
    self.chat_id = None
    self.chat_name = None
    self.cards.clear()
    self._save()

  def status(self) -> dict[str, Any]:
    """Return what the page shows about this channel."""
    return {
      "configured": bool(self.token),
      "bot": self.username,
      "linked": self.chat_id is not None,
      "chat_name": self.chat_name,
    }

  # ---- Telegram's API

  async def api(self, method: str, **params: Any) -> Any:
    """Call a Bot API method; return its result or None on failure."""
    try:
      response = await self.http.post(
        f"{API}/bot{self.token}/{method}", json=params
      )
      body = response.json()
    except (httpx.HTTPError, ValueError) as error:
      log.warning("telegram %s failed: %s", method, error)
      return None
    if not body.get("ok"):
      log.warning("telegram %s: %s", method, body.get("description"))
      return None
    return body.get("result")

  async def start(self) -> None:
    """Learn the bot's name and begin polling in the background."""
    me = await self.api("getMe")
    if me:
      self.username = me.get("username")
      log.info("telegram bot @%s ready", self.username)
    self._task = asyncio.create_task(self._poll())

  async def stop(self) -> None:
    """Stop polling."""
    if self._task:
      self._task.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await self._task

  async def _poll(self) -> None:
    offset = 0
    while True:
      updates = await self.api(
        "getUpdates",
        offset=offset,
        timeout=POLL_SECONDS,
        allowed_updates=["message", "callback_query"],
      )
      if updates is None:
        await asyncio.sleep(3)
        continue
      for update in updates:
        offset = update["update_id"] + 1
        try:
          await self.handle(update)
        except Exception:  # noqa: BLE001  A bad update must not stop the bot.
          log.exception("telegram update failed")

  # ---- What comes in

  async def handle(self, update: dict[str, Any]) -> None:
    """Act on one update: a message, or a tap on a card's button."""
    if "callback_query" in update:
      await self._on_tap(update["callback_query"])
      return
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    if not text or not chat:
      return
    if text.startswith("/start"):
      await self._on_start(chat, text)
      return
    if chat.get("id") != self.chat_id:
      await self.api(
        "sendMessage",
        chat_id=chat["id"],
        text=(
          "This agent belongs to someone else. Link your own from your"
          " agent's page."
        ),
      )
      return
    if text in ("/help", "/pending"):
      await self._on_help(text)
      return
    await self._on_message(text)

  async def _on_start(self, chat: dict[str, Any], text: str) -> None:
    given = text.split(maxsplit=1)[1].strip() if " " in text else ""
    if (
      self.code
      and given
      and secrets.compare_digest(given, self.code[0])
      and self.code[1] > time.time()
    ):
      self.chat_id = chat["id"]
      self.chat_name = chat.get("username") or chat.get("first_name")
      self.code = None
      self._save()
      await self.api(
        "sendMessage",
        chat_id=chat["id"],
        text=(
          "Linked. I'm your shopping agent. Tell me what you want and I'll"
          " search the shops and propose; you approve here, on the page or"
          " wherever the card shows. I never pay without your yes."
        ),
      )
      return
    if chat.get("id") == self.chat_id:
      await self.api(
        "sendMessage", chat_id=chat["id"], text="Already linked. Ask away."
      )
      return
    await self.api(
      "sendMessage",
      chat_id=chat["id"],
      text=(
        "To use this agent, open the link from its page (What it knows"
        " about you → Connections → Telegram)."
      ),
    )

  async def _on_help(self, text: str) -> None:
    waiting = self.session.approvals()["pending"]
    if text == "/pending" or waiting:
      if not waiting:
        await self.send("Nothing waits for your approval.")
      for proposal in waiting:
        await self.send_card(proposal)
      return
    await self.send(
      "Say what you want ('a spa day for two, refundable, under $100')."
      " /pending shows what waits for your approval."
    )

  async def _on_message(self, text: str) -> None:
    """Run a turn of the conversation from the phone; send the words back."""
    self.session.via = "Telegram"
    try:
      async for event in self.session.say(text):
        if event.get("type") == "agent" and event.get("text"):
          await self.send(event["text"])
    finally:
      self.session.via = None

  async def _on_tap(self, query: dict[str, Any]) -> None:
    data = query.get("data") or ""
    chat = (query.get("message") or {}).get("chat") or {}
    if chat.get("id") != self.chat_id:
      await self.api(
        "answerCallbackQuery", callback_query_id=query["id"], text="Not yours."
      )
      return
    action, _, proposal_id = data.partition(":")
    proposal = self.session.proposals.get(proposal_id)
    if not proposal or proposal["status"] != "pending":
      await self.api(
        "answerCallbackQuery",
        callback_query_id=query["id"],
        text="That proposal is no longer waiting.",
      )
      return
    await self.api(
      "answerCallbackQuery",
      callback_query_id=query["id"],
      text="Approving…" if action == "approve" else "Declined.",
    )
    stream = (
      self.session.approve(proposal_id, method="telegram")
      if action == "approve"
      else self.session.decline(proposal_id, method="telegram")
    )
    # What the agent says about it was said here, and the thread shows so.
    self.session.via = "Telegram"
    try:
      async for event in stream:
        if event.get("type") == "agent" and event.get("text"):
          await self.send(event["text"])
    finally:
      self.session.via = None

  # ---- What goes out

  async def send(self, text: str) -> None:
    """Send plain words to the linked chat."""
    if self.chat_id is None:
      return
    await self.api("sendMessage", chat_id=self.chat_id, text=text)

  def _caption(self, proposal: dict[str, Any], outcome: str | None) -> str:
    """Return the card's text, in Telegram's HTML."""
    e = html.escape
    service = proposal.get("service") or {}
    window = service.get("window") or {}
    cancel = proposal.get("cancellation") or {}
    lines = [
      f"<b>{e(proposal['title'])}</b>",
      e(proposal["shop_name"])
      + (f" · {e(service['merchant'])}" if service.get("merchant") else ""),
    ]
    if proposal.get("reason"):
      lines.append(e(proposal["reason"]))
    if window.get("not_before"):
      start, end = _when(window["not_before"]), _when(window.get("not_after"))
      lines.append(f"When: {e(start)} → {e(end)}")
    if cancel.get("refundability"):
      words = cancel["refundability"].replace("_", " ")
      until = cancel.get("refundable_until")
      lines.append(
        f"Cancellation: {e(words)}"
        + (f" until {e(_when(until))}" if until else "")
      )
    if proposal.get("quantity", 1) > 1:
      lines.append(f"Quantity: {proposal['quantity']}")
    lines.append("")
    for total in proposal.get("totals") or []:
      if total["type"] == "total":
        continue
      lines.append(f"{e(total['label'])}: {_money(total['amount'])}")
    lines.append(f"<b>Your card pays {_money(proposal['total'])}</b>")
    via = proposal.get("via")
    if via == "Telegram":
      lines.append("<i>Asked here, on Telegram</i>")
    elif via:
      lines.append(f"<i>Proposed by {e(via)}</i>")
    if outcome:
      lines.append("")
      lines.append(f"<b>{e(outcome)}</b>")
    return "\n".join(lines)

  def _keyboard(self, proposal: dict[str, Any]) -> dict[str, Any]:
    return {
      "inline_keyboard": [
        [
          {
            "text": f"Approve {_money(proposal['total'])}",
            "callback_data": f"approve:{proposal['id']}",
          },
          {
            "text": "Not this one",
            "callback_data": f"decline:{proposal['id']}",
          },
        ],
        [
          {
            "text": "Open in my agent",
            "url": f"{self.page_url}/approvals/{proposal['id']}",
          }
        ],
      ]
    }

  async def send_card(self, proposal: dict[str, Any]) -> None:
    """Put a proposal in the chat, with its photo and its buttons."""
    if self.chat_id is None:
      return
    caption = self._caption(proposal, None)
    keyboard = self._keyboard(proposal)
    sent = None
    if proposal.get("image"):
      sent = await self.api(
        "sendPhoto",
        chat_id=self.chat_id,
        photo=proposal["image"],
        caption=caption,
        parse_mode="HTML",
        reply_markup=keyboard,
      )
    if not sent:
      sent = await self.api(
        "sendMessage",
        chat_id=self.chat_id,
        text=caption,
        parse_mode="HTML",
        reply_markup=keyboard,
      )
    if sent:
      self.cards[proposal["id"]] = (self.chat_id, sent["message_id"])

  OUTCOMES = {
    "approved": "Approved. Paying the shop…",
    "bought": "Bought, at the total you approved.",
    "declined": "You turned this down. Nothing was paid.",
    "withdrawn": "Replaced by a newer proposal. Nothing was paid.",
    "changed": (
      "The shop changed the total after you approved. Nothing was paid."
    ),
    "failed": "The shop refused the purchase. Nothing was paid.",
  }

  async def update_card(self, proposal_id: str, status: str) -> None:
    """Edit the card to say what became of the proposal; drop the buttons."""
    card = self.cards.get(proposal_id)
    proposal = self.session.proposals.get(proposal_id)
    if not card or not proposal:
      return
    chat_id, message_id = card
    caption = self._caption(proposal, self.OUTCOMES.get(status, status))
    with_photo = bool(proposal.get("image"))
    method = "editMessageCaption" if with_photo else "editMessageText"
    key = "caption" if with_photo else "text"
    await self.api(
      method,
      chat_id=chat_id,
      message_id=message_id,
      parse_mode="HTML",
      reply_markup={"inline_keyboard": []},
      **{key: caption},
    )

  async def send_receipt(self, receipt: dict[str, Any]) -> None:
    """Tell the chat what was bought, with the voucher codes."""
    e = html.escape
    codes = " ".join(f"<code>{e(c)}</code>" for c in receipt.get("codes") or [])
    await self.api(
      "sendMessage",
      chat_id=self.chat_id,
      parse_mode="HTML",
      text=(
        f"<b>Bought: {e(receipt['title'])}</b> at {e(receipt['shop_name'])}\n"
        f"Charged {_money(receipt.get('charged') or 0)}"
        + (f" + {receipt['coins']} coins" if receipt.get("coins") else "")
        + f"\nVoucher: {codes}\n"
        f"Receipt: {self.page_url}/receipts/{receipt['order_id']}"
      ),
    )

  # ---- The session's events, whichever channel caused them

  def _on_event(self, event: dict[str, Any]) -> None:
    if self.chat_id is None:
      return
    kind = event.get("type")
    if kind == "proposal":
      asyncio.create_task(self.send_card(event["proposal"]))
    elif kind == "proposal_status" and event["status"] != "pending":
      asyncio.create_task(self.update_card(event["id"], event["status"]))
    elif kind == "receipt":
      asyncio.create_task(self.send_receipt(event["receipt"]))
