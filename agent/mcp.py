"""The agent as a tool server for another brain: MCP over HTTP.

A person's ChatGPT, Claude Code or any other assistant that speaks the Model
Context Protocol connects here and gets the agent's tools: search the shops,
read the wallets, recall and remember, propose a purchase. The external brain
thinks; this agent signs, keeps the memory and the rules, and puts the
proposal in front of the person, who approves on the agent's own page.
There is no tool that pays, here or anywhere else.

The endpoint lives at `/mcp/{key}`: the key is a secret in the address, so
only the agent's person can connect their assistant to it.

Only the part of MCP a tool server needs: JSON-RPC 2.0 over POST, the
`initialize`, `ping`, `tools/list`, `tools/call`, `resources/list` and
`resources/read` methods, and `search` and `fetch` as ChatGPT's connectors
expect them.

The proposal also comes with a card ChatGPT can draw inside the chat (the
MCP Apps / OpenAI Apps SDK way): `propose_purchase` names a `ui://` resource,
and `widget.html` is that card. Its Approve button calls `approve_from_card`
with a one-time token the result carries in `_meta`, which reaches the card
but not the model. So the person still approves on the agent's own
interface, only embedded; the model has no way to approve, and the record
says the yes came through the card.
"""

import hashlib
import hmac
import json
import logging
import pathlib
import secrets
import time
from typing import Any

from brain import Call
from session import Session

# Uvicorn's logger, so the lines show up with the server's own.
log = logging.getLogger("uvicorn.error")

# The newest protocol this server knows; a client proposing an older one
# gets that one back (this server uses nothing version-specific).
PROTOCOL_VERSION = "2025-11-25"
UI_EXTENSION = "io.modelcontextprotocol/ui"
SERVER_NAME = "shopping-agent"
CARD_MIME = "text/html;profile=mcp-app"
CARD_HTML = pathlib.Path(__file__).with_name("widget.html")


def _card_uri() -> str:
  """Return the card's URI, versioned by its content.

  Hosts cache a ui:// resource once fetched; a new address per version
  makes them fetch the card again after a deploy.
  """
  digest = hashlib.sha256(CARD_HTML.read_bytes()).hexdigest()[:10]
  return f"ui://shopping-agent/proposal-{digest}.html"


CARD_URI = _card_uri()
CARD_URI_PREFIX = "ui://shopping-agent/proposal"
# Where the card loads the MCP Apps client from, in hosts that speak it.
SDK_ORIGIN = "https://unpkg.com"
# A token for the card's Approve button lives this long; a proposal that
# is decided stops it earlier.
TOKEN_SECONDS = 24 * 3600
# The tools only the card may call, never the model.
CARD_TOOLS = ("approve_from_card", "decline_from_card")
# The tools whose result is a proposal waiting for the person: a purchase,
# or a change to one. Each gets the card and its one-time token.
PROPOSING_TOOLS = ("propose_purchase", "reschedule_purchase", "cancel_purchase")


def load_key(path) -> str:
  """Return the secret in the MCP address, making one on the first run."""
  if path.is_file():
    return path.read_text().strip()
  key = secrets.token_urlsafe(24)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(key)
  path.chmod(0o600)
  return key


def _card_binding() -> dict[str, Any]:
  """Return the _meta that ties a tool's result to the proposal card."""
  return {
    "ui": {"resourceUri": CARD_URI},
    # The older flat key some hosts still read.
    "ui/resourceUri": CARD_URI,
    "openai/outputTemplate": CARD_URI,
    "openai/toolInvocation/invoking": "Opening a checkout at the shop…",
    "openai/toolInvocation/invoked": "Proposal ready. It waits for your yes.",
  }


def _card_only() -> dict[str, Any]:
  """Return the _meta of a tool the card may call but the model may not."""
  return {
    "ui": {"visibility": ["app"]},
    "openai/widgetAccessible": True,
  }


def tool_specs(session: Session) -> list[dict[str, Any]]:
  """Describe the agent's tools the way MCP clients expect."""
  specs = [
    {
      "name": tool.name,
      "description": tool.description,
      "inputSchema": tool.parameters,
      **({"_meta": _card_binding()} if tool.name in PROPOSING_TOOLS else {}),
    }
    for tool in session.tools
  ]
  # The card's own buttons. They need the token the card was handed; the
  # model never sees it, so these do nothing for the model.
  for name, what in (
    ("approve_from_card", "Approve a proposal from the agent's own card."),
    ("decline_from_card", "Decline a proposal from the agent's own card."),
  ):
    specs.append(
      {
        "name": name,
        "description": (
          f"{what} Only the card can use it: it needs the card's token."
        ),
        "inputSchema": {
          "type": "object",
          "properties": {
            "proposal_id": {"type": "string"},
            "token": {"type": "string"},
            "host": {
              "type": "string",
              "description": "The app the card is shown in.",
            },
          },
          "required": ["proposal_id", "token"],
        },
        "_meta": _card_only(),
        "annotations": {
          "title": what.split(" from")[0],
          "readOnlyHint": False,
          "destructiveHint": False,
          "openWorldHint": True,
        },
      }
    )
  # ChatGPT's connectors look for `search` and `fetch`; they map onto the
  # catalog: search finds deals, fetch returns one deal in full.
  specs += [
    {
      "name": "search",
      "description": (
        "Search the shops for deals that match a few words. Returns a list"
        " of results with ids; use fetch for the full deal."
      ),
      "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
      },
      "annotations": {
        "title": "Search",
        "readOnlyHint": True,
        "openWorldHint": True,
      },
    },
    {
      "name": "fetch",
      "description": (
        "Return one deal in full by the id a search gave (shop:option)."
      ),
      "inputSchema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
      },
      "annotations": {
        "title": "Fetch a deal",
        "readOnlyHint": True,
        "openWorldHint": True,
      },
    },
  ]
  return specs


def _signing_key(session: Session) -> bytes:
  """Return the key the card tokens are signed with: the agent's MCP key."""
  key = getattr(session, "mcp_key", None) or ""
  if not key and session.run_dir and (session.run_dir / "mcp_key").is_file():
    key = (session.run_dir / "mcp_key").read_text().strip()
  return (key or "no-key").encode()


def issue_token(session: Session, proposal_id: str) -> str:
  """Make the token the card's buttons need: signed, tied to the proposal.

  It needs no memory on the server, so a restart doesn't void it; it stops
  working when the proposal is decided (the tools check the status) or
  after TOKEN_SECONDS.
  """
  expiry = str(int(time.time()) + TOKEN_SECONDS)
  signature = hmac.new(
    _signing_key(session), f"{proposal_id}|{expiry}".encode(), "sha256"
  ).hexdigest()[:32]
  return f"{expiry}.{signature}"


def token_ok(session: Session, proposal_id: str, token: str) -> bool:
  """Whether `token` was issued for that proposal and is still in time."""
  expiry, _, signature = (token or "").partition(".")
  if not expiry.isdigit() or int(expiry) < time.time():
    return False
  expected = hmac.new(
    _signing_key(session), f"{proposal_id}|{expiry}".encode(), "sha256"
  ).hexdigest()[:32]
  return hmac.compare_digest(expected, signature)


def card_view(session: Session, proposal_id: str) -> dict[str, Any]:
  """Return what the card shows once something happened to a proposal."""
  proposal = session.proposals.get(proposal_id) or {}
  receipt = next(
    (
      e["receipt"]
      for e in reversed(session.events)
      if e.get("type") == "receipt"
      and e["receipt"].get("proposal_id") == proposal_id
    ),
    None,
  )
  words = next(
    (e["text"] for e in reversed(session.events) if e.get("type") == "agent"),
    "",
  )
  return {
    "proposal_id": proposal_id,
    "status": proposal.get("status", "unknown"),
    "title": proposal.get("title"),
    "shop_name": proposal.get("shop_name"),
    "total": proposal.get("total"),
    "receipt": receipt,
    "agent_said": words,
  }


async def card_action(
  session: Session, name: str, args: dict[str, Any]
) -> dict[str, Any]:
  """Approve or decline from the card, once the token checks out."""
  proposal_id = str(args.get("proposal_id") or "")
  proposal = session.proposals.get(proposal_id)
  if not proposal or proposal["status"] != "pending":
    return _text(
      {
        "error": "not_open",
        "message": "That proposal is no longer waiting: it was decided,"
        " replaced or is unknown here.",
        **card_view(session, proposal_id),
      },
      error=True,
    )
  if not token_ok(session, proposal_id, str(args.get("token") or "")):
    return _text(
      {
        "error": "not_allowed",
        "message": (
          "Only the person, from the agent's card, can decide this. The"
          " token is missing, wrong or expired."
        ),
      },
      error=True,
    )
  # The record says which assistant's card the yes came through. The card
  # itself knows its host best (web or desktop); else the MCP client's name.
  host = str(args.get("host") or "").strip()[:40]
  client = host or getattr(session, "external_client", None) or "assistant"
  method = f"card:{client}"
  stream = (
    session.approve(proposal_id, method=method)
    if name == "approve_from_card"
    else session.decline(proposal_id, method=method)
  )
  async for _ in stream:
    pass
  return _text(card_view(session, proposal_id))


async def call_tool(
  session: Session, name: str, args: dict[str, Any], page_url: str
) -> dict[str, Any]:
  """Run a tool for the external brain and shape the answer for MCP."""
  if name in CARD_TOOLS:
    return await card_action(session, name, args)
  if name == "search":
    found = await session.search_deals(query=args.get("query") or None)
    results = [
      {
        "id": f"{deal['shop']}:{option['id']}",
        "title": f"{deal['title']} · {option['label']} · {deal['shop_name']}",
        "url": f"{page_url}/",
      }
      for deal in found["deals"]
      for option in deal["options"]
      if option["available"]
    ]
    return _text({"results": results})
  if name == "fetch":
    shop_id, _, option_id = (args.get("id") or "").partition(":")
    deal = session._seen.get((shop_id, option_id))  # noqa: SLF001
    if not deal:
      found = await session.search_deals()
      deal = next(
        (
          d
          for d in found["deals"]
          if any(o["id"] == option_id for o in d["options"])
          and d["shop"] == shop_id
        ),
        None,
      )
    if not deal:
      return _text(
        {"error": "not_found", "message": f"No deal {args.get('id')}"},
        error=True,
      )
    return _text(
      {
        "id": args["id"],
        "title": deal["title"],
        "text": json.dumps(deal, ensure_ascii=False),
        "url": f"{page_url}/",
        "metadata": {"shop": deal["shop_name"]},
      }
    )

  result = await session._run(Call(name, args))  # noqa: SLF001
  # Tell the brain where the person approves: on the card, or on the page.
  meta = None
  if (
    name in PROPOSING_TOOLS
    and isinstance(result, dict)
    and not result.get("error")
    and result.get("proposal_id")
  ):
    result["page_url"] = f"{page_url}/approvals/{result['proposal_id']}"
    result["approval"] = (
      "Waiting for the customer to approve, on the card shown with this"
      f" result or on their agent page: {result['page_url']}."
      " You cannot approve or pay; only they can."
    )
    # The card's token travels in _meta: the card gets it, the model doesn't.
    # So does the address where the card can read the proposal's state.
    token = issue_token(session, result["proposal_id"])
    meta = {
      "card_token": token,
      "status_url": f"{page_url}/card/{result['proposal_id']}?t={token}",
    }
  is_error = isinstance(result, dict) and bool(result.get("error"))
  return _text(result, error=is_error, meta=meta)


def _text(
  payload: Any, error: bool = False, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
  body = {
    "content": [
      {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
    ],
  }
  if isinstance(payload, dict):
    body["structuredContent"] = payload
  if error:
    body["isError"] = True
  if meta:
    body["_meta"] = meta
  return body


def card_resource(session: Session) -> dict[str, Any]:
  """Describe the proposal card as an MCP resource."""
  hosts = sorted(
    {
      shop.public_url.split("//", 1)[-1].split("/", 1)[0]
      for shop in session.shops.values()
      if shop.public_url
    }
  )
  # The shops (photos) and the MCP Apps client the card loads in Claude.
  domains = [f"https://{host}" for host in hosts] + [SDK_ORIGIN]
  # Where the card may connect: the agent itself, to read a proposal's state.
  agent = getattr(session, "public_url", "") or ""
  connect = [agent] if agent.startswith("https://") else []
  return {
    "uri": CARD_URI,
    "name": "Proposal card",
    "description": "A purchase the agent proposes, with Approve and Decline.",
    "mimeType": CARD_MIME,
    "_meta": {
      "ui": {
        "prefersBorder": True,
        "csp": {"resourceDomains": domains, "connectDomains": connect},
      },
      "openai/widgetPrefersBorder": True,
      "openai/widgetCSP": {
        "resource_domains": domains,
        "connect_domains": connect,
      },
      "openai/widgetDescription": (
        "The agent's proposal: photo, terms, the total, and the person's"
        " Approve and Decline."
      ),
    },
  }


async def handle(
  session: Session, message: dict[str, Any], page_url: str
) -> dict[str, Any] | None:
  """Answer one JSON-RPC message. None means a notification: no reply."""
  method = message.get("method")
  request_id = message.get("id")
  # Which methods the client uses says what it understands (resources,
  # the card...): one line each, for the server's log.
  log.info(
    "mcp %s %s",
    method,
    (message.get("params") or {}).get("name")
    or (message.get("params") or {}).get("uri")
    or "",
  )
  if request_id is None:
    # A notification (initialized, cancelled...): nothing to say back.
    return None
  params = message.get("params") or {}

  def result(value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}

  def error(code: int, text: str) -> dict[str, Any]:
    return {
      "jsonrpc": "2.0",
      "id": request_id,
      "error": {"code": code, "message": text},
    }

  if method == "initialize":
    # Remember who is on the other end, to stamp its proposals with.
    info = params.get("clientInfo") or {}
    client = info.get("name")
    log.info("mcp client %s %s", client, info.get("version"))
    session.external_client = client or "another assistant"
    asked = str(params.get("protocolVersion") or PROTOCOL_VERSION)
    return result(
      {
        # Speak the client's version when it is at least as new as ours;
        # a host may gate its features (the card) on what the server speaks.
        "protocolVersion": asked
        if asked >= PROTOCOL_VERSION
        else PROTOCOL_VERSION,
        "capabilities": {
          "tools": {"listChanged": False},
          "resources": {"listChanged": False},
          # MCP Apps: the card is HTML of this type.
          "extensions": {UI_EXTENSION: {"mimeTypes": [CARD_MIME]}},
        },
        "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
        "instructions": (
          "You are talking to a person's shopping agent. Search the shops,"
          " read the wallets and what the agent knows about the person, and"
          " propose one purchase with propose_purchase. The person approves"
          " on the card shown with the proposal, or on the agent's page;"
          " you cannot pay."
        ),
      }
    )
  if method == "ping":
    return result({})
  if method == "tools/list":
    return result({"tools": tool_specs(session)})
  if method == "resources/list":
    return result({"resources": [card_resource(session)]})
  if method == "resources/templates/list":
    return result({"resourceTemplates": []})
  if method == "resources/read":
    # Any version of the card's address serves the current card.
    if not str(params.get("uri") or "").startswith(CARD_URI_PREFIX):
      return error(-32002, f"Unknown resource {params.get('uri')}")
    return result(
      {
        "contents": [
          {
            "uri": params["uri"],
            "mimeType": CARD_MIME,
            "text": CARD_HTML.read_text(encoding="utf-8"),
            "_meta": card_resource(session)["_meta"],
          }
        ]
      }
    )
  if method == "tools/call":
    name = params.get("name")
    args = params.get("arguments") or {}
    known = {spec["name"] for spec in tool_specs(session)}
    if name not in known:
      return error(-32602, f"Unknown tool {name}")
    client = getattr(session, "external_client", None) or "another assistant"
    # Where the person is: the card names its host (ChatGPT, Claude
    # Desktop); a tool call names the connector. Set before the note, so
    # the note itself carries it.
    host = args.get("host") if name in CARD_TOOLS else None
    session.via = (host if isinstance(host, str) and host else None) or client
    try:
      if name in CARD_TOOLS:
        session.note_external(
          f"The customer answered from the card in {session.via}: {name}."
        )
      else:
        session.note_external(f"{client} (over MCP) called {name}.")
      return result(await call_tool(session, name, args, page_url))
    finally:
      session.via = None
  return error(-32601, f"Method not found: {method}")
