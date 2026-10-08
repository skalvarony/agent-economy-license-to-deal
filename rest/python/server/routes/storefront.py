"""Storefront routes: the shop as a person sees it in a browser.

`/` lists the deals and `/deals/{id}` shows one, read from the same catalog an
agent searches. A person adds deals to a cart and pays on the web
(routes/web_checkout.py); each deal also offers a request to hand to an agent.

This module also holds what every page shares: the page shell, the shopper's
cookies and the formatting helpers.
"""

import contextlib
import dataclasses
import datetime
import html
import pathlib
import re
from typing import Annotated, Any
import urllib.parse

import config
import db
import dependencies
from exceptions import ResourceNotFoundError
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from models import UnifiedCart
from routes import catalog
from services import visitor
from services import account_service
from services import coin_service
from services import payment_rail
from services import voucher_service
from services.cart_service import CartService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

_TEMPLATE = pathlib.Path(__file__).parent / "storefront.html"
_ASSETS = pathlib.Path(__file__).parent / "assets"
ASSET_TYPES = {
  "shop.css": "text/css",
  "shop.js": "text/javascript",
  "vendor/motion.js": "text/javascript",
  "fonts/inter.woff2": "font/woff2",
  "fonts/fraunces.woff2": "font/woff2",
  "fonts/bricolage.woff2": "font/woff2",
  "fonts/archivo.woff2": "font/woff2",
}
# A shop's display font: its family name and what to fall back to.
DISPLAY_FONTS = {
  "fraunces": ("Fraunces", "Georgia, serif"),
  "bricolage": ("Bricolage Grotesque", "system-ui, sans-serif"),
  "archivo": ("Archivo", "system-ui, sans-serif"),
}
# At or below this many places, the card says how few are left.
LOW_STOCK = 4
# Most units of one deal a person can put in the cart.
MAX_QUANTITY = 6
# A deal rated at least this is flagged on its card.
TOP_RATED = 4.8

ProductsDb = Annotated[AsyncSession, Depends(dependencies.get_products_db)]
TransactionsDb = Annotated[
  AsyncSession, Depends(dependencies.get_transactions_db)
]
Carts = Annotated[CartService, Depends(dependencies.get_cart_service)]


@dataclasses.dataclass
class Shopper:
  """Who is browsing: their cart, and their account if they are signed in."""

  cart_cookie: str
  session_cookie: str
  cart: UnifiedCart | None
  user: db.User | None
  coins: int = 0  # What the account's wallet holds in this shop

  @property
  def cart_count(self) -> int:
    """How many units are in the cart."""
    return sum(li.quantity for li in self.cart.line_items) if self.cart else 0


async def get_shopper(
  request: Request, carts: Carts, transactions_session: TransactionsDb
) -> Shopper:
  """Read the shopper's cart and account from this shop's cookies."""
  # Browsers share cookies across ports, so each shop names its own.
  port = request.url.port or 80
  cart_cookie, session_cookie = f"cart_{port}", f"session_{port}"
  cart = None
  if cart_id := request.cookies.get(cart_cookie):
    # No cart is found once it was paid for or the shop was re-seeded.
    with contextlib.suppress(ResourceNotFoundError):
      cart = await carts.get_cart(cart_id)
  user = await account_service.user_for_token(
    transactions_session, request.cookies.get(session_cookie)
  )
  coins = await db.coin_balance(transactions_session, user and user.email)
  return Shopper(cart_cookie, session_cookie, cart, user, coins)


ShopperSession = Annotated[Shopper, Depends(get_shopper)]


async def read_form(request: Request) -> dict[str, str]:
  """Read a posted HTML form."""
  fields = urllib.parse.parse_qs((await request.body()).decode())
  return {name: values[0] for name, values in fields.items()}


def local_path(path: str | None, fallback: str = "/") -> str:
  """Return a path inside this shop to send the browser to, never elsewhere."""
  if path and path.startswith("/") and not path.startswith(("//", "/\\")):
    return path
  return fallback


def login_redirect(back_to: str) -> RedirectResponse:
  """Send a signed-out shopper to sign in, and back here afterwards."""
  query = urllib.parse.urlencode({"next": back_to})
  return RedirectResponse(f"/login?{query}", status_code=303)


def esc(value: Any) -> str:
  """Escape a value for HTML."""
  return html.escape(str(value))


def money(cents: int) -> str:
  """Format cents as dollars, e.g. '$99', '$12.50' or '-$9.90'."""
  sign = "-" if cents < 0 else ""
  return f"{sign}${abs(cents) / 100:,.2f}".removesuffix(".00")


def day(iso: str) -> str:
  """Format an ISO time as a day, e.g. 'Sat 10 Oct'."""
  return datetime.datetime.fromisoformat(iso).strftime("%a %-d %b")


def moment(iso: str) -> str:
  """Format an ISO time with its hour, e.g. 'Sat 10 Oct, 10:00'."""
  return datetime.datetime.fromisoformat(iso).strftime("%a %-d %b, %H:%M")


def window(not_before: str, not_after: str) -> str:
  """Format a service window, giving the day once when it fits in a day."""
  start = datetime.datetime.fromisoformat(not_before)
  end = datetime.datetime.fromisoformat(not_after)
  if start.date() == end.date():
    return f"{moment(not_before)} to {end:%H:%M}"
  return f"{moment(not_before)} to {moment(not_after)}"


# The helpers below read a deal's terms in the shape line items carry them
# (voucher_service.terms), so the same text is shown before and after buying.


def refund_pill(cancellation: dict[str, Any]) -> str:
  """Render the refund policy as a small coloured label."""
  refundability = cancellation["refundability"]
  if refundability == "non_refundable":
    return '<span class="pill final">Final sale, non-refundable</span>'
  kind = (
    "Free cancellation" if refundability == "refundable" else "Partial refund"
  )
  if cancellation.get("refundable_until"):
    kind += f" until {day(cancellation['refundable_until'])}"
  elif cancellation.get("refund_days"):
    kind += f" for {cancellation['refund_days']} days"
  return f'<span class="pill free">{esc(kind)}</span>'


def refund_terms(cancellation: dict[str, Any]) -> str:
  """Spell out the refund policy in a sentence or two."""
  refundability = cancellation["refundability"]
  text = {
    "refundable": "Full refund if you cancel in time.",
    "partially_refundable": "Part of the price back if you cancel in time.",
    "non_refundable": "All sales are final. No refunds or changes.",
  }.get(refundability, refundability)
  if cancellation.get("refundable_until"):
    text += f" Cancel by {moment(cancellation['refundable_until'])}."
  elif cancellation.get("refund_days"):
    text += f" Cancel within {cancellation['refund_days']} days of buying."
  return text


_DAY_NAMES = {
  "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri",
  "sat": "Sat", "sun": "Sun",
}


def days_text(days: list[str]) -> str:
  """Say which days of the week, e.g. 'Every day' or 'Wed to Sun'."""
  order = list(_DAY_NAMES)
  chosen = [d for d in order if d in days]
  if len(chosen) == 7:
    return "Every day"
  if not chosen:
    return "No day"
  first, last = order.index(chosen[0]), order.index(chosen[-1])
  if last - first + 1 == len(chosen) and len(chosen) > 2:
    return f"{_DAY_NAMES[chosen[0]]} to {_DAY_NAMES[chosen[-1]]}"
  return " and ".join(_DAY_NAMES[d] for d in chosen) if len(chosen) <= 2 else (
    ", ".join(_DAY_NAMES[d] for d in chosen[:-1]) + f" and {_DAY_NAMES[chosen[-1]]}"
  )


def hours_text(booking: dict[str, Any]) -> str:
  """Say the calendar a deal is booked on: days, hours and slot length."""
  hours = ", ".join(
    part.strip().replace("-", " to ") for part in (booking.get("hours") or "").split(";")
  )
  return (
    f"{days_text(booking.get('days') or [])}, {hours},"
    f" every {booking.get('slot_minutes')} min"
  )


def slot_text(booking: dict[str, Any]) -> str:
  """Say the slot that was booked, e.g. 'Sat 10 Oct, 11:00 to 12:00'."""
  if booking.get("ends_at"):
    return window(booking["starts_at"], booking["ends_at"])
  return moment(booking["starts_at"])


def when_text(terms: dict[str, Any], short: bool = False) -> str:
  """Say when the service happens: the slot booked, its date, or any day."""
  booking = terms["service"].get("booking") or {}
  if booking.get("starts_at"):
    return moment(booking["starts_at"]) if short else slot_text(booking)
  if booking.get("required"):
    if short:
      return "Date and time at checkout"
    return f"A date and time you pick at checkout: {hours_text(booking)}"
  if span := terms["service"].get("window"):
    if short:
      return day(span["not_before"])
    return window(span["not_before"], span["not_after"])
  if short:
    return "Any day you book"
  voucher = terms["voucher"]
  if voucher.get("expires_at"):
    return f"Any day you book, until {day(voucher['expires_at'])}"
  return f"Any day you book, within {voucher.get('valid_days')} days of buying"


def validity_text(voucher: dict[str, Any]) -> str:
  """Say how long the voucher's promotional value lasts."""
  if voucher.get("expires_at"):
    text = f"Promotional value expires {moment(voucher['expires_at'])}."
  else:
    text = (
      f"Promotional value expires {voucher.get('valid_days')} days after"
      " purchase."
    )
  return text + " Amount paid never expires."


def booking_text(
  redemption: dict[str, Any], booking: dict[str, Any] | None = None
) -> str:
  """Say how the visit is booked: at checkout, or by contacting the venue."""
  booking = booking or {}
  if booking.get("starts_at"):
    held = "Your place is held" if booking.get("status") != "released" else "The place was released"
    return f"Booked for {slot_text(booking)}. {held}."
  if booking.get("required"):
    return (
      "You pick the date and time at checkout, and the shop holds your"
      f" place ({booking.get('capacity_per_slot', 1)} per slot)."
    )
  if not redemption.get("appointment_required"):
    return "No appointment needed."
  if contact := redemption.get("booking_contact"):
    return f"Appointment required. Book at {contact}."
  return "Appointment required. The contact to book comes with your voucher."


def limit_text(purchase: dict[str, Any]) -> str:
  """Say how many one person may buy, and when they may buy again."""
  if not purchase.get("limit_per_person"):
    return ""
  text = f"Limit {purchase['limit_per_person']} per person."
  if purchase.get("repurchase_days"):
    text += f" May be bought again every {purchase['repurchase_days']} days."
  return text


def fine_print(terms: dict[str, Any]) -> str:
  """Render a deal's terms as a list: what the buyer is agreeing to."""
  service = terms["service"]
  rows = [
    ("Includes", ", ".join(service.get("includes") or [])),
    ("When", when_text(terms)),
    ("Where", f"{service['merchant']}, {service['location']}"),
    ("Booking", booking_text(terms["redemption"], service.get("booking"))),
    ("How to redeem", terms["redemption"]["instructions"]),
    ("Cancellation", refund_terms(terms["cancellation"])),
    ("Validity", validity_text(terms["voucher"])),
    ("Limits", limit_text(terms.get("purchase") or {})),
  ]
  return (
    '<dl class="fine">'
    + "".join(
      f"<dt>{label}</dt><dd>{esc(text)}</dd>" for label, text in rows if text
    )
    + "</dl>"
  )


def gallery(listing_id: str) -> list[str]:
  """Return the URLs of a deal's photos: the main one, then the extras."""
  names = [listing_id, f"{listing_id}_2", f"{listing_id}_3"]
  return [f"/images/{name}.jpg" for name in names if catalog.image_file(name)]


def thumb(listing_id: str, morph: bool = False) -> str:
  """Render a deal's photo, or an empty frame when it has none.

  With `morph`, the browser animates the photo between the pages that show
  it. A page may only mark one photo per deal that way.
  """
  image = (
    f'<img src="/images/{esc(listing_id)}.jpg" alt="" loading="lazy">'
    if catalog.image_file(listing_id)
    else ""
  )
  name = f' style="view-transition-name: photo-{esc(listing_id)}"'
  return f'<div class="photo"{name if morph else ""}>{image}</div>'


def notice_banner(text: str, kind: str = "warn") -> str:
  """Render a message at the top of a page."""
  return (
    f'<p class="notice {kind}" role="status">{esc(text)}</p>' if text else ""
  )


def logo_svg(shop: dict) -> str:
  """Return the shop's mark: logo.svg in its folder, else its initial."""
  if config.FLAGS.is_parsed() and config.FLAGS.shop_dir:
    path = pathlib.Path(config.FLAGS.shop_dir) / "logo.svg"
    if path.is_file():
      return path.read_text(encoding="utf-8")
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    f'<rect width="32" height="32" rx="9" fill="{shop["accent"]}"/>'
    '<text x="16" y="23" font-family="system-ui,sans-serif" font-size="19"'
    f' font-weight="700" text-anchor="middle" fill="white">'
    f"{esc(shop['name'][:1])}</text></svg>"
  )


def agent_notice(request: Request) -> str:
  """Tell a visitor that declares itself an agent where agents buy."""
  who = visitor.classify(request.headers)
  if not who.declared:
    return ""
  return f"""
    <aside class="notice agent-door" role="note">
      You're browsing as <b>{esc(who.agent)}</b>. Agents buy through this
      shop's agent door, where the customer's approval is checked:
      <a href="/.well-known/ucp">/.well-known/ucp</a>.
    </aside>"""


def agent_door(
  request: Request, shopper: Shopper, wants_json: bool = False
) -> Response:
  """Refuse a web checkout to a declared agent, pointing it to UCP."""
  who = visitor.classify(request.headers)
  message = (
    "Agents buy through the agent door (UCP), where the shop checks who the"
    " agent is and that the customer approved the total."
  )
  if wants_json:
    return JSONResponse(
      {"code": "agent_door", "message": message, "agent": who.agent},
      status_code=403,
    )
  body = f"""
    <section class="blank lost">
      <p class="big-number">403</p>
      <h1>This checkout is for people</h1>
      <p>You're browsing as <b>{esc(who.agent or "an agent")}</b>. {message}
        The door is at <a href="/.well-known/ucp">/.well-known/ucp</a>.</p>
      <a class="primary" href="/">Back to the deals</a>
    </section>"""
  return HTMLResponse(
    page("Agents buy elsewhere", body, request, shopper), status_code=403
  )


def page(title: str, body: str, request: Request, shopper: Shopper) -> str:
  """Wrap a page body in the shop's shell: header, styles and footer."""
  shop = config.get_shop()
  body = agent_notice(request) + body
  count = shopper.cart_count
  if shopper.user:
    first_name = shopper.user.full_name.split()[0]
    wallet = ""
    if shopper.coins:
      wallet = (
        f'<a class="coins" href="/account" title="Your coins in this shop">'
        f"{shopper.coins} coins</a>"
      )
    account = (
      f'{wallet}<a class="nav" href="/vouchers">My vouchers</a>'
      f'<a class="nav who" href="/account">{esc(first_name)}</a>'
    )
  else:
    account = '<a class="nav" href="/login">Sign in</a>'
  family, fallback = DISPLAY_FONTS.get(shop["font"], ("", ""))
  font_face = display = ""
  if family:
    font_face = (
      f'@font-face {{ font-family: "{family}"; font-weight: 100 900;'
      f" font-display: swap; src: url(/assets/fonts/{shop['font']}.woff2)"
      ' format("woff2"); }'
    )
    display = f'--display: "{family}", {fallback};'
  return (
    _TEMPLATE.read_text(encoding="utf-8")
    .replace("{{TITLE}}", esc(title))
    .replace("{{NAME}}", esc(shop["name"]))
    .replace("{{MARK}}", '<img class="mark" src="/logo.svg" alt="">')
    .replace("{{CITY}}", esc(shop["city"]))
    .replace("{{ACCENT}}", esc(shop["accent"]))
    .replace("{{DISPLAY}}", display)
    .replace("{{FONT_FACE}}", font_face)
    .replace("{{TAGLINE}}", esc(shop["tagline"]))
    .replace("{{FAVICON}}", "/logo.svg")
    .replace("{{ACCOUNT}}", account)
    .replace("{{CART_COUNT}}", str(count))
    .replace("{{CART_BADGE_HIDDEN}}", "" if count else " hidden")
    .replace("{{BASE_URL}}", esc(str(request.base_url).rstrip("/")))
    .replace("{{PAYMENTS}}", _payments_note())
    .replace("{{BODY}}", body)
  )


def _payments_note() -> str:
  """Say in the footer what the payments are, for the rail in use."""
  if payment_rail.rail_name() == payment_rail.StripeRail.name:
    if payment_rail.StripeRail().mode == "test":
      return (
        "payments run through Stripe in test mode, and no real money moves."
      )
    return "payments run through Stripe."
  return "payments are simulated, and no real money moves."


def not_found(
  request: Request, shopper: Shopper, what: str = "page"
) -> HTMLResponse:
  """Render the page for something this shop doesn't have."""
  body = f"""
    <section class="blank lost">
      <p class="big-number">404</p>
      <h1>We can't find that {esc(what)}</h1>
      <p>It may have sold out for good, or the link is wrong.</p>
      <a class="primary" href="/">Browse deals</a>
    </section>"""
  return HTMLResponse(
    page("Not found", body, request, shopper), status_code=404
  )


def percent_off(price: int, list_price: int | None) -> int:
  """Return the saving against the list price, as a whole percentage."""
  if not list_price or list_price <= price:
    return 0
  return round((1 - price / list_price) * 100)


def _offer_off(offer: catalog.Offer) -> int:
  list_price = offer.option.list_price if offer.option else None
  return percent_off(offer.product.price, list_price)


def _on_sale(listing: catalog.Listing) -> list[catalog.Offer]:
  """Return the offers that can be bought now, or all if none can."""
  return [o for o in listing.offers if o.stock > 0] or listing.offers


def lead_offer(listing: catalog.Listing) -> catalog.Offer:
  """Return the offer a listing is shown by: the cheapest one on sale."""
  return min(_on_sale(listing), key=lambda offer: offer.product.price)


def _best_off(listing: catalog.Listing) -> int:
  return max(_offer_off(offer) for offer in _on_sale(listing))


def stock_note(stock: int) -> str:
  """Say how much is left without giving the count, unless it is running out."""
  if stock <= 0:
    return "Sold out"
  return f"Only {stock} left" if stock <= LOW_STOCK else "In stock"


def _rating(deal: db.Deal) -> str:
  if not deal.rating:
    return ""
  bought = f" · {deal.bought:,}+ sold" if deal.bought else ""
  return (
    f'<p class="rating"><span class="stars" style="--rating:{deal.rating}"'
    f' aria-hidden="true">★★★★★</span> <b>{deal.rating}</b>'
    f' <span class="count">({deal.reviews or 0:,})</span>{bought}</p>'
  )


def _photo(listing: catalog.Listing, morph: bool = False) -> str:
  """Render a deal's photo with its discount and availability badges."""
  off = _best_off(listing)
  badges = f'<span class="off">-{off}%</span>' if off else ""
  if listing.stock <= 0:
    badges += '<span class="stock out">Sold out</span>'
  elif listing.stock <= LOW_STOCK:
    badges += f'<span class="stock">Only {listing.stock} left</span>'
  return thumb(listing.id, morph).replace("</div>", f"{badges}</div>")


def _offer_price(offer: catalog.Offer) -> str:
  """Render an offer's price, with the list price struck when it is lower."""
  list_price = offer.option.list_price if offer.option else None
  was = f"<s>{money(list_price)}</s> " if _offer_off(offer) else ""
  return f"{was}<strong>{money(offer.product.price)}</strong>"


def _price(listing: catalog.Listing) -> str:
  """Render a listing's price: its cheapest offer, 'From' when they differ."""
  varies = len({offer.product.price for offer in listing.offers}) > 1
  return (
    f'<p class="price">{"<small>From</small> " if varies else ""}'
    f"{_offer_price(lead_offer(listing))}</p>"
  )


HEART = (
  '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">'
  '<path d="M12 20.5s-7.5-4.6-7.5-10.1A4.4 4.4 0 0 1 12 7.6a4.4 4.4 0 0 1'
  ' 7.5 2.8c0 5.5-7.5 10.1-7.5 10.1Z"/></svg>'
)


def _save_button(listing: catalog.Listing) -> str:
  """Render the heart that keeps a deal in the browser's saved list."""
  return (
    f'<button class="fav" type="button" data-fav="{esc(listing.id)}"'
    f' aria-pressed="false" aria-label="Save {esc(listing.title)}">'
    f"{HEART}</button>"
  )


def card(listing: catalog.Listing, position: int = 0) -> str:
  """Render a deal as a card for the grid."""
  deal = listing.deal
  where = rating = pill = quick = ""
  category = "other"
  searchable = listing.title
  if deal:
    area = deal.location.rsplit(",", 1)[-1].strip()
    where = f'<p class="where">{esc(deal.merchant)} · {esc(area)}</p>'
    rating = _rating(deal)
    pill = refund_pill(voucher_service.terms(deal)["cancellation"])
    category = deal.category
    searchable = f"{listing.title} {deal.merchant} {deal.category}"
  flag = ""
  if deal and (deal.rating or 0) >= TOP_RATED:
    flag = '<span class="flag">Top rated</span>'
  if listing.stock > 0 and len(listing.offers) == 1:
    quick = f"""
      <form class="quick" method="post" action="/cart/add" data-async="cart">
        <input type="hidden" name="product_id"
               value="{esc(listing.offers[0].product.id)}">
        <button data-busy="Adding…">Add to cart</button>
      </form>"""
  elif listing.stock > 0:
    # Several options: the shopper picks one on the deal's page.
    quick = f"""
      <div class="quick">
        <a href="/deals/{esc(listing.id)}">Choose an option</a></div>"""
  return f"""
    <article class="card{" sold" if listing.stock <= 0 else ""}"
             data-id="{esc(listing.id)}" data-category="{esc(category)}"
             data-search="{esc(searchable.lower())}"
             data-price="{lead_offer(listing).product.price}"
             data-off="{_best_off(listing)}"
             data-rating="{(deal.rating if deal else 0) or 0}"
             data-position="{position}" style="--i: {position}">
      <a class="cover" href="/deals/{esc(listing.id)}">
        {_photo(listing, morph=True)}
        <div class="body">
          {flag}
          {where}
          <h3>{esc(listing.title)}</h3>
          {rating}
          {_price(listing)}
          {pill}
        </div>
      </a>
      {_save_button(listing)}
      {quick}
    </article>"""


def _hero(shop: dict, listings: list[catalog.Listing]) -> str:
  """Render the banner, featuring the available deal with the biggest saving."""
  live = [listing for listing in listings if listing.stock > 0]
  best_off = max((_best_off(listing) for listing in live), default=0)
  stats = f"<span>{len(live)} deals live</span>"
  if best_off:
    stats += f"<span>Up to {best_off}% off</span>"
  featured = ""
  if live:
    top = max(live, key=_best_off)
    featured = f"""
      <a class="featured" href="/deals/{esc(top.id)}">
        {_photo(top)}
        <span class="label">Top deal</span>
        <span class="name">{esc(top.title)}</span>
        {_price(top)}
      </a>"""
  return f"""
    <section class="hero">
      <span class="glow" aria-hidden="true"></span>
      <div class="pitch">
        <p class="eyebrow">{esc(shop["tagline"])}</p>
        <h1>{esc(shop["hero"] or shop["name"])}</h1>
        <p class="sub">{esc(shop["hero_sub"])}</p>
        <p class="stats">{stats}</p>
      </div>
      {featured}
    </section>"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(
  request: Request,
  shopper: ShopperSession,
  products_session: ProductsDb,
  transactions_session: TransactionsDb,
) -> str:
  """List the shop's deals, with a search box and category filters."""
  shop = config.get_shop()
  listings = await catalog.load_catalog(products_session, transactions_session)
  categories = sorted({li.deal.category for li in listings if li.deal})
  chips = '<button class="chip on" data-filter="">All deals</button>' + "".join(
    f'<button class="chip" data-filter="{esc(c)}">{esc(c.title())}</button>'
    for c in categories
  )
  cards = "".join(card(listing, n) for n, listing in enumerate(listings))
  body = f"""
    {_hero(shop, listings)}
    <div class="toolbar">
      <nav class="chips" aria-label="Categories">{chips}
        <button class="chip" data-filter="saved">Saved</button></nav>
      <label class="sort">Sort by
        <select id="sort">
          <option value="position">Recommended</option>
          <option value="price">Price: low to high</option>
          <option value="off">Biggest saving</option>
          <option value="rating">Top rated</option>
        </select></label>
    </div>
    <h2 class="section">Deals in {esc(shop["city"] or "town")}
      <span id="count">{len(listings)}</span></h2>
    <section class="grid" id="deals">{cards}</section>
    <p class="empty" id="empty" hidden>No deals match. Try another one.</p>"""
  return page(shop["name"], body, request, shopper)


async def shop_promo(transactions_session: AsyncSession) -> db.Discount | None:
  """Return the promo code this shop advertises, if it has one."""
  code = config.get_shop().get("promo_code")
  return await db.get_discount(transactions_session, code) if code else None


def promo_price(price: int, promo: db.Discount | None) -> int:
  """Return a price with the promo code applied."""
  if not promo:
    return price
  if promo.type == "percentage":
    return price - int(price * promo.value / 100)
  return max(price - promo.value, 0)


def coins_line(price: int) -> str:
  """Say how many coins buying at this price gives back, if the shop does."""
  earned = coin_service.earned_for(price)
  if not earned:
    return ""
  return f"Earn {earned} coin{'s' if earned != 1 else ''} back"


def _promo_line(price: int, promo: db.Discount | None) -> str:
  if not promo:
    return ""
  return (
    f"<b>{money(promo_price(price, promo))}</b> with code"
    f" <code>{esc(promo.code)}</code>"
  )


def most_units(offer: catalog.Offer, deal: db.Deal | None) -> int:
  """Return how many units of an offer one shopper can put in the cart."""
  limit = deal.limit_per_person if deal and deal.limit_per_person else None
  return min(offer.stock, MAX_QUANTITY, limit or MAX_QUANTITY)


def _includes(listing: catalog.Listing, chosen: catalog.Offer) -> str:
  """Render what each option gives; only the chosen one's block is shown."""
  blocks = ""
  for offer in listing.offers:
    option = offer.option
    if not option or not (option.description or option.includes):
      continue
    items = "".join(f"<li>{esc(item)}</li>" for item in option.includes or [])
    blocks += f"""
      <div data-includes="{esc(offer.product.id)}"
           {"" if offer is chosen else "hidden"}>
        <h3>{esc(option.title)}</h3>
        <p>{esc(option.description or "")}</p>
        <ul class="ticks">{items}</ul>
      </div>"""
  return f"<section><h2>What you get</h2>{blocks}</section>" if blocks else ""


def _gallery(listing: catalog.Listing) -> str:
  """Render a deal's main photo, with the others to switch to below it."""
  photos = gallery(listing.id)
  if len(photos) < 2:
    return _photo(listing, morph=True)
  thumbs = "".join(
    f'<button type="button" data-photo="{esc(url)}"'
    f"{' aria-current=true' if n == 0 else ''}"
    f' aria-label="Photo {n + 1}"><img src="{esc(url)}" alt=""></button>'
    for n, url in enumerate(photos)
  )
  return f"""
    <div class="gallery">
      {_photo(listing, morph=True)}
      <div class="thumbs">{thumbs}</div>
    </div>"""


def _reviews(deal: db.Deal, reviews: list[db.DealReview]) -> str:
  """Render a deal's rating and what customers wrote."""
  if not reviews:
    return ""
  items = ""
  for review in reviews:
    when = datetime.date.fromisoformat(review.date).strftime("%-d %b %Y")
    initial = review.author[:1]
    reply = ""
    if review.reply:
      reply = f"""
        <p class="reply"><b>{esc(deal.merchant)} replied</b>
          {esc(review.reply)}</p>"""
    items += f"""
      <li class="review">
        <header>
          <span class="avatar" aria-hidden="true">{esc(initial)}</span>
          <b>{esc(review.author)}</b>
          <span class="stars" style="--rating:{review.rating}"
                aria-label="{review.rating} out of 5">★★★★★</span>
          <time>{esc(when)}</time>
        </header>
        <p>{esc(review.text)}</p>
        {reply}
      </li>"""
  return f"""
    <section class="reviews"><h2>What customers say</h2>
      <p class="score"><strong>{deal.rating}</strong>
        <span class="stars" style="--rating:{deal.rating}"
              aria-hidden="true">★★★★★</span>
        <span>{deal.reviews or 0:,} ratings</span></p>
      <ul class="review-list">{items}</ul>
    </section>"""


def _option_picker(
  listing: catalog.Listing, chosen: catalog.Offer, promo: db.Discount | None
) -> str:
  """Render a deal's options, each with its own price and availability.

  Each choice carries what the buy box shows for it, so the page's script can
  switch between them without asking the server.
  """
  choices = ""
  for offer in listing.offers:
    off = _offer_off(offer)
    note = (
      "" if stock_note(offer.stock) == "In stock" else stock_note(offer.stock)
    )
    choices += f"""
      <label class="option{" out" if offer.stock <= 0 else ""}">
        <input type="radio" name="product_id" value="{esc(offer.product.id)}"
               data-price="{esc(_offer_price(offer))}"
               data-cents="{offer.product.price}"
               data-save="{f"You save {off}%" if off else ""}"
               data-left="{esc(stock_note(offer.stock))}"
               data-max="{most_units(offer, listing.deal)}"
               data-promo="{esc(_promo_line(offer.product.price, promo))}"
               data-coins="{esc(coins_line(offer.product.price))}"
               data-title="{esc(offer.product.title)}"
               {"checked" if offer is chosen else ""}
               {"disabled" if offer.stock <= 0 else ""}>
        <span class="name">{esc(offer.option.title)}
          <small>{esc(note)}</small></span>
        <span class="cost">{_offer_price(offer)}</span>
      </label>"""
  label = listing.deal.option_label or "Option"
  return (
    f'<fieldset class="options"><legend>{esc(label)}</legend>'
    f"{choices}</fieldset>"
  )


@router.get("/deals/{id}", response_class=HTMLResponse, include_in_schema=False)
async def deal_page(
  request: Request,
  shopper: ShopperSession,
  listing_id: Annotated[str, Path(..., alias="id")],
  products_session: ProductsDb,
  transactions_session: TransactionsDb,
) -> HTMLResponse:
  """Show one deal with its options, fine print and the two ways to buy it."""
  listings = await catalog.load_catalog(products_session, transactions_session)
  listing = next((li for li in listings if li.id == listing_id), None)
  if not listing:
    return not_found(request, shopper, "deal")
  deal = listing.deal
  shop = config.get_shop()
  base_url = str(request.base_url).rstrip("/")

  promo = await shop_promo(transactions_session)
  chosen = lead_offer(listing)
  about = need_to_know = where = rating = pill = highlights = ""
  merchant = reviews = ""
  crumb = "Deal"
  if deal:
    terms = voucher_service.terms(deal)
    points = "".join(
      f"<li>{esc(point)}</li>" for point in deal.highlights or []
    )
    highlights = f'<ul class="ticks highlights">{points}</ul>' if points else ""
    if deal.about_merchant:
      merchant = f"""
        <section><h2>About {esc(deal.merchant)}</h2>
          <p>{esc(deal.about_merchant)}</p>
          <p class="where">{esc(deal.location)}</p></section>"""
    reviews = _reviews(deal, await db.list_reviews(products_session, deal.id))
    crumb = deal.category.title()
    where = f'<p class="where">{esc(deal.merchant)} · {esc(deal.location)}</p>'
    rating = _rating(deal)
    pill = refund_pill(terms["cancellation"])
    about = f"""
      <section><h2>About this deal</h2>
        <p>{esc(deal.description)}</p></section>
      {_includes(listing, chosen)}"""
    need_to_know = f"""
      <section><h2>Need to know</h2>{fine_print(terms)}</section>"""

  off = _offer_off(chosen)
  if listing.stock > 0:
    if len(listing.offers) > 1:
      picker = _option_picker(listing, chosen, promo)
    else:
      picker = (
        '<input type="hidden" name="product_id"'
        f' value="{esc(chosen.product.id)}">'
      )
    # The request names the option; the script rewrites it when another is
    # picked.
    ask = (
      f'Buy "{{title}}" from {shop["name"]} ({base_url}), item {{id}}. Show'
      " me the price, the date and the refund terms before you pay."
    )
    shown = ask.format(title=chosen.product.title, id=chosen.product.id)
    most = most_units(chosen, deal)
    buy = f"""
      <form class="add" method="post" action="/cart/add" data-async="cart">
        {picker}
        <div class="row">
          <div class="stepper" data-stepper>
            <button type="button" data-step="-1"
                    aria-label="One less">−</button>
            <input name="quantity" type="number" value="1" min="1"
                   max="{most}" aria-label="Quantity">
            <button type="button" data-step="1"
                    aria-label="One more">+</button>
          </div>
          <button class="primary" type="submit"
                  data-busy="Adding…">Add to cart</button>
        </div>
      </form>
      <details class="agent">
        <summary>Or buy with your AI agent</summary>
        <p>Hand your agent this request and approve the purchase when it
          asks.</p>
        <pre id="ask" data-template="{esc(ask)}">{esc(shown)}</pre>
        <button class="secondary" type="button"
                data-copy-from="ask">Copy request</button>
        <p class="tech">For agents: profile <code>/.well-known/ucp</code>,
          item <code data-option-id>{esc(chosen.product.id)}</code></p>
      </details>"""
  else:
    buy = '<p class="soldout">No places left for this deal.</p>'

  others = [li for li in listings if li.id != listing.id and li.stock > 0][:3]
  more = ""
  if others:
    more = f"""
      <section class="more">
        <h2 class="section">More from {esc(shop["name"])}</h2>
        <div class="grid">{"".join(card(other) for other in others)}</div>
      </section>"""

  body = f"""
    <nav class="crumbs"><a href="/">All deals</a> › {esc(crumb)}</nav>
    <div class="detail">
      <article class="main">
        {_gallery(listing)}
        <div class="heading">
          <h1>{esc(listing.title)}</h1>
          {_save_button(listing)}
        </div>
        {where}
        {rating}
        {highlights}
        {about}
        {need_to_know}
        {merchant}
        {reviews}
      </article>
      <aside class="buy">
        <p class="price" data-option-price
           data-cents="{chosen.product.price}">{_offer_price(chosen)}</p>
        <span class="save" data-option-save>{
    f"You save {off}%" if off else ""
  }</span>
        <p class="promo" data-option-promo>{
    _promo_line(chosen.product.price, promo)
  }</p>
        <p class="earn" data-option-coins>{
    esc(coins_line(chosen.product.price))
  }</p>
        <p class="left" data-option-left>{esc(stock_note(chosen.stock))}</p>
        {pill}
        {buy}
      </aside>
    </div>
    {more}"""
  return HTMLResponse(
    page(f"{listing.title} · {shop['name']}", body, request, shopper)
  )


@router.get("/logo.svg", include_in_schema=False)
async def logo() -> Response:
  """Serve the shop's mark, for its own pages and for the agent's."""
  return Response(
    logo_svg(config.get_shop()),
    media_type="image/svg+xml",
    headers={"Cache-Control": "max-age=3600"},
  )


@router.get("/assets/{name:path}", include_in_schema=False)
async def asset(name: str) -> FileResponse:
  """Serve the storefront's stylesheet, scripts and fonts."""
  if name not in ASSET_TYPES:
    raise HTTPException(status_code=404, detail="Asset not found")
  # The shop's own files change while it is being built; the rest don't.
  ours = name in ("shop.css", "shop.js")
  return FileResponse(
    _ASSETS / name,
    media_type=ASSET_TYPES[name],
    headers={"Cache-Control": "no-cache" if ours else "max-age=86400"},
  )


@router.get("/images/{name}", include_in_schema=False)
async def image(name: str) -> FileResponse:
  """Serve a deal's photo from the shop's images folder."""
  match = re.fullmatch(r"([\w-]+)\.jpg", name)
  path = catalog.image_file(match.group(1)) if match else None
  if not path:
    raise HTTPException(status_code=404, detail="Image not found")
  return FileResponse(path, media_type="image/jpeg")
