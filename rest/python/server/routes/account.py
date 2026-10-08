"""Account routes: registering, signing in and out, and the account page.

Browsing and filling a cart need no account. Checking out, and seeing the
vouchers bought, do: the pages that need one send the shopper here and bring
them back afterwards (`next`).
"""

import datetime
from typing import Annotated

import config
import db
import dependencies
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from exceptions import ResourceNotFoundError
from routes import storefront
from routes.storefront import esc
from services import account_service
from services import coin_service
from services.checkout_service import CheckoutService

router = APIRouter()

# How each kind of coin movement reads to the customer.
COIN_REASONS = {
  "granted": "A gift from the shop",
  "earned": "Earned on a purchase",
  "spent": "Paid with coins",
  "refunded": "Refund of coins paid",
  "reversed": "Coins of a refunded purchase",
}


def _day(iso: str) -> str:
  if not iso:
    return ""
  return datetime.datetime.fromisoformat(iso).strftime("%-d %b %Y")


SESSION_SECONDS = account_service.SESSION_DAYS * 24 * 3600


def _auth_page(
  request: Request,
  shopper: storefront.Shopper,
  error: str = "",
  form: dict[str, str] | None = None,
  status_code: int = 200,
) -> HTMLResponse:
  """Render the sign-in form.

  There is no public registration: the shop creates the accounts with
  `POST /accounts` and the merchant secret.
  """
  form = form or {}
  back_to = storefront.local_path(
    form.get("next") or request.query_params.get("next")
  )
  body = f"""
    <section class="auth">
      <h1>Sign in</h1>
      {storefront.notice_banner(error)}
      <form method="post" action="/login">
        <input type="hidden" name="next" value="{esc(back_to)}">
        <label>Email
          <input name="email" type="email" required autocomplete="email"
                 value="{esc(form.get("email", ""))}"></label>
        <label>Password
          <input name="password" type="password" required
                 autocomplete="current-password"></label>
        <button class="primary" type="submit">Sign in</button>
      </form>
      <p class="switch">Accounts are set up by the shop. Ask us for yours.</p>
    </section>"""
  return HTMLResponse(
    storefront.page("Sign in", body, request, shopper), status_code=status_code
  )


async def _sign_in(
  shopper: storefront.Shopper,
  transactions_session: storefront.TransactionsDb,
  user: object,
  back_to: str | None,
) -> RedirectResponse:
  """Start a session for an account and send the browser on its way."""
  token = await account_service.start_session(transactions_session, user)
  response = RedirectResponse(storefront.local_path(back_to), status_code=303)
  response.set_cookie(
    shopper.session_cookie,
    token,
    max_age=SESSION_SECONDS,
    httponly=True,
    samesite="lax",
    secure=config.FLAGS.secure_cookies,
  )
  return response


@router.post(
  "/accounts",
  response_model=dict[str, str],
  operation_id="create_account",
  dependencies=[Depends(dependencies.verify_simulation_secret)],
)
async def create_account(
  transactions_session: storefront.TransactionsDb,
  full_name: Annotated[str, Body(embed=True)],
  email: Annotated[str, Body(embed=True)],
  password: Annotated[str, Body(embed=True)],
) -> dict[str, str]:
  """Create an account, as the shop. People can't register themselves."""
  try:
    user = await account_service.register(
      transactions_session, full_name, email, password
    )
  except account_service.AccountError as e:
    raise HTTPException(status_code=400, detail=str(e)) from e
  return {"email": user.email, "full_name": user.full_name}


@router.put(
  "/accounts/{email}/password",
  response_model=dict[str, str],
  operation_id="set_account_password",
  dependencies=[Depends(dependencies.verify_simulation_secret)],
)
async def set_account_password(
  transactions_session: storefront.TransactionsDb,
  email: str,
  password: Annotated[str, Body(embed=True)],
) -> dict[str, str]:
  """Set an account's password, as the shop."""
  try:
    await account_service.set_password(transactions_session, email, password)
  except account_service.AccountError as e:
    raise HTTPException(status_code=400, detail=str(e)) from e
  return {"email": email.lower()}


@router.get("/login", include_in_schema=False)
async def login_page(
  request: Request, shopper: storefront.ShopperSession
) -> Response:
  """Show the sign-in form."""
  if shopper.user:
    return RedirectResponse("/account", status_code=303)
  return _auth_page(request, shopper)


@router.post("/login", include_in_schema=False)
async def login(
  request: Request,
  shopper: storefront.ShopperSession,
  transactions_session: storefront.TransactionsDb,
) -> Response:
  """Sign an account in."""
  form = await storefront.read_form(request)
  user = await account_service.authenticate(
    transactions_session, form.get("email", ""), form.get("password", "")
  )
  if not user:
    # The same answer for an unknown email and a wrong password.
    return _auth_page(
      request,
      shopper,
      error="That email or password isn't right.",
      form=form,
      status_code=401,
    )
  return await _sign_in(shopper, transactions_session, user, form.get("next"))


@router.post("/logout", include_in_schema=False)
async def logout(
  request: Request,
  shopper: storefront.ShopperSession,
  transactions_session: storefront.TransactionsDb,
) -> RedirectResponse:
  """Sign the browser out."""
  await account_service.end_session(
    transactions_session, request.cookies.get(shopper.session_cookie)
  )
  response = RedirectResponse("/", status_code=303)
  response.delete_cookie(shopper.session_cookie)
  return response


def _initials(name: str) -> str:
  parts = [w for w in name.split() if w]
  return "".join(w[0] for w in parts[:2]).upper() or "?"


def _wallet_card(entries, coins: int) -> str:
  """Render the coins: balance, the rate, and the last movements."""
  back = coin_service.back_percent()
  earning = (
    f"You earn {back}% of what you pay by card back in coins."
    if back
    else "This shop doesn't give coins back."
  )
  moves = "".join(
    f"<li><span>{esc(COIN_REASONS.get(entry.reason, entry.reason))}"
    f" <small>{esc(_day(entry.at))}</small></span>"
    f'<b class="{"in" if entry.coins > 0 else "out"}">{entry.coins:+d}</b></li>'
    for entry in list(entries)[:6]
  )
  history = (
    f'<ul class="moves">{moves}</ul>'
    if moves
    else '<p class="acc-empty">Nothing moved yet.</p>'
  )
  worth = (
    f"Worth {storefront.money(coins * 100)} at checkout."
    if coins
    else "Use them at checkout."
  )
  return f"""
    <section class="acc-card wallet-card">
      <h2>Coins</h2>
      <p class="balance"><strong>{coins}</strong>
        coin{"s" if coins != 1 else ""} <span>{esc(worth)}</span></p>
      <p class="acc-note">One coin is worth $1 here. {esc(earning)}</p>
      {history}
    </section>"""


def _orders_card(orders: list[tuple[str, dict]]) -> str:
  """Render the orders: a few numbers, the latest, and the way to them all."""
  from routes import provenance
  from routes import web_checkout

  if not orders:
    return """
    <section class="acc-card orders-card">
      <h2>Your orders</h2>
      <p class="acc-empty">Nothing bought yet. Everything you or your AI agent
        buys here shows up in this account.</p>
      <a class="secondary" href="/">Browse the deals</a>
    </section>"""
  ready = spent = by_agent = 0
  for _, order in orders:
    payment = order.get("payment") or {}
    if payment.get("status") != "refunded":
      spent += payment.get("amount", 0)
    if order.get("channel") != "web":
      by_agent += 1
    for line in order["line_items"]:
      if "voucher" in line:
        state, _ = web_checkout._voucher_state(line)
        ready += state == "Ready to use"
  stats = "".join(
    f"<div><b>{esc(str(big))}</b><span>{esc(small)}</span></div>"
    for big, small in (
      (len(orders), "order" if len(orders) == 1 else "orders"),
      (ready, "ready to use"),
      (storefront.money(spent), "spent"),
      (by_agent, "by your AI agent"),
    )
  )
  recent = ""
  for order_id, order in orders[:4]:
    line = order["line_items"][0]
    state, style = (
      web_checkout._voucher_state(line)
      if "voucher" in line
      else ("Ordered", "used")
    )
    recent += f"""
      <li>
        <a class="acc-order" href="/vouchers/{esc(order_id)}">
          {web_checkout._thumb(line)}
          <span class="acc-order-info">
            <b>{esc(line["item"]["title"])}</b>
            <span class="acc-order-meta">{provenance.badge(order, short=True)}
              · {esc(_day(order.get("placed_at") or ""))}</span>
          </span>
          <span class="pill {style}">{esc(state)}</span>
        </a>
      </li>"""
  more = len(orders) - min(len(orders), 4)
  return f"""
    <section class="acc-card orders-card">
      <h2>Your orders</h2>
      <div class="acc-stats">{stats}</div>
      <ul class="acc-recent">{recent}</ul>
      <a class="secondary" href="/vouchers">
        {"All my vouchers" if not more else f"All my vouchers ({len(orders)})"}
      </a>
    </section>"""


def _agents_card(orders: list[tuple[str, dict]]) -> str:
  """Render who buys for this person: each agent, how often, signed, where."""
  from routes import provenance

  agents: dict[str, dict] = {}
  for _, order in orders:
    if order.get("channel") == "web":
      continue
    key = order.get("agent") or "unknown"
    told = order.get("agent_context") or {}
    entry = agents.setdefault(
      key, {"name": None, "orders": 0, "verified": 0, "channels": {}}
    )
    entry["orders"] += 1
    entry["name"] = entry["name"] or told.get("agent_name")
    if (order.get("signature") or {}).get("status") == "verified":
      entry["verified"] += 1
    if told.get("approved_via"):
      _, mark = provenance.channel(told["approved_via"])
      entry["channels"][provenance.where(told["approved_via"])] = mark
  if not agents:
    return """
    <section class="acc-card agents-card">
      <h2>Who buys for you</h2>
      <p class="acc-empty">No AI agent has bought for you yet. An agent that
        checks out over UCP with your email as the buyer puts its orders in
        this account, and says here who it is.</p>
      <p class="acc-note">Agents start from <code>/.well-known/ucp</code>.</p>
    </section>"""
  rows = ""
  for profile, entry in agents.items():
    name = esc(entry["name"] or "An AI agent")
    if profile != "unknown":
      name = f'<a href="{esc(profile)}" rel="noopener">{name}</a>'
    signed = (
      "every request signed"
      if entry["verified"] == entry["orders"]
      else f"{entry['verified']} of {entry['orders']} signed"
      if entry["verified"]
      else "requests not signed"
    )
    marks = "".join(
      f'<span class="acc-chan">{provenance.icon(mark)} {esc(label)}</span>'
      for label, mark in entry["channels"].items()
    )
    n = entry["orders"]
    rows += f"""
      <li>
        <span class="acc-agent-mark">{provenance.icon("agent")}</span>
        <span class="acc-agent-info">
          <b>{name}</b>
          <span class="acc-order-meta">{n} order{"s" if n != 1 else ""}
            · {esc(signed)}</span>
          {f'<span class="acc-chans">{marks}</span>' if marks else ""}
        </span>
      </li>"""
  return f"""
    <section class="acc-card agents-card">
      <h2>Who buys for you</h2>
      <ul class="acc-agents">{rows}</ul>
      <p class="acc-note">Each order says in "My vouchers" who placed it,
        in which app you approved it, and what the shop recorded.</p>
    </section>"""


def _details_card(user, since: str) -> str:
  return f"""
    <section class="acc-card details-card">
      <h2>Account</h2>
      <dl class="fine">
        <dt>Name</dt><dd>{esc(user.full_name)}</dd>
        <dt>Email</dt><dd>{esc(user.email)}</dd>
        <dt>Member since</dt><dd>{esc(since)}</dd>
        <dt>Sign-in</dt><dd>Stays {account_service.SESSION_DAYS} days on this
          browser.</dd>
      </dl>
      <p class="acc-note">The shop creates accounts and resets passwords;
        there is no self-service registration.</p>
    </section>"""


@router.get("/account", include_in_schema=False)
async def account_page(
  request: Request,
  shopper: storefront.ShopperSession,
  transactions_session: storefront.TransactionsDb,
  checkouts: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
  ],
) -> Response:
  """Show the account: the coins, the orders, who buys for them, the way out."""
  user = shopper.user
  if not user:
    return storefront.login_redirect("/account")
  entries = await db.list_coin_entries(transactions_session, email=user.email)
  orders = []
  for order_id in await db.list_order_ids(transactions_session, user.id):
    try:
      orders.append((order_id, await checkouts.get_order(order_id)))
    except ResourceNotFoundError:
      continue  # The shop was re-seeded since.
  orders.sort(key=lambda pair: pair[1].get("placed_at") or "", reverse=True)
  since = datetime.datetime.fromisoformat(user.created_at).strftime("%-d %b %Y")
  body = f"""
    <section class="account">
      <header class="acc-head">
        <span class="avatar big">{esc(_initials(user.full_name))}</span>
        <div>
          <h1>{esc(user.full_name)}</h1>
          <p>{esc(user.email)} · member since {esc(since)}</p>
        </div>
        <form method="post" action="/logout">
          <button class="secondary" type="submit">Sign out</button>
        </form>
      </header>
      <div class="acc-grid">
        {_wallet_card(entries, shopper.coins)}
        {_orders_card(orders)}
        {_agents_card(orders)}
        {_details_card(user, since)}
      </div>
    </section>"""
  return HTMLResponse(storefront.page("Your account", body, request, shopper))
