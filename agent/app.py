"""The agent's web app: the page a person talks to their agent on.

  GET  /                             the page
  GET  /profile.json                 the agent's public profile, for shops
  POST /mcp/{key}                    the agent's tools for another brain (MCP)
  GET  /api/state                    who the agent is and the conversation
  GET  /evidence/{order}             what the agent and the shop recorded
  GET  /receipts/{order}             a printable receipt
  GET  /approvals, /approvals/{id}   the page, open on what waits for approval
  GET  /api/approvals                proposals waiting, and the decided ones
  GET  /api/history                  every conversation and decision, from
                                     the agent's journal
  GET  /api/telegram, POST /link, /unlink   the Telegram channel's state
  GET  /api/wallets                  the person's coins in every shop
  GET  /api/purchases                everything bought, as the shops see it
  GET  /api/memory, PUT, POST /forget  what the agent knows about its person
  GET  /api/conversations/{order}    the conversation that led to an order
  POST /api/messages                 the person writes; the agent answers
  POST /api/proposals/{id}/approve   the person approves a purchase
  POST /api/proposals/{id}/decline   the person turns it down
  POST /api/reset                    start a new conversation

The three POSTs that make the agent work answer with a stream of events, one
JSON object per line, as they happen.

Run it with `scripts/agent.sh start`, or `uv run uvicorn app:app --port 8190`.
With OPENAI_API_KEY set, a language model decides for the agent
(model_brain.py); AGENT_MODEL names it and OPENAI_BASE_URL moves it to another
provider. AGENT_BRAIN=scripted keeps the scripted stand-in even with a key.
The person signs in on /login (teamlogin.py) when LOGIN_USERS or AGENT_USER +
AGENT_PASSWORD_HASH are set; otherwise the page runs open.
TELEGRAM_BOT_TOKEN (from BotFather) gives the agent a Telegram bot: the
person links their chat from the page and talks to the agent there too.
AGENT_URL is the address shops reach the agent at (to fetch its profile);
AGENT_CONFIG moves its settings, and AGENT_RUN_DIR where it keeps its key
and its record of approvals. AGENT_NAME, AGENT_CUSTOMER_NAME and
AGENT_CUSTOMER_EMAIL say whose agent this is; AGENT_CARD_HANDLER,
AGENT_CARD_TOKEN and AGENT_CARD_LABEL which card it pays with. SHOP_<ID>_URL and
SHOP_<ID>_PUBLIC_URL override a shop's addresses: the one the agent calls and
the one people open.
"""

from collections.abc import AsyncIterator
import contextlib
import json
import os
import uuid
import pathlib
import time
import re
import secrets
from typing import Annotated, Any

from brain import Brain, ScriptedBrain
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (
  HTMLResponse,
  JSONResponse,
  Response,
  StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
import httpx
import mcp
import model_brain
import teamlogin
import telegram
from session import Session
from shops import Shop
import signing

HERE = pathlib.Path(__file__).parent
AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8190").rstrip("/")
# A token for this run of the process; the page notices a restart by it.
BOOT = uuid.uuid4().hex[:8]
CONFIG = pathlib.Path(os.environ.get("AGENT_CONFIG", HERE / "agent.json"))
RUN_DIR = pathlib.Path(
  os.environ.get("AGENT_RUN_DIR", HERE.parent / ".run" / "agent")
)
UCP_VERSION = "2026-04-08"
KEY_ID = "shopping-agent"
TIMEOUT_SECONDS = 15


# Static files are referenced with a version, so a browser doesn't keep an old
# script or stylesheet after a deploy.
_VERSION = str(int(time.time()))


def page_html(name: str) -> HTMLResponse:
  """Serve a page from static/, stamping its asset URLs with the version."""
  html = (HERE / "static" / name).read_text(encoding="utf-8")
  html = re.sub(r'(/static/[\w./-]+\.(?:css|js))"', rf'\1?v={_VERSION}"', html)
  # The page itself is never cached: a browser must see each deploy's page.
  return HTMLResponse(
    html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
  )


def choose_brain(settings: dict[str, Any]) -> Brain:
  """Return a model-backed brain if there is a key for one, else the script."""
  key = os.environ.get("OPENAI_API_KEY")
  if not key or os.environ.get("AGENT_BRAIN") == "scripted":
    return ScriptedBrain()
  return model_brain.ModelBrain(
    key,
    settings["customer"]["full_name"],
    os.environ.get("AGENT_MODEL", model_brain.DEFAULT_MODEL),
    os.environ.get("OPENAI_BASE_URL", model_brain.DEFAULT_BASE_URL),
    reasoning=os.environ.get("AGENT_REASONING", "low") or None,
  )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
  """Load the agent's key and open its conversation."""
  settings = json.loads(CONFIG.read_text())
  # AGENT_NAME, AGENT_CUSTOMER_NAME and AGENT_CUSTOMER_EMAIL override
  # agent.json, so the same image runs one agent per person.
  settings["name"] = os.environ.get("AGENT_NAME", settings["name"])
  customer = settings["customer"]
  customer["full_name"] = os.environ.get(
    "AGENT_CUSTOMER_NAME", customer["full_name"]
  )
  customer["email"] = os.environ.get("AGENT_CUSTOMER_EMAIL", customer["email"])
  # The card the agent pays with: the shops' payment handler and its token.
  # AGENT_CARD_HANDLER=stripe with AGENT_CARD_TOKEN=pm_card_visa pays through
  # Stripe's test card when the shops run on the stripe rail.
  card = settings["card"]
  card["handler"] = os.environ.get("AGENT_CARD_HANDLER", card["handler"])
  card["token"] = os.environ.get("AGENT_CARD_TOKEN", card["token"])
  card["label"] = os.environ.get("AGENT_CARD_LABEL", card["label"])
  for shop in settings["shops"]:
    # SHOP_A_URL and SHOP_A_PUBLIC_URL override agent.json, for containers.
    key = shop["id"].upper()
    shop["url"] = os.environ.get(f"SHOP_{key}_URL", shop["url"])
    shop["public_url"] = os.environ.get(
      f"SHOP_{key}_PUBLIC_URL", shop.get("public_url")
    )
  signer, public_key = signing.load_identity(RUN_DIR / "key.pem", KEY_ID)
  async with httpx.AsyncClient(auth=signer, timeout=TIMEOUT_SECONDS) as http:
    shops = [
      Shop(http=http, agent_profile=f"{AGENT_URL}/profile.json", **shop)
      for shop in settings["shops"]
    ]
    app.state.settings = settings
    app.state.public_key = public_key
    brain = choose_brain(settings)
    app.state.session = Session(settings, shops, brain, RUN_DIR)
    app.state.mcp_key = os.environ.get("AGENT_MCP_KEY") or mcp.load_key(
      RUN_DIR / "mcp_key"
    )
    # Telegram, when the agent has a bot: TELEGRAM_BOT_TOKEN from BotFather.
    app.state.telegram = None
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
      app.state.telegram = telegram.Telegram(
        os.environ["TELEGRAM_BOT_TOKEN"],
        app.state.session,
        RUN_DIR,
        AGENT_URL,
      )
      await app.state.telegram.start()
    # The card's approval tokens are signed with it; the card reads the
    # proposal's state from the agent's public address.
    app.state.session.mcp_key = app.state.mcp_key
    app.state.session.public_url = AGENT_URL
    yield
    if app.state.telegram:
      await app.state.telegram.stop()
    if isinstance(brain, model_brain.ModelBrain):
      await brain.aclose()


app = FastAPI(title="Shopping agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
# The person signs in (LOGIN_USERS, or AGENT_USER + AGENT_PASSWORD_HASH). The
# profile stays public (shops fetch it) and so does the MCP address (its
# secret is in the path).
teamlogin.install(
  app,
  os.environ.get("AGENT_NAME", "Shopping agent"),
  RUN_DIR,
  open_paths=("/profile.json", "/mcp/", "/card/"),
)


def _lines(events: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
  async def lines():
    async for event in events:
      yield json.dumps(event) + "\n"

  return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/", include_in_schema=False)
async def page() -> HTMLResponse:
  """Serve the page."""
  return page_html("index.html")


@app.get("/guides/{name}", include_in_schema=False)
async def guide_page(name: str) -> HTMLResponse:
  """Serve the page open on one of its guides (telegram, chatgpt, claude…)."""
  del name
  return page_html("index.html")


@app.get("/approvals", include_in_schema=False)
@app.get("/approvals/{proposal_id}", include_in_schema=False)
async def approvals_page(proposal_id: str = "") -> HTMLResponse:
  """Serve the page open on the approvals; the script reads the id."""
  del proposal_id
  return page_html("index.html")


@app.get("/api/approvals")
async def approvals() -> dict[str, Any]:
  """Return what waits for the person's approval, and what they decided."""
  return app.state.session.approvals()


@app.get("/api/history")
async def history() -> dict[str, Any]:
  """Return the journal: conversations with their messages, and decisions."""
  return app.state.session.history()


@app.get("/evidence/{order_id}", include_in_schema=False)
async def evidence_page(order_id: str) -> HTMLResponse:
  """Serve the page that lays out the evidence behind an order."""
  del order_id  # The page reads it from its address.
  return page_html("evidence.html")


@app.get("/api/memory")
async def read_memory() -> dict[str, Any]:
  """Return what the agent knows about its person."""
  session: Session = app.state.session
  return {**session.memory.as_dict(), "history": session.history_words()}


@app.put("/api/memory")
async def write_memory(
  profile: Annotated[dict[str, Any] | None, Body(embed=True)] = None,
  rules: Annotated[dict[str, Any] | None, Body(embed=True)] = None,
  presentation: Annotated[dict[str, Any] | None, Body(embed=True)] = None,
) -> dict[str, Any]:
  """Set the person's profile, hard rules and presentation, as typed."""
  session: Session = app.state.session
  try:
    session.memory.update(profile, rules, presentation)
  except (TypeError, ValueError) as error:
    raise HTTPException(status_code=400, detail=str(error)) from error
  return session.memory.as_dict()


@app.post("/api/memory/forget")
async def forget(fact: Annotated[str, Body(embed=True)]) -> dict[str, Any]:
  """Drop a preference the person no longer wants kept."""
  session: Session = app.state.session
  session.memory.forget(fact)
  return session.memory.as_dict()


@app.get("/api/telegram")
async def telegram_status() -> dict[str, Any]:
  """Say whether this agent has a bot and whether a chat is linked."""
  bot = app.state.telegram
  if not bot:
    return {"configured": False, "bot": None, "linked": False}
  return bot.status()


@app.post("/api/telegram/link")
async def telegram_link() -> dict[str, Any]:
  """Make a one-time link the person opens in Telegram to pair their chat."""
  bot = app.state.telegram
  if not bot:
    raise HTTPException(status_code=404, detail="This agent has no bot")
  bot.link_code()
  return {"url": bot.link_url(), "bot": bot.username}


@app.post("/api/telegram/unlink")
async def telegram_unlink() -> dict[str, Any]:
  """Forget the linked chat."""
  bot = app.state.telegram
  if bot:
    bot.unlink()
  return {"linked": False}


@app.get("/api/wallets")
async def wallets() -> list[dict[str, Any]]:
  """Return the person's coins in every shop, for the page's sidebar."""
  return await app.state.session.read_wallets(quiet=True)


@app.get("/api/purchases")
async def purchases() -> list[dict[str, Any]]:
  """Return everything this agent bought, as the shops see it today."""
  return await app.state.session.purchases()


@app.get("/api/purchases/{order_id}")
async def purchase(order_id: str) -> dict[str, Any]:
  """Return one purchase, for its receipt."""
  found = await app.state.session.purchase(order_id)
  if not found:
    raise HTTPException(status_code=404, detail="Not a purchase of this agent")
  return found


@app.get("/receipts/{order_id}", include_in_schema=False)
async def receipt_page(order_id: str) -> HTMLResponse:
  """Serve the printable receipt page."""
  del order_id  # The page reads it from its address.
  return page_html("receipt.html")


@app.get("/api/evidence/{order_id}")
async def evidence(order_id: str) -> dict[str, Any]:
  """Return the agent's record and the shop's order for one purchase."""
  return await app.state.session.evidence(order_id)


@app.get("/api/conversations/{order_id}")
async def conversation(order_id: str) -> dict[str, Any]:
  """Return the conversation that led to an order, for the shop's console."""
  found = app.state.session.conversation_of(order_id)
  if not found:
    raise HTTPException(
      status_code=404, detail="No conversation for that order"
    )
  return found


@app.post("/mcp/{key}")
async def mcp_endpoint(key: str, request: Request) -> Response:
  """Serve the agent's tools to another brain over MCP (JSON-RPC over HTTP)."""
  if not secrets.compare_digest(key, app.state.mcp_key):
    raise HTTPException(status_code=404, detail="Not found")
  try:
    message = await request.json()
  except ValueError as error:
    raise HTTPException(status_code=400, detail="Not JSON") from error
  messages = message if isinstance(message, list) else [message]
  replies = [
    reply
    for m in messages
    if (reply := await mcp.handle(app.state.session, m, AGENT_URL)) is not None
  ]
  if not replies:
    return Response(status_code=202)
  return JSONResponse(replies[0] if not isinstance(message, list) else replies)


@app.get("/mcp/{key}")
async def mcp_no_stream(key: str) -> Response:
  """MCP clients may open a stream here; this server has nothing to push."""
  if not secrets.compare_digest(key, app.state.mcp_key):
    raise HTTPException(status_code=404, detail="Not found")
  return Response(status_code=405)


@app.get("/card/{proposal_id}")
async def card_state(proposal_id: str, t: str = "") -> Response:
  """Return a proposal's state for the card inside ChatGPT or Claude.

  No sign-in: the card lives in another site's frame. The token the card
  was handed (signed, tied to the proposal) is the key; the model never
  sees it. CORS is open because the card's origin is the host's sandbox.
  """
  session: Session = app.state.session
  if not mcp.token_ok(session, proposal_id, t):
    raise HTTPException(status_code=404, detail="Not found")
  return JSONResponse(
    mcp.card_view(session, proposal_id),
    headers={
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "no-store",
    },
  )


@app.get("/profile.json")
async def profile() -> dict[str, Any]:
  """Publish the key shops check this agent's signatures against."""
  return {"ucp": {"version": UCP_VERSION, "keys": [app.state.public_key]}}


@app.get("/api/state")
async def state() -> dict[str, Any]:
  """Return who the agent is, the shops it knows and the conversation."""
  session: Session = app.state.session
  return {
    "name": app.state.settings["name"],
    # Changes when the agent restarts: event numbers start over, and an
    # open page draws its thread again.
    "boot": BOOT,
    "brain": {"name": session.brain.name, "is_model": session.brain.is_model},
    "customer": session.customer,
    "card": session.card["label"],
    "memory": session.memory.as_dict(),
    # Where another brain (ChatGPT, Claude Code) connects to this agent.
    "mcp_url": f"{AGENT_URL}/mcp/{app.state.mcp_key}",
    "pending_approvals": len(session.approvals()["pending"]),
    "shops": [
      {
        "id": shop.id,
        "name": shop.name,
        "url": shop.public_url,
        "color": shop.color,
        "logo": f"{shop.public_url}/logo.svg",
      }
      for shop in session.shops.values()
    ],
    "events": session.events,
  }


@app.post("/api/messages")
async def message(
  text: Annotated[str, Body(embed=True, min_length=1, max_length=2000)],
) -> StreamingResponse:
  """Take what the person wrote; stream what the agent does about it."""
  return _lines(app.state.session.say(text.strip()))


@app.post("/api/proposals/{proposal_id}/approve")
async def approve(proposal_id: str) -> StreamingResponse:
  """Buy a proposal the person approved, at the total they saw."""
  return _lines(app.state.session.approve(proposal_id))


@app.post("/api/proposals/{proposal_id}/switch")
async def switch(
  proposal_id: str,
  shop: Annotated[str, Body(embed=True)],
  option_id: Annotated[str, Body(embed=True)],
) -> StreamingResponse:
  """Switch the proposal to another option the person picked."""
  return _lines(app.state.session.switch(proposal_id, shop, option_id))


@app.post("/api/proposals/{proposal_id}/decline")
async def decline(proposal_id: str) -> StreamingResponse:
  """Drop a proposal the person turned down."""
  return _lines(app.state.session.decline(proposal_id))


@app.post("/api/reset")
async def reset() -> dict[str, bool]:
  """Start a new episode for the brain. The thread the person sees goes on."""
  app.state.session.reset()
  return {"ok": True}


@app.get("/api/thread")
async def thread(before: int | None = None, limit: int = 60) -> dict[str, Any]:
  """Return a page of the thread, oldest first; `before` pages back."""
  return app.state.session.thread(before, max(1, min(limit, 200)))
