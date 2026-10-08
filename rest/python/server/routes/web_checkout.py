"""Web checkout routes: a person's cart, checkout and vouchers in a browser.

These pages drive the same cart and checkout services the agent reaches over
UCP, so a web purchase and an agent purchase end in the same order, voucher,
payment and ledger events. What differs is the door: an agent signs its
requests, a person signs in to an account. Filling the cart needs no account;
checking out does, and an order's vouchers are only shown to the account that
bought them.

Every action is a plain HTML form that works on its own. The page's script
(routes/assets/shop.js) sends the same forms with `Accept: application/json`
to update the page in place; the answer then carries the HTML to swap in.

The pay form carries the total the person saw. If the shop's total is no
longer that one, nothing is charged and the page is shown again, the same rule
the agent's checkout follows.
"""

import datetime
from typing import Annotated, Any
import time
import uuid

import config
import db
import dependencies
from exceptions import ConsentRequiredError
from exceptions import OutOfStockError
from exceptions import PaymentFailedError
from exceptions import PurchaseLimitError
from exceptions import ResourceNotFoundError
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from models import UnifiedCart
from models import UnifiedCartCreateRequest
from models import UnifiedCartUpdateRequest
from models import UnifiedCheckoutCreateRequest
from models import UnifiedCheckoutUpdateRequest
from routes import catalog
from routes import provenance
from routes import storefront
from routes.storefront import esc
from routes.storefront import money
from services import visitor
from services import coin_service
from services import ledger
from services import payment_rail
from services import voucher_service
from services.checkout_service import CheckoutService
from ucp_sdk.models.schemas.shopping.payment_create_request import (
  PaymentCreateRequest,
)

router = APIRouter()

Checkouts = Annotated[
  CheckoutService, Depends(dependencies.get_checkout_service)
]

COOKIE_MAX_AGE = 7 * 24 * 3600

# The test cards the pay form offers, and the mock token each one sends.
CARDS = {
  "ok": ("Visa •••• 4242", "success_token"),
  "declined": ("Visa •••• 0002, always declined", "fail_token"),
}

NOTICES = {
  "stock": "Sorry, there aren't enough places left for something you chose.",
  "changed": (
    "The total changed since you last saw it. Nothing was charged. Review"
    " the new total before you pay."
  ),
  "declined": "Your card was declined and nothing was charged. Try another.",
  "limit": "One of these deals has a limit per person, and this goes over it.",
  "promo": "That promo code isn't valid.",
  "paid": "Payment received. Your voucher is ready.",
}


_form = storefront.read_form


def _redirect(url: str) -> RedirectResponse:
  return RedirectResponse(url, status_code=303)


def _wants_json(request: Request) -> bool:
  """Whether the page's script, not a plain form, sent the request."""
  return "application/json" in request.headers.get("accept", "")


def _notice(request: Request) -> str:
  code = request.query_params.get("notice", "")
  kind = "good" if code == "paid" else "warn"
  return storefront.notice_banner(NOTICES.get(code, ""), kind)


def _quantity(text: str | None) -> int:
  try:
    return max(0, int(text or 0))
  except ValueError:
    return 0


def _total(totals: list[dict[str, Any]]) -> int:
  return next(t["amount"] for t in totals if t["type"] == "total")


def _cart_lines(shopper: storefront.Shopper) -> list[dict[str, Any]]:
  """Return the cart's lines in the shape a cart update takes."""
  if not shopper.cart:
    return []
  return [
    {"id": li.id, "item": {"id": li.item.id}, "quantity": li.quantity}
    for li in shopper.cart.line_items
  ]


async def _save_cart(
  shopper: storefront.Shopper,
  carts: Any,
  lines: list[dict[str, Any]],
  promo_codes: list[str] | None = None,
) -> UnifiedCart | None:
  """Store the cart's new lines; an empty cart is deleted.

  `promo_codes` replaces the codes on the cart; None keeps them.
  """
  key = str(uuid.uuid4())
  if not lines:
    if shopper.cart:
      await carts.cancel_cart(shopper.cart.id, key)
    return None
  discounts = None if promo_codes is None else {"codes": promo_codes}
  if shopper.cart:
    request = UnifiedCartUpdateRequest(
      id=shopper.cart.id, line_items=lines, discounts=discounts
    )
    return await carts.update_cart(shopper.cart.id, request, key)
  return await carts.create_cart(
    UnifiedCartCreateRequest(line_items=lines, discounts=discounts), key
  )


async def _cap_to_limits(
  lines: list[dict[str, Any]], products_session: Any
) -> bool:
  """Trim the lines so no deal goes over its limit per person.

  A deal's limit covers all its options together. Returns whether anything
  was trimmed.
  """
  room: dict[str, int] = {}
  trimmed = False
  for line in lines:
    found = await db.get_option(products_session, line["item"]["id"])
    if not found or not found[0].limit_per_person:
      continue
    deal = found[0]
    left = room.setdefault(deal.id, deal.limit_per_person)
    allowed = min(line["quantity"], left)
    trimmed = trimmed or allowed < line["quantity"]
    line["quantity"] = allowed
    room[deal.id] = left - allowed
  lines[:] = [line for line in lines if line["quantity"] > 0]
  return trimmed


def _set_cart_cookie(
  response: Response, shopper: storefront.Shopper, cart: UnifiedCart | None
) -> None:
  """Keep the shopper's cart cookie in step with the stored cart."""
  if cart is None:
    response.delete_cookie(shopper.cart_cookie)
    return
  response.set_cookie(
    shopper.cart_cookie,
    cart.id,
    max_age=COOKIE_MAX_AGE,
    httponly=True,
    samesite="lax",
    secure=config.FLAGS.secure_cookies,
  )


async def _cart_view(
  cart: UnifiedCart | None,
  products_session: Any,
  transactions_session: Any,
) -> dict[str, Any]:
  """Describe the cart for the page: its lines and its sums as HTML."""
  if not cart:
    return {"count": 0, "lines_html": "", "summary_html": "", "promo_code": ""}

  # Units already in the cart per deal, to stop "+" at the limit per person.
  in_cart: dict[str, int] = {}
  found = {}
  for line in cart.line_items:
    found[line.id] = await db.get_option(products_session, line.item.id)
    if found[line.id]:
      deal_id = found[line.id][0].id
      in_cart[deal_id] = in_cart.get(deal_id, 0) + line.quantity

  rows = ""
  for line in cart.line_items:
    stock = await db.get_inventory(transactions_session, line.item.id) or 0
    can_add = line.quantity < min(stock, storefront.MAX_QUANTITY)
    where = pill = ""
    listing_id = line.item.id
    if found[line.id]:
      deal, _ = found[line.id]
      terms = voucher_service.terms(deal)
      listing_id = deal.id
      when = storefront.when_text(terms, short=True)
      where = f'<p class="where">{esc(deal.merchant)} · {esc(when)}</p>'
      pill = storefront.refund_pill(terms["cancellation"])
      if deal.limit_per_person:
        can_add = can_add and in_cart[deal.id] < deal.limit_per_person
    more = "" if can_add else " disabled"
    rows += f"""
      <li class="line">
        {storefront.thumb(listing_id)}
        <div class="info">
          <a href="/deals/{esc(listing_id)}">
            <h3>{esc(line.item.title)}</h3></a>
          {where}
          {pill}
        </div>
        <form class="stepper" method="post" action="/cart/update"
              data-async="cart">
          <input type="hidden" name="line_id" value="{esc(line.id)}">
          <button name="quantity" value="{line.quantity - 1}"
                  aria-label="One less">−</button>
          <output>{line.quantity}</output>
          <button name="quantity" value="{line.quantity + 1}"
                  aria-label="One more"{more}>+</button>
          <button class="remove" name="quantity" value="0">Remove</button>
        </form>
        <p class="amount">{money(line.item.price * line.quantity)}
          <small>{money(line.item.price)} each</small></p>
      </li>"""

  data = cart.model_dump(mode="json")
  applied = (data.get("discounts") or {}).get("applied") or []
  return {
    "count": sum(line.quantity for line in cart.line_items),
    "lines_html": rows,
    "summary_html": _summary(data["totals"], applied),
    "promo_code": applied[0]["code"] if applied else "",
  }


def _summary(
  totals: list[dict[str, Any]], applied: list[dict] | None = None
) -> str:
  """Render the sums of a cart or checkout: each amount, then the total."""
  code = f" ({applied[0]['code']})" if applied else ""
  rows = "".join(
    f"<dt>{esc(label)}{esc(code) if label == 'Discount' else ''}</dt>"
    f"<dd>{esc(amount)}</dd>"
    for label, amount in _breakdown(totals)
    # A lone subtotal would only repeat the total.
    if len(totals) > 2
  )
  with_coins = any(t["type"] == "coins" for t in totals)
  label = "To pay by card" if with_coins else "Total"
  return f"""{rows}
    <dt class="grand">{label}</dt>
    <dd class="grand"><strong>{money(_total(totals))}</strong></dd>"""


async def _cart_response(
  request: Request,
  shopper: storefront.Shopper,
  cart: UnifiedCart | None,
  products_session: Any,
  transactions_session: Any,
  message: str = "",
  bad: bool = False,
) -> Response:
  """Answer a cart change: the new cart for the script, else the cart page."""
  if _wants_json(request):
    view = await _cart_view(cart, products_session, transactions_session)
    response = JSONResponse({**view, "message": message, "bad": bad})
  else:
    response = _redirect("/cart")
  _set_cart_cookie(response, shopper, cart)
  return response


def _thumb(line: dict[str, Any]) -> str:
  """Render the photo of the deal a checkout or order line belongs to."""
  service = line.get("service") or {}
  return storefront.thumb(service.get("deal_id") or line["item"]["id"])


def _terms(line: dict[str, Any]) -> str:
  """Render a line's voucher terms: what the buyer is agreeing to."""
  return storefront.fine_print(line) if "service" in line else ""


@router.post("/cart/add", include_in_schema=False)
async def add_to_cart(
  request: Request,
  shopper: storefront.ShopperSession,
  carts: storefront.Carts,
  products_session: storefront.ProductsDb,
  transactions_session: storefront.TransactionsDb,
) -> Response:
  """Add a deal to the cart, up to the places left."""
  form = await _form(request)
  product_id = form.get("product_id", "")
  product = await db.get_product(products_session, product_id)
  if not product:
    raise HTTPException(status_code=404, detail="Deal not found")
  stock = await db.get_inventory(transactions_session, product_id) or 0
  limit = min(stock, storefront.MAX_QUANTITY)
  if limit <= 0:
    if _wants_json(request):
      return JSONResponse({"message": NOTICES["stock"]}, status_code=409)
    found = await db.get_option(products_session, product_id)
    return _redirect(f"/deals/{found[0].id if found else product_id}")

  lines = _cart_lines(shopper)
  quantity = max(1, _quantity(form.get("quantity")))
  line = next((li for li in lines if li["item"]["id"] == product_id), None)
  if line:
    line["quantity"] = min(line["quantity"] + quantity, limit)
  else:
    lines.append({"item": {"id": product_id}, "quantity": min(quantity, limit)})
  already = shopper.cart_count
  trimmed = await _cap_to_limits(lines, products_session)

  cart = await _save_cart(shopper, carts, lines)
  added = sum(line["quantity"] for line in lines) > already
  message = f"Added {product.title} to your cart" if added else ""
  if trimmed:
    message = NOTICES["limit"]
  return await _cart_response(
    request,
    shopper,
    cart,
    products_session,
    transactions_session,
    message=message,
    bad=not added,
  )


@router.post("/cart/update", include_in_schema=False)
async def update_cart(
  request: Request,
  shopper: storefront.ShopperSession,
  carts: storefront.Carts,
  products_session: storefront.ProductsDb,
  transactions_session: storefront.TransactionsDb,
) -> Response:
  """Change a line's quantity; zero removes it."""
  form = await _form(request)
  lines = _cart_lines(shopper)
  for line in lines:
    if line["id"] == form.get("line_id"):
      stock = (
        await db.get_inventory(transactions_session, line["item"]["id"]) or 0
      )
      line["quantity"] = min(
        _quantity(form.get("quantity")), stock, storefront.MAX_QUANTITY
      )

  lines = [li for li in lines if li["quantity"] > 0]
  trimmed = await _cap_to_limits(lines, products_session)
  cart = await _save_cart(shopper, carts, lines)
  return await _cart_response(
    request,
    shopper,
    cart,
    products_session,
    transactions_session,
    message=NOTICES["limit"] if trimmed else "",
    bad=trimmed,
  )


@router.post("/cart/promo", include_in_schema=False)
async def apply_promo(
  request: Request,
  shopper: storefront.ShopperSession,
  carts: storefront.Carts,
  products_session: storefront.ProductsDb,
  transactions_session: storefront.TransactionsDb,
) -> Response:
  """Put a promo code on the cart; an empty code takes it off."""
  code = (await _form(request)).get("code", "").strip()
  lines = _cart_lines(shopper)
  cart = await _save_cart(shopper, carts, lines, [code] if code else [])
  applied = cart and cart.discounts and cart.discounts.applied
  message, bad = "", False
  if code and not applied:
    # The cart keeps no code it can't use.
    cart = await _save_cart(shopper, carts, lines, [])
    message, bad = NOTICES["promo"], True
  elif code:
    message = f"Code {applied[0].code} applied"
  response = await _cart_response(
    request,
    shopper,
    cart,
    products_session,
    transactions_session,
    message=message,
    bad=bad,
  )
  if bad and not _wants_json(request):
    return _redirect("/cart?notice=promo")
  return response


@router.get("/cart", include_in_schema=False)
async def cart_page(
  request: Request,
  shopper: storefront.ShopperSession,
  products_session: storefront.ProductsDb,
  transactions_session: storefront.TransactionsDb,
) -> Response:
  """Show the cart with a quantity control per line."""
  view = await _cart_view(shopper.cart, products_session, transactions_session)
  if _wants_json(request):
    return JSONResponse({**view, "message": "", "bad": False})

  empty = not view["count"]
  advertised = config.get_shop().get("promo_code")
  hint = f"Try <code>{esc(advertised)}</code>." if advertised else ""
  body = f"""
    <section class="blank" data-cart-empty{"" if empty else " hidden"}>
      <h1>Your cart is empty</h1>
      <p>Find something to do and it will show up here.</p>
      <a class="primary" href="/">Browse deals</a>
    </section>
    <div data-cart-filled{" hidden" if empty else ""}>
      <h1 class="title">Your cart</h1>
      {_notice(request)}
      <div class="detail">
        <ul class="lines" data-cart-lines>{view["lines_html"]}</ul>
        <aside class="buy">
          <dl class="sum" data-cart-summary>{view["summary_html"]}</dl>
          <form class="promo-code" method="post" action="/cart/promo"
                data-async="cart">
            <input name="code" placeholder="Promo code" aria-label="Promo code"
                   value="{esc(view["promo_code"])}" data-cart-promo
                   autocomplete="off">
            <button class="secondary" data-busy="Applying…">Apply</button>
          </form>
          <p class="tech">{hint} Any fee is shown before you pay.</p>
          <form method="post" action="/checkout">
            <button class="primary" type="submit">Checkout</button>
          </form>
          <a class="back" href="/">Keep browsing</a>
        </aside>
      </div>
    </div>"""
  return HTMLResponse(storefront.page("Your cart", body, request, shopper))


@router.get("/search", include_in_schema=False)
async def search_suggestions(
  q: str,
  products_session: storefront.ProductsDb,
  transactions_session: storefront.TransactionsDb,
) -> list[dict[str, Any]]:
  """Suggest deals while the shopper types in the search box."""
  words = q.lower().split()
  suggestions = []
  for listing in await catalog.load_catalog(
    products_session, transactions_session
  ):
    deal = listing.deal
    text = f"{listing.title} {deal.merchant} {deal.category}" if deal else ""
    text = (text or listing.title).lower()
    if not words or not all(word in text for word in words):
      continue
    lead = storefront.lead_offer(listing)
    varies = len({offer.product.price for offer in listing.offers}) > 1
    suggestions.append(
      {
        "url": f"/deals/{listing.id}",
        "title": listing.title,
        "where": deal.merchant if deal else "",
        "price": ("From " if varies else "") + money(lead.product.price),
        "image": f"/images/{listing.id}.jpg"
        if catalog.image_file(listing.id)
        else None,
        "sold_out": listing.stock <= 0,
      }
    )
  return suggestions[:6]


@router.post("/checkout", include_in_schema=False)
async def start_checkout(
  request: Request, shopper: storefront.ShopperSession, checkouts: Checkouts
) -> Response:
  """Open a checkout for what is in the cart."""
  # A declared agent is sent to the agent door before anything else.
  if visitor.classify(request.headers).declared:
    return storefront.agent_door(request, shopper)
  if not shopper.user:
    return storefront.login_redirect("/cart")
  if not shopper.cart:
    return _redirect("/cart")
  lines = [
    {"item": line["item"], "quantity": line["quantity"]}
    for line in _cart_lines(shopper)
  ]
  discounts = shopper.cart.discounts
  codes = (discounts and discounts.codes) or []
  try:
    checkout = await checkouts.create_checkout(
      UnifiedCheckoutCreateRequest(
        line_items=lines,
        discounts={"codes": codes},
        # The account is the buyer, so the checkout knows their wallet.
        buyer={
          "full_name": shopper.user.full_name,
          "email": shopper.user.email,
        },
      ),
      str(uuid.uuid4()),
    )
  except OutOfStockError:
    return _redirect("/cart?notice=stock")
  except PurchaseLimitError:
    return _redirect("/cart?notice=limit")
  return _redirect(f"/checkout/{checkout.id}")


async def _load_checkout(checkouts: CheckoutService, checkout_id: str) -> dict:
  try:
    checkout = await checkouts.get_checkout(checkout_id)
  except ResourceNotFoundError as e:
    raise HTTPException(status_code=404, detail="Checkout not found") from e
  return checkout.model_dump(mode="json", by_alias=True, exclude_none=True)


@router.get(
  "/checkout/{id}", response_class=HTMLResponse, include_in_schema=False
)
async def checkout_page(
  request: Request,
  shopper: storefront.ShopperSession,
  checkouts: Checkouts,
  checkout_id: Annotated[str, Path(..., alias="id")],
):
  """Show what is being bought, on what terms, and take the payment."""
  if visitor.classify(request.headers).declared:
    return storefront.agent_door(request, shopper)
  if not shopper.user:
    return storefront.login_redirect(f"/checkout/{checkout_id}")
  checkout = await _load_checkout(checkouts, checkout_id)
  if checkout["status"] == "completed":
    return _redirect(f"/vouchers/{checkout['order']['id']}")
  if checkout["status"] == "canceled":
    return _redirect("/cart")

  lines = ""
  for line in checkout["line_items"]:
    item = line["item"]
    amount = _total(line["totals"])
    lines += f"""
      <li class="line review">
        {_thumb(line)}
        <div class="info">
          <h3>{esc(item["title"])}</h3>
          <p class="where">Quantity {line["quantity"]} · {money(amount)}</p>
          {_terms(line)}
        </div>
      </li>"""

  applied = (checkout.get("discounts") or {}).get("applied")
  total = _total(checkout["totals"])
  wallet = checkout.get("coins") or {}
  coins = _coins_form(checkout_id, wallet, total)
  earns = ""
  if wallet.get("earns"):
    n = wallet["earns"]
    earns = (
      f'<p class="earn">You\'ll earn {n} coin{"s" if n != 1 else ""} back.</p>'
    )
  rail = payment_rail.rail_name()
  # Coins can cover the whole purchase; then there is no card to choose.
  card_choice = _card_form(rail) if total else ""
  if rail == payment_rail.StripeRail.name:
    mode = payment_rail.StripeRail().mode
    about = f"Stripe, {mode} mode" + (
      ": no real money moves." if mode == "test" else "."
    )
  else:
    about = f"Test mode: no real money moves (rail: <code>{esc(rail)}</code>)."
  pay_label = (
    f"Pay {money(total)}"
    if total
    else f"Pay with {wallet.get('applied', 0)} coins"
  )
  body = f"""
    <nav class="crumbs"><a href="/cart">Cart</a> › Checkout</nav>
    <h1 class="title">Review and pay</h1>
    {_notice(request)}
    <div class="detail">
      <ul class="lines">{lines}</ul>
      <aside class="buy">
        <dl class="sum">{_summary(checkout["totals"], applied)}</dl>
        {earns}
        {coins}
        <form class="pay" method="post" data-async="pay"
              action="/checkout/{esc(checkout_id)}/pay">
          <input type="hidden" name="seen_total" value="{total}">
          <input type="hidden" name="shown_at" value="{time.time():.0f}">
          <p class="notice warn" data-pay-error hidden></p>
          <p class="buyer">Buying as <b>{esc(shopper.user.full_name)}</b>
            <span>{esc(shopper.user.email)}</span></p>
          {card_choice}
          <button class="primary" type="submit"
                  data-busy="Processing payment…">{pay_label}</button>
        </form>
        <p class="tech">{about}</p>
      </aside>
    </div>"""
  return HTMLResponse(storefront.page("Checkout", body, request, shopper))


def _card_form(rail: str) -> str:
  """Render the card part of the pay form, for the rail in use.

  With Stripe, the card fields are Stripe's own (loaded from js.stripe.com):
  the browser turns them into a PaymentMethod and the shop only ever sees
  its id. With the mock rail, a choice of pretend cards.
  """
  if rail == payment_rail.StripeRail.name:
    key = payment_rail.publishable_key()
    return f"""
      <script src="https://js.stripe.com/v3/"></script>
      <fieldset class="stripe" data-stripe="{esc(key)}">
        <legend>Pay by card</legend>
        <div class="card-box" id="card-element"></div>
        <p class="hint">Test card: 4242 4242 4242 4242, any future date, any
          CVC. 4000 0000 0000 0002 is always declined.</p>
      </fieldset>
      <input type="hidden" name="payment_method" value="">"""
  cards = "".join(
    f'<label class="radio"><input type="radio" name="card" value="{key}"'
    f"{' checked' if key == 'ok' else ''}> {esc(label)}</label>"
    for key, (label, _) in CARDS.items()
  )
  return f"<fieldset><legend>Pay with a test card</legend>{cards}</fieldset>"


def _coins_form(checkout_id: str, wallet: dict[str, Any], total: int) -> str:
  """Render the control to pay part of a checkout with coins."""
  balance, using = wallet.get("balance", 0), wallet.get("applied", 0)
  if not balance:
    return ""
  # Whole coins only, and never more than the purchase costs.
  most = min(balance, using + total // coin_service.COIN_VALUE)
  return f"""
    <form class="use-coins" method="post"
          action="/checkout/{esc(checkout_id)}/coins">
      <p>You have <b>{balance} coins</b> in this shop. Use them here:</p>
      <div class="row">
        <div class="stepper" data-stepper>
          <button type="button" data-step="-1" aria-label="One less">−</button>
          <input name="use" type="number" min="0" max="{most}"
                 value="{using}" aria-label="Coins to use">
          <button type="button" data-step="1" aria-label="One more">+</button>
        </div>
        <button class="secondary" type="submit">Apply</button>
        <button class="secondary" type="submit" name="all"
                value="{most}">Use {most}</button>
      </div>
    </form>"""


def _checkout_lines(checkout: dict[str, Any]) -> list[dict[str, Any]]:
  """Return a checkout's lines in the shape a checkout update takes."""
  return [
    {
      "id": li["id"],
      "item": {"id": li["item"]["id"]},
      "quantity": li["quantity"],
    }
    for li in checkout["line_items"]
  ]


@router.post("/checkout/{id}/coins", include_in_schema=False)
async def use_coins(
  request: Request,
  shopper: storefront.ShopperSession,
  checkouts: Checkouts,
  checkout_id: Annotated[str, Path(..., alias="id")],
) -> RedirectResponse:
  """Set how many coins pay for the checkout; the rest goes on the card."""
  here = f"/checkout/{checkout_id}"
  if not shopper.user:
    return storefront.login_redirect(here)
  form = await _form(request)
  checkout = await _load_checkout(checkouts, checkout_id)
  if checkout["status"] == "ready_for_complete":
    # The server caps the number at what the wallet holds and the price.
    use = _quantity(form.get("all") or form.get("use"))
    update = UnifiedCheckoutUpdateRequest(
      line_items=_checkout_lines(checkout), coins={"use": use}
    )
    await checkouts.update_checkout(checkout_id, update, str(uuid.uuid4()))
  return _redirect(here)


@router.post("/checkout/{id}/pay", include_in_schema=False)
async def pay(
  request: Request,
  shopper: storefront.ShopperSession,
  carts: storefront.Carts,
  checkouts: Checkouts,
  checkout_id: Annotated[str, Path(..., alias="id")],
) -> Response:
  """Charge the total the buyer saw, or stop if it moved."""
  here = f"/checkout/{checkout_id}"
  who = visitor.classify(request.headers)
  if who.declared:
    return storefront.agent_door(request, shopper, _wants_json(request))
  form = await _form(request)
  user = shopper.user
  if not user:
    sign_in = storefront.login_redirect(here)
    if _wants_json(request):
      return JSONResponse(
        {"code": "login", "redirect": sign_in.headers["location"]}
      )
    return sign_in

  def refuse(code: str, message: str = "", **extra: Any) -> Response:
    """Answer a payment that didn't go through; nothing was charged."""
    if _wants_json(request):
      text = message or NOTICES[code]
      return JSONResponse({"code": code, "message": text, **extra})
    return _redirect(f"{here}?notice={code}")

  def done(url: str) -> Response:
    if _wants_json(request):
      return JSONResponse({"code": "paid", "redirect": url})
    return _redirect(url)

  checkout = await _load_checkout(checkouts, checkout_id)
  if checkout["status"] == "completed":
    return done(f"/vouchers/{checkout['order']['id']}")

  try:
    # Attaching the buyer recalculates the checkout, so this is also where a
    # price or fee change since the page was shown comes to light.
    update = UnifiedCheckoutUpdateRequest(
      line_items=_checkout_lines(checkout),
      buyer={"full_name": user.full_name, "email": user.email},
    )
    updated = await checkouts.update_checkout(
      checkout_id, update, str(uuid.uuid4())
    )
    seen_total = _quantity(form.get("seen_total"))
    totals = updated.model_dump(mode="json")["totals"]
    new_total = _total(totals)
    if new_total != seen_total:
      ledger.emit(
        "CHECKOUT_CHANGED",
        checkout_id=checkout_id,
        detail={"approved_total": seen_total, "new_total": new_total},
      )
      return refuse(
        "changed",
        seen_total=money(seen_total),
        new_total=money(new_total),
        new_total_cents=new_total,
        breakdown=_breakdown(totals),
      )

    if payment_rail.rail_name() == payment_rail.StripeRail.name:
      handler, token = "stripe", form.get("payment_method", "")
      if new_total and not token:
        return refuse("declined", message="Enter your card details first.")
    else:
      handler = "mock_payment_handler"
      _, token = CARDS.get(form.get("card", ""), CARDS["ok"])
    card = {
      "id": "web_card",
      "handler_id": handler,
      "type": "card",
      "credential": {"type": "token", "token": token},
    }
    # Coins can cover it all; then no card is charged.
    payment = PaymentCreateRequest(instruments=[card] if new_total else [])
    completed = await checkouts.complete_checkout(
      checkout_id,
      payment,
      {},
      str(uuid.uuid4()),
      channel="web",
      visit=visitor.purchase_record(
        who, page_script=_wants_json(request), shown_at=form.get("shown_at")
      ),
    )
  except OutOfStockError:
    return refuse("stock")
  except PurchaseLimitError as e:
    return refuse("limit", message=e.message)
  except PaymentFailedError as e:
    # Say why, when the rail says: "Your card was declined." beats a code.
    return refuse("declined", message=e.message if _wants_json(request) else "")
  except ConsentRequiredError:
    return refuse("changed")

  order_id = completed.order.id
  response = done(f"/vouchers/{order_id}?notice=paid")
  # The cart has been bought; empty it.
  _set_cart_cookie(response, shopper, await _save_cart(shopper, carts, []))
  return response


def _breakdown(totals: list[dict[str, Any]]) -> list[tuple[str, str]]:
  """Return the lines above the total: (label, amount) for each."""
  labels = {
    "subtotal": "Subtotal",
    "fee": "Booking fee",
    "discount": "Discount",
  }
  return [
    (
      t.get("display_text") or labels.get(t["type"], t["type"]),
      money(t["amount"]),
    )
    for t in totals
    if t["type"] != "total"
  ]


def _voucher_state(line: dict[str, Any]) -> tuple[str, str]:
  """Return a voucher's state for people, and the style to show it in."""
  if line["voucher"]["status"] == "refunded":
    return "Refunded", "final"
  return {
    "redeemed": ("Used", "used"),
    "cancelled_by_merchant": ("Cancelled by the merchant", "final"),
    "redemption_failed": ("Couldn't be used at the venue", "final"),
  }.get(line["redemption"]["status"], ("Ready to use", "free"))


def _paid_label(order: dict[str, Any]) -> str:
  refunded = (order.get("payment") or {}).get("status") == "refunded"
  return "Refunded" if refunded else "Paid"


@router.get("/vouchers/{id}", include_in_schema=False)
async def voucher_page(
  request: Request,
  shopper: storefront.ShopperSession,
  checkouts: Checkouts,
  transactions_session: storefront.TransactionsDb,
  order_id: Annotated[str, Path(..., alias="id")],
) -> Response:
  """Show an order's vouchers: the code, how to use it and its state."""
  if not shopper.user:
    if _wants_json(request):
      raise HTTPException(status_code=401, detail="Sign in to see this order")
    return storefront.login_redirect(f"/vouchers/{order_id}")
  # Someone else's order looks the same as one that doesn't exist.
  owner = await db.get_order_owner(transactions_session, order_id)
  try:
    if owner != shopper.user.id:
      raise ResourceNotFoundError("Order not found")
    order = await checkouts.get_order(order_id)
  except ResourceNotFoundError as e:
    if _wants_json(request):
      raise HTTPException(status_code=404, detail="Order not found") from e
    return storefront.not_found(request, shopper, "order")

  if _wants_json(request):
    # The page asks again every few seconds to follow the voucher's state.
    states = {}
    for line in order["line_items"]:
      if "voucher" in line:
        state, style = _voucher_state(line)
        states[line["id"]] = {"state": state, "style": style}
    return JSONResponse({"paid": _paid_label(order), "vouchers": states})

  lines = ""
  for line in order["line_items"]:
    item = line["item"]
    code = ""
    if "voucher" in line:
      state, style = _voucher_state(line)
      codes = "".join(
        f"""
          <li><strong>{esc(code)}</strong>
            <button class="copy" type="button"
                    data-copy="{esc(code)}">Copy</button></li>"""
        for code in line["voucher"]["codes"]
      )
      plural = "s" if len(line["voucher"]["codes"]) > 1 else ""
      code = f"""
        <div class="code">
          <span>Voucher code{plural}</span>
          <span class="pill {style}"
                data-voucher="{esc(line["id"])}">{esc(state)}</span>
          <ul>{codes}</ul>
        </div>"""
    lines += f"""
      <li class="line review">
        {_thumb(line)}
        <div class="info">
          <h3>{esc(item["title"])}</h3>
          <p class="where">Quantity {line["quantity"]["total"]}</p>
          {code}
          {_terms(line)}
        </div>
      </li>"""

  payment = order.get("payment") or {}
  coin_rows = ""
  if payment.get("coins"):
    coin_rows += f"<dt>With coins</dt><dd>{payment['coins']} coins</dd>"
  if payment.get("coins_earned"):
    coin_rows += f"<dt>Coins earned</dt><dd>+{payment['coins_earned']}</dd>"
  # Fresh from paying: the page marks the moment.
  paid_now = request.query_params.get("notice") == "paid"
  celebrate = " data-celebrate" if paid_now else ""
  body = f"""
    <nav class="crumbs"><a href="/vouchers">My vouchers</a> › Order</nav>
    <h1 class="title">Your order</h1>
    <p class="origin">{provenance.badge(order)}</p>
    {_notice(request)}
    <div class="detail" data-live-order{celebrate}>
      <ul class="lines">{lines}</ul>
      <aside class="buy">
        <dl class="sum">
          <dt class="big" data-paid>{_paid_label(order)}</dt>
          <dd class="big">
            <strong>{money(payment.get("amount", 0))}</strong></dd>
          {coin_rows}
        </dl>
        <p class="tech">Test payment, no real money moved (rail:
          <code>{esc(payment.get("rail", ""))}</code>). Reference
          <code>{esc(payment.get("payment_id", ""))}</code>. Order
          <code>{esc(order_id)}</code>.</p>
        <a class="back" href="/">Keep browsing</a>
      </aside>
    </div>
    <div class="provenance">
      {provenance.card(order, order_id)}
      {provenance.timeline(order, order_id)}
    </div>"""
  return HTMLResponse(storefront.page("Your order", body, request, shopper))


# The states the vouchers list can be narrowed to, and which rows each takes.
STATE_FILTERS = {
  "all": ("All", None),
  "ready": ("Ready to use", {"free"}),
  "used": ("Used", {"used"}),
  "closed": ("Refunded or cancelled", {"final"}),
}
WHO_FILTERS = {
  "all": ("Anyone", None),
  "me": ("Bought by me", "web"),
  "agent": ("Bought by my AI agent", "agent"),
}


def _order_state(order: dict[str, Any]) -> tuple[str, str]:
  """Return one state for the whole order: its first voucher's."""
  line = order["line_items"][0]
  return _voucher_state(line) if "voucher" in line else ("Ordered", "used")


def _month(iso: str | None) -> str:
  if not iso:
    return "Earlier"
  return datetime.datetime.fromisoformat(iso).astimezone().strftime("%B %Y")


def _placed(iso: str | None) -> str:
  if not iso:
    return ""
  return (
    datetime.datetime.fromisoformat(iso)
    .astimezone()
    .strftime("%a %-d %b, %H:%M")
  )


def _filter_chips(
  name: str, options: dict[str, tuple[str, Any]], chosen: str, other: str
) -> str:
  chips = ""
  for key, (label, _) in options.items():
    href = f"/vouchers?{name}={key}&{other}"
    on = ' aria-current="true"' if key == chosen else ""
    chips += f'<a class="o-chip" href="{esc(href)}"{on}>{esc(label)}</a>'
  return f'<div class="o-chips">{chips}</div>'


def _order_row(order_id: str, order: dict[str, Any]) -> str:
  lines = order["line_items"]
  first = lines[0]
  title = first["item"]["title"]
  if len(lines) > 1:
    title += f" + {len(lines) - 1} more"
  units = sum(line["quantity"]["total"] for line in lines)
  state, style = _order_state(order)
  payment = order.get("payment") or {}
  paid = money(payment.get("amount", 0))
  if payment.get("coins"):
    paid += f" + {payment['coins']} coins"
  if payment.get("status") == "refunded":
    paid = f"<s>{paid}</s>"
  codes = [
    code
    for line in lines
    if "voucher" in line
    for code in line["voucher"]["codes"]
  ]
  code = (
    f'<code class="o-code">{esc(codes[0])}</code>'
    if codes and style == "free"
    else ""
  )
  return f"""
    <li>
      <a class="o-row" href="/vouchers/{esc(order_id)}">
        {_thumb(first)}
        <span class="o-info">
          <b>{esc(title)}</b>
          <span class="o-meta">{esc(_placed(order.get("placed_at")))}
            · {units} voucher{"s" if units != 1 else ""}
            · ref <code>{esc(order_id[:8])}</code></span>
          <span class="o-meta">{provenance.badge(order)}</span>
        </span>
        <span class="o-right">
          <span class="o-paid">{paid}</span>
          <span class="pill {style}">{esc(state)}</span>
          {code}
        </span>
      </a>
    </li>"""


@router.get("/vouchers", include_in_schema=False)
async def vouchers_page(
  request: Request,
  shopper: storefront.ShopperSession,
  checkouts: Checkouts,
  transactions_session: storefront.TransactionsDb,
) -> Response:
  """List the orders the signed-in account has placed in this shop.

  Newest first, by month, one row per order; `?state=` and `?who=` narrow
  the list to a voucher state or to who placed the order.
  """
  if not shopper.user:
    return storefront.login_redirect("/vouchers")
  state_key = request.query_params.get("state", "all")
  who_key = request.query_params.get("who", "all")
  if state_key not in STATE_FILTERS:
    state_key = "all"
  if who_key not in WHO_FILTERS:
    who_key = "all"

  orders = []
  for order_id in await db.list_order_ids(
    transactions_session, shopper.user.id
  ):
    try:
      orders.append((order_id, await checkouts.get_order(order_id)))
    except ResourceNotFoundError:
      continue  # The shop was re-seeded since.
  orders.sort(key=lambda pair: pair[1].get("placed_at") or "", reverse=True)

  ready = sum(1 for _, o in orders if _order_state(o)[1] == "free")
  by_agent = sum(1 for _, o in orders if o.get("channel") != "web")
  spent = sum(
    (o.get("payment") or {}).get("amount", 0)
    for _, o in orders
    if (o.get("payment") or {}).get("status") != "refunded"
  )
  summary = (
    f"{len(orders)} order{'s' if len(orders) != 1 else ''} · {ready} ready"
    f" to use · {money(spent)} spent"
    + (f" · {by_agent} by your AI agent" if by_agent else "")
  )

  styles = STATE_FILTERS[state_key][1]
  channel = WHO_FILTERS[who_key][1]
  shown = [
    (order_id, order)
    for order_id, order in orders
    if (styles is None or _order_state(order)[1] in styles)
    and (
      channel is None or (order.get("channel") == "web") == (channel == "web")
    )
  ]
  groups = ""
  month = None
  for order_id, order in shown:
    m = _month(order.get("placed_at"))
    if m != month:
      if month is not None:
        groups += "</ul></section>"
      month = m
      groups += f'<section class="o-month"><h2>{esc(m)}</h2><ul>'
    groups += _order_row(order_id, order)
  if month is not None:
    groups += "</ul></section>"
  if not orders:
    groups = """
      <section class="blank">
        <p>Nothing bought yet. Everything you or your AI agent buys here
          shows up on this page.</p>
        <a class="primary" href="/">Browse the deals</a>
      </section>"""
  elif not shown:
    groups = """
      <section class="blank"><p>No orders match these filters.</p>
        <a class="secondary" href="/vouchers">Show everything</a></section>"""

  body = f"""
    <section class="orders">
      <header class="o-head">
        <div>
          <h1 class="title">My vouchers</h1>
          <p class="o-summary">{esc(summary)}</p>
        </div>
      </header>
      <div class="o-filters">
        {_filter_chips("state", STATE_FILTERS, state_key, f"who={who_key}")}
        {_filter_chips("who", WHO_FILTERS, who_key, f"state={state_key}")}
      </div>
      {groups}
    </section>"""
  return HTMLResponse(storefront.page("My vouchers", body, request, shopper))
