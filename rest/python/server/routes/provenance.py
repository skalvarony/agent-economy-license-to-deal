"""Who bought an order and what the shop saw, for the customer's own pages.

An order placed on the web was placed by the person; one placed over UCP was
placed by their AI agent. For the agent's orders the shop also knows what the
agent told it at payment time (`agent_context`: who proposed, through which
channel the yes came, where the agent keeps its records) and what its own
ledger recorded afterwards. This module renders both for people.
"""

import datetime
from typing import Any

import config
from routes.storefront import esc
from routes.storefront import moment
from routes.storefront import money
from services import ledger
from services import visitor

# Brand marks, to name the app a decision came through. Paths from Simple
# Icons (CC0); the marks belong to their owners and only name the app.
MARKS = {
  "openai": (
    "#000000",
    "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0"
    "-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0"
    "-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107"
    " 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557"
    " 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557"
    " 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764"
    "-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l"
    "2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945"
    " 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783"
    " 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1"
    "-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408"
    " 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765"
    "l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504"
    " 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757"
    ".0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772"
    "a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759"
    ".7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l"
    "4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638"
    "a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l"
    "-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602"
    "-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z",
  ),
  "claude": (
    "#D97757",
    "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486"
    "-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546"
    "-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255"
    "h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879"
    "-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607"
    ".2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182"
    "-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607"
    "-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642"
    ".6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579"
    "v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468"
    "-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643"
    " 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046"
    ".5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414"
    "-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794"
    " 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364"
    ".8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892"
    ".522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113"
    "-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768"
    "-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l"
    ".4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214"
    "-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156"
    ".3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157"
    "-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796"
    " 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386"
    "-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293"
    ".1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z",
  ),
  "telegram": (
    "#26A5E4",
    "M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0"
    " 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0"
    " 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627"
    "-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653"
    "-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307"
    "-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14"
    "-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752"
    "-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83"
    "-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z",
  ),
}

# A line icon for the channels without a brand, and for the two buyers.
ICONS = {
  "web": (
    '<rect x="3" y="4" width="18" height="13" rx="2"/>'
    '<path d="M8 21h8M12 17v4"/>'
  ),
  "person": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/>',
  "agent": (
    '<circle cx="12" cy="3.5" r="1.3"/><path d="M12 5v2"/>'
    '<rect x="4.5" y="7" width="15" height="11" rx="4.5"/>'
    '<circle cx="9.5" cy="12" r="1.2" fill="currentColor" stroke="none"/>'
    '<circle cx="14.5" cy="12" r="1.2" fill="currentColor" stroke="none"/>'
    '<path d="M9.5 14.8c1.4 1.1 3.6 1.1 5 0"/>'
  ),
  "voice": (
    '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 005 5L15 13l5 2v4a2 2 0 01-2 2'
    'A16 16 0 013 6a2 2 0 012-2z"/>'
  ),
  "mcp": (
    '<circle cx="12" cy="12" r="3"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>'
  ),
}


def icon(name: str) -> str:
  """Return a small inline icon: a brand's mark, or one of our line icons."""
  if name in MARKS:
    color, path = MARKS[name]
    return (
      f'<svg class="pmark" viewBox="0 0 24 24" aria-hidden="true"'
      f' style="color:{color}"><path fill="currentColor" d="{path}"/></svg>'
    )
  return (
    '<svg class="pmark stroke" viewBox="0 0 24 24" aria-hidden="true"'
    ' fill="none" stroke="currentColor" stroke-width="1.8"'
    f' stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>'
  )


def channel(name: str | None) -> tuple[str, str]:
  """Name an app or channel for people: ("ChatGPT", "openai")."""
  n = (name or "").lower()
  if n.startswith("card:"):
    n = n[5:]
  if not n or n in ("page", "button", "web"):
    return "the agent's page", "web"
  if "claude" in n and "code" in n:
    return "Claude Code", "claude"
  if "claude" in n and "desktop" in n:
    return "Claude Desktop", "claude"
  if "claude" in n:
    return "Claude", "claude"
  if "chatgpt" in n or "openai" in n or "codex" in n:
    return "ChatGPT", "openai"
  if "telegram" in n:
    return "Telegram", "telegram"
  if "voice" in n or "phone" in n:
    return "the phone", "voice"
  if "agent" in n:
    return "the agent itself", "agent"
  return name or "an assistant", "mcp"


def _preposition(label: str) -> str:
  return (
    "on" if label in ("the agent's page", "Telegram", "the phone") else "in"
  )


def badge(order: dict[str, Any], short: bool = False) -> str:
  """One line on who placed an order, for the list of orders.

  `short` keeps it to the buyer and the app's mark, for tight rows.
  """
  if order.get("channel") == "web":
    text = "You" if short else "You, on the web"
    return f'<span class="who by-person">{icon("person")} {text}</span>'
  told = order.get("agent_context") or {}
  label, mark = channel(told.get("approved_via"))
  via = ""
  if told and not short:
    via = f" · approved {_preposition(label)} {esc(label)}"
  app = (
    f'<span class="who-app" title="Approved {_preposition(label)}'
    f' {esc(label)}">{icon(mark)}</span>'
    if told
    else ""
  )
  return (
    f'<span class="who by-agent">{icon("agent")} Your AI agent{via} {app}'
    "</span>"
  )


def where(name: str | None) -> str:
  """Say where a yes came from, capitalised: "In ChatGPT"."""
  label, _ = channel(name)
  return f"{_preposition(label).capitalize()} {label}"


def _when(iso: str | None) -> str:
  if not iso:
    return ""
  try:
    at = datetime.datetime.fromisoformat(iso)
  except ValueError:
    return esc(iso)
  return at.astimezone().strftime("%-d %b, %H:%M")


def card(order: dict[str, Any], order_id: str) -> str:
  """Who bought this order, in detail: the person, or their agent and how."""
  if order.get("channel") == "web":
    told = visitor.describe(order.get("visit"))
    rows = [
      ("Placed by", f"{icon('person')} You, signed in on this site"),
      ("When", _when(order.get("placed_at"))),
      ("Your browser said", esc(told) if told else None),
    ]
    return _dl("Who bought this", rows)

  told = order.get("agent_context") or {}
  signature = (order.get("signature") or {}).get("status")
  signed = {
    "verified": "verified: the request was signed with the agent's key",
    "failed": "failed: the request's signature did not check out",
    "missing": "none: the request was not signed",
  }.get(signature)
  label, mark = channel(told.get("approved_via"))
  proposer, proposer_mark = channel(told.get("proposed_by") or "agent")
  profile = order.get("agent") or ""
  agent = esc(told.get("agent_name") or "Your AI agent")
  if profile:
    agent = f'<a href="{esc(profile)}" rel="noopener">{agent}</a>'
  records = told.get("records_url")
  if records:
    records = records.rstrip("/") + "/" + order_id
  rows = [
    ("Placed by", f"{icon('agent')} <span>{agent}, over UCP</span>"),
    ("When", _when(order.get("placed_at"))),
    ("Signature", esc(signed) if signed else None),
    (
      "Proposed by",
      f"{icon(proposer_mark)} {esc(proposer)}"
      if told.get("proposed_by")
      else None,
    ),
    (
      "You approved it",
      f"{icon(mark)} {_preposition(label)} {esc(label)}"
      + (f", {_when(told['approved_at'])}" if told.get("approved_at") else "")
      if told
      else None,
    ),
    (
      "Agent's records",
      f'<a href="{esc(records)}" rel="noopener">What the agent recorded</a>'
      " (its own page, your sign-in)"
      if records
      else None,
    ),
  ]
  if not told:
    rows.append(
      (
        "Note",
        "The agent did not say how you decided; the shop only knows it paid.",
      )
    )
  return _dl("Who bought this", rows)


def _dl(title: str, rows: list[tuple[str, str | None]]) -> str:
  items = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in rows if v)
  return (
    f'<section class="prov"><h2>{esc(title)}</h2>'
    f'<dl class="fine">{items}</dl></section>'
  )


def _describe(event: dict[str, Any]) -> str | None:
  """Say what one ledger event meant, or None for events not worth a line."""
  d = event.get("detail") or {}
  kind = event.get("event")
  if kind == "CHECKOUT_CREATED":
    return f"Checkout opened, total {money(d.get('total', 0))}"
  if kind == "CHECKOUT_CHANGED":
    return (
      f"The total moved from {money(d.get('approved_total', 0))} to"
      f" {money(d.get('new_total', 0))}; the shop refused to charge the old"
      " one"
    )
  if kind == "PAYMENT_DECLINED":
    return "Card declined" + (
      f": {esc(d['message'])}" if d.get("message") else ""
    )
  if kind == "PAYMENT_CONFIRMED":
    paid = money(d.get("amount", 0))
    coins = f" + {d['coins']} coins" if d.get("coins") else ""
    door = "by your AI agent" if d.get("channel") == "agent" else "by you"
    told = d.get("agent_context") or {}
    via = ""
    if told.get("approved_via"):
      label, _ = channel(told["approved_via"])
      via = f", approved {_preposition(label)} {label}"
    return f"Paid {paid}{coins} {door}{esc(via)}"
  if kind == "VOUCHER_ISSUED":
    n = d.get("vouchers", 1)
    return f"{n} voucher{'s' if n != 1 else ''} issued"
  if kind == "VOUCHER_REDEEMED":
    return "Used at the venue"
  if kind == "REDEMPTION_FAILED":
    return "Turned away at the venue; the merchant could not deliver"
  if kind == "MERCHANT_CANCELLED":
    return "Cancelled by the marketplace"
  if kind == "BOOKING_CHANGED":
    return f"Moved to {moment(d['to'])}" if d.get("to") else "Visit moved"
  if kind == "SHOPPER_CANCELLED":
    return "Cancelled by you"
  if kind == "REFUND_AUTHORISED":
    back = money(d.get("amount", 0))
    coins = f" and {d['coins']} coins" if d.get("coins") else ""
    why = f" — {esc(d['reason'])}" if d.get("reason") else ""
    return f"Refunded {back}{coins}{why}"
  return None


def timeline(order: dict[str, Any], order_id: str) -> str:
  """Render what this shop's ledger recorded about an order, oldest first."""
  mine = config.get_shop()["name"]
  checkout_id = order.get("checkout_id")
  lines = []
  for event in ledger.read():
    if event.get("seller") != mine:
      continue
    d = event.get("detail") or {}
    if (
      event.get("checkout_id") != checkout_id and d.get("order_id") != order_id
    ):
      continue
    said = _describe(event)
    if said:
      lines.append(
        f"<li><time>{_when(event.get('at'))}</time><span>{said}</span></li>"
      )
  if not lines:
    return ""
  return (
    '<section class="prov"><h2>What the shop recorded</h2>'
    f'<ol class="recorded">{"".join(lines)}</ol>'
    "<p class=\"tech\">From the shop's own append-only ledger; the agent's"
    " records are kept separately, by the agent.</p></section>"
  )
