"""The shops' console: one place to see and act on what the three shops sold.

It is the marketplace's side. Every call to a shop carries the merchant secret,
and the agent is asked, when an order came through it, for the approval it
recorded and the conversation that led to the purchase.

  GET  /                                  the page
  GET  /api/shops                         the shops it manages
  GET  /api/{shop}/orders                 every order, newest first
  GET  /api/{shop}/stats                  the numbers behind them
  GET  /api/{shop}/orders/{id}            one order, its events, its payment
                                          as the rail sees it, the agent side
                                          (opening it marks it seen)
  POST /api/{shop}/orders/{id}/review     {"state": "handled" | "open"}
  POST /api/{shop}/orders/{id}/cancel     cancel the purchase and refund it
  GET  /api/{shop}/events                 the shop's ledger
  GET  /api/{shop}/inventory              each option's codes by status
  GET  /api/{shop}/wallets/{email}        a wallet and its movements
  POST /api/{shop}/wallets/grant          {"email", "coins"}
  POST /api/{shop}/accounts               {"full_name", "email", "password"}
  PUT  /api/{shop}/accounts/{email}/password   {"password"}
  POST /api/{shop}/booking-fee            {"amount"} in cents; 0 removes it

`{shop}` is a shop's id, or `all` for the three together (orders and stats).

Each order carries an `attention` state for the merchant's inbox: `new`
(nobody opened it), `seen` (opened, nothing done) or `handled` (an action
was taken on it, or someone marked it handled). What the console itself
knows (opened, marked) lives in reviews.json under CONSOLE_RUN_DIR, shared
by everyone who uses the console.

SIMULATION_SECRET is the merchant secret the shops expect. SHOP_<ID>_URL
and AGENT_URLS (profile=address, comma-separated) override console.json.
The team signs in on /login (teamlogin.py): LOGIN_USERS or AGENT_USER +
AGENT_PASSWORD_HASH name who may; with neither set the console runs open.
"""

import contextlib
import collections
import datetime
import json
import os
import pathlib
import time
import re
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx
import teamlogin

HERE = pathlib.Path(__file__).parent
CONFIG = pathlib.Path(os.environ.get("CONSOLE_CONFIG", HERE / "console.json"))
SECRET = os.environ.get("SIMULATION_SECRET", "demo-secret")
RUN_DIR = pathlib.Path(
  os.environ.get("CONSOLE_RUN_DIR", HERE.parent / ".run" / "console")
)
TIMEOUT_SECONDS = 20


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


def load_settings() -> dict[str, Any]:
  """Read console.json and apply the environment's overrides."""
  settings = json.loads(CONFIG.read_text())
  for shop in settings["shops"]:
    shop["url"] = os.environ.get(
      f"SHOP_{shop['id'].upper()}_URL", shop["url"]
    ).rstrip("/")
  # The agents, by the profile URL an order records: {profile: address}.
  # AGENT_URLS=profile=address,profile=address overrides console.json.
  agents = {
    a["profile"]: a["url"].rstrip("/") for a in settings.get("agents", [])
  }
  if os.environ.get("AGENT_URLS"):
    agents = {}
    for pair in os.environ["AGENT_URLS"].split(","):
      profile, _, url = pair.strip().partition("=")
      if profile and url:
        agents[profile] = url.rstrip("/")
  settings["agents"] = agents
  return settings


class Shops:
  """The console's calls to the shops and to the agent."""

  def __init__(
    self,
    settings: dict[str, Any],
    http: httpx.AsyncClient,
    reviews: "Reviews | None" = None,
  ):
    """Keep the addresses, the client and the merchant's marks."""
    self.by_id = {shop["id"]: shop for shop in settings["shops"]}
    self.agents: dict[str, str] = settings["agents"]
    self.http = http
    self.reviews = reviews or Reviews(RUN_DIR / "reviews.json")

  def shop(self, shop_id: str) -> dict[str, Any]:
    """Return a shop's settings, or 404."""
    if shop_id not in self.by_id:
      raise HTTPException(status_code=404, detail=f"No shop {shop_id}")
    return self.by_id[shop_id]

  def chosen(self, shop_id: str) -> list[dict[str, Any]]:
    """Return the shops `shop_id` names: one, or every one for `all`."""
    if shop_id == "all":
      return list(self.by_id.values())
    return [self.shop(shop_id)]

  async def orders(self, shop_id: str) -> list[dict[str, Any]]:
    """Every order of the chosen shops, summarised, newest first."""
    rows = []
    for shop in self.chosen(shop_id):
      rows += [
        summarize(order, shop, self.reviews.get(shop["id"], order["id"]))
        for order in await self.call(shop["id"], "GET", "/orders")
      ]
    rows.sort(key=lambda row: row.get("placed_at") or "", reverse=True)
    return rows

  async def events(self, shop_id: str) -> list[dict[str, Any]]:
    """Return the ledger lines of the chosen shops (the file is shared)."""
    names = {shop["name"] for shop in self.chosen(shop_id)}
    first = self.chosen(shop_id)[0]["id"]
    lines = await self.call(first, "GET", "/ledger")
    return [line for line in lines if line.get("seller") in names]

  async def call(
    self, shop_id: str, method: str, path: str, body: Any = None
  ) -> Any:
    """Call a shop as the merchant. A refusal becomes the same HTTP error."""
    shop = self.shop(shop_id)
    try:
      response = await self.http.request(
        method,
        shop["url"] + path,
        json=body,
        headers={"Simulation-Secret": SECRET},
      )
    except httpx.HTTPError as error:
      raise HTTPException(
        status_code=502, detail=f"{shop['name']} did not answer: {error}"
      ) from error
    if response.status_code >= 400:
      raise HTTPException(
        status_code=response.status_code, detail=_problem(response)
      )
    return response.json()

  async def ask_agent(self, profile: str | None, path: str) -> Any | None:
    """Ask the agent with that profile; None when unknown or silent."""
    url = self.agents.get(profile or "")
    if not url:
      return None
    try:
      response = await self.http.get(
        url + path, headers={"Simulation-Secret": SECRET}
      )
    except httpx.HTTPError:
      return None
    return response.json() if response.status_code == 200 else None


def _problem(response: httpx.Response) -> str:
  try:
    body = response.json()
  except ValueError:
    return response.text[:200] or response.reason_phrase
  detail = body.get("detail") if isinstance(body, dict) else None
  if isinstance(detail, str):
    return detail
  if isinstance(detail, dict):
    errors = detail.get("errors") or [{}]
    return str(errors[0].get("message") or detail)
  messages = body.get("messages") if isinstance(body, dict) else None
  if messages:
    return str(messages[0].get("content") or messages[0])
  return str(body)[:200]


class Reviews:
  """What the console knows about each order: opened, or marked handled.

  One JSON file, keyed by `shop:order_id`, so the whole team shares it.
  """

  def __init__(self, path: pathlib.Path) -> None:
    """Read the file, if there is one."""
    self.path = path
    self.marks: dict[str, dict[str, Any]] = {}
    if path.is_file():
      self.marks = json.loads(path.read_text())

  def get(self, shop_id: str, order_id: str) -> dict[str, Any] | None:
    """Return the mark on an order, or None if nobody touched it."""
    return self.marks.get(f"{shop_id}:{order_id}")

  def set(self, shop_id: str, order_id: str, state: str) -> dict[str, Any]:
    """Mark an order `seen`, `handled` or `open` (seen again) and save."""
    mark = {
      "state": state,
      "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    self.marks[f"{shop_id}:{order_id}"] = mark
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.path.write_text(json.dumps(self.marks, indent=1))
    return mark

  def seen(self, shop_id: str, order_id: str) -> None:
    """Take an opened order out of `new`; a handled one stays handled."""
    if self.get(shop_id, order_id) is None:
      self.set(shop_id, order_id, "seen")


def acted_on(order: dict[str, Any]) -> bool:
  """Whether the merchant side already did something to the order."""
  if (order.get("payment") or {}).get("status") == "refunded":
    return True
  return any(
    (line.get("redemption") or {}).get("status", "unredeemed") != "unredeemed"
    for line in order.get("line_items") or []
  )


def attention(order: dict[str, Any], mark: dict[str, Any] | None) -> str:
  """Return `new`, `seen` or `handled` for the merchant's inbox."""
  if (mark or {}).get("state") == "handled" or acted_on(order):
    return "handled"
  return "seen" if mark else "new"


def summarize(
  order: dict[str, Any],
  shop: dict[str, Any],
  mark: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Reduce an order to the row the console lists."""
  payment = order.get("payment") or {}
  lines = order.get("line_items") or []
  return {
    "attention": attention(order, mark),
    "reviewed_at": (mark or {}).get("at"),
    "id": order["id"],
    "shop": shop["id"],
    "shop_name": shop["name"],
    "shop_color": shop["color"],
    "placed_at": order.get("placed_at"),
    "buyer": order.get("buyer") or {},
    "channel": order.get("channel"),
    "agent": order.get("agent"),
    "signature": (order.get("signature") or {}).get("status"),
    "visit": order.get("visit"),
    "payment": {
      "amount": payment.get("amount", 0),
      "coins": payment.get("coins", 0),
      "coins_earned": payment.get("coins_earned", 0),
      "status": payment.get("status"),
      "rail": payment.get("rail"),
      # Stripe's fraud check, when the rail has one: `review` marks an
      # order the merchant should look at.
      "risk": payment.get("risk"),
    },
    "items": [
      {
        "title": line["item"]["title"],
        "quantity": (line.get("quantity") or {}).get("total", 1)
        if isinstance(line.get("quantity"), dict)
        else line.get("quantity", 1),
        "voucher": (line.get("voucher") or {}).get("status"),
        "redemption": (line.get("redemption") or {}).get("status"),
        "codes": (line.get("voucher") or {}).get("codes") or [],
      }
      for line in lines
    ],
  }


def stats(
  orders: list[dict[str, Any]], events: list[dict[str, Any]]
) -> dict[str, Any]:
  """Work the management numbers out of the orders and the ledger.

  Money is what the card paid, in cents; coins are counted apart. An order
  counts as refunded by its payment status; its card amount then moves from
  `net` to `refunded`. Declines come from the ledger, which keeps a line per
  refused payment.
  """
  gross = refunded = coins_spent = coins_earned = 0
  doors = collections.Counter()
  signatures = collections.Counter()
  rails = collections.Counter()
  redemption = collections.Counter()
  inbox = collections.Counter()
  by_day: dict[str, dict[str, int]] = collections.defaultdict(
    lambda: {"orders": 0, "amount": 0, "refunded": 0}
  )
  for order in orders:
    payment = order["payment"]
    amount = payment.get("amount") or 0
    gross += amount
    coins_spent += payment.get("coins") or 0
    coins_earned += payment.get("coins_earned") or 0
    if payment.get("status") == "refunded":
      refunded += amount
    doors[order.get("channel") or "web"] += 1
    if order.get("channel") == "agent":
      signatures[order.get("signature") or "unknown"] += 1
    rails[payment.get("rail") or "?"] += 1
    inbox[order.get("attention") or "new"] += 1
    for item in order["items"]:
      if item.get("voucher") == "refunded":
        redemption["refunded"] += 1
      else:
        redemption[item.get("redemption") or "unredeemed"] += 1
    day = (order.get("placed_at") or "")[:10]
    if day:
      by_day[day]["orders"] += 1
      by_day[day]["amount"] += amount
      if payment.get("status") == "refunded":
        by_day[day]["refunded"] += amount
  declines = [e for e in events if e.get("event") == "PAYMENT_DECLINED"]
  decline_codes = collections.Counter(
    (e.get("detail") or {}).get("code") or "?" for e in declines
  )
  opened = {
    e["checkout_id"]
    for e in events
    if e.get("event") == "CHECKOUT_CREATED" and e.get("checkout_id")
  }
  paid = {
    e["checkout_id"]
    for e in events
    if e.get("event") == "PAYMENT_CONFIRMED" and e.get("checkout_id")
  }
  # The last 14 days, so the chart has a bar for every day, sales or not.
  today = datetime.date.today()
  days = [
    (today - datetime.timedelta(days=i)).isoformat() for i in range(13, -1, -1)
  ]
  return {
    "orders": len(orders),
    "gross": gross,
    "refunded": refunded,
    "net": gross - refunded,
    "coins_spent": coins_spent,
    "coins_earned": coins_earned,
    "doors": dict(doors),
    "signatures": dict(signatures),
    "rails": dict(rails),
    "redemption": dict(redemption),
    "attention": {
      "new": inbox["new"],
      "seen": inbox["seen"],
      "handled": inbox["handled"],
    },
    "declines": len(declines),
    "decline_codes": dict(decline_codes),
    "checkouts_opened": len(opened),
    "checkouts_paid": len(opened & paid),
    "days": [{"day": d, **by_day[d]} for d in days],
  }


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
  """Open the client to the shops."""
  settings = load_settings()
  async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
    app.state.settings = settings
    app.state.shops = Shops(settings, http, Reviews(RUN_DIR / "reviews.json"))
    yield


app = FastAPI(title="Shops console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
# The team signs in (LOGIN_USERS, or AGENT_USER + AGENT_PASSWORD_HASH).
teamlogin.install(app, "Shops console", RUN_DIR)


def shops() -> Shops:
  """Return the shops client of the running app."""
  return app.state.shops


@app.get("/", include_in_schema=False)
async def page() -> HTMLResponse:
  """Serve the page."""
  return page_html("index.html")


@app.get("/api/shops")
async def list_shops() -> dict[str, Any]:
  """Return the shops the console manages."""
  return {
    "name": app.state.settings["name"],
    "shops": [
      {"id": s["id"], "name": s["name"], "url": s["url"], "color": s["color"]}
      for s in app.state.settings["shops"]
    ],
    "agents": list(app.state.settings["agents"]),
  }


@app.get("/api/{shop}/logo.svg", include_in_schema=False)
async def shop_logo(shop: str) -> Response:
  """Serve a shop's mark through the console (the shops may be internal)."""
  client = shops()
  url = client.shop(shop)["url"] + "/logo.svg"
  try:
    answer = await client.http.get(url)
  except httpx.HTTPError as error:
    raise HTTPException(status_code=502, detail="No logo") from error
  if answer.status_code != 200:
    raise HTTPException(status_code=404, detail="No logo")
  return Response(
    answer.content,
    media_type="image/svg+xml",
    headers={"Cache-Control": "max-age=3600"},
  )


@app.get("/api/{shop}/orders")
async def list_orders(shop: str) -> list[dict[str, Any]]:
  """Every order of a shop (or of all of them), newest first."""
  return await shops().orders(shop)


@app.get("/api/{shop}/stats")
async def shop_stats(shop: str) -> dict[str, Any]:
  """Return the management numbers of a shop, or of all of them."""
  client = shops()
  return stats(await client.orders(shop), await client.events(shop))


@app.get("/api/{shop}/orders/{order_id}")
async def order_detail(shop: str, order_id: str) -> dict[str, Any]:
  """One order with the shop's events behind it and the agent's side."""
  client = shops()
  # The shop's GET /orders/{id} is the agent door (signed UCP); the merchant
  # reads the list instead.
  order = next(
    (
      o
      for o in await client.call(shop, "GET", "/orders")
      if o["id"] == order_id
    ),
    None,
  )
  if not order:
    raise HTTPException(status_code=404, detail="Order not found")
  events = [
    event
    for event in await client.call(shop, "GET", "/ledger")
    if event.get("checkout_id") == order.get("checkout_id")
    or (event.get("detail") or {}).get("order_id") == order_id
  ]
  # What the rail (Stripe, or the mock) knows about the payment. A rail that
  # doesn't answer is a note, not a failure of the whole page.
  try:
    payment = await client.call(shop, "GET", f"/orders/{order_id}/payment")
  except HTTPException as error:
    payment = {"error": error.detail}
  agent_side = None
  if order.get("channel") == "agent":
    profile = order.get("agent")
    agent_side = {
      "profile": profile,
      "known": profile in client.agents,
      "evidence": await client.ask_agent(profile, f"/api/evidence/{order_id}"),
      "conversation": await client.ask_agent(
        profile, f"/api/conversations/{order_id}"
      ),
    }
  # Opening the order is looking at it: it leaves the `new` pile.
  client.reviews.seen(shop, order_id)
  return {
    "summary": summarize(
      order, client.shop(shop), client.reviews.get(shop, order_id)
    ),
    "order": order,
    "payment": payment,
    "events": events,
    "agent": agent_side,
  }


@app.post("/api/{shop}/orders/{order_id}/review")
async def review(
  shop: str,
  order_id: str,
  state: Annotated[str, Body(embed=True, pattern="^(handled|open)$")],
) -> dict[str, Any]:
  """Mark an order handled by hand, or open it again (`open` = seen)."""
  shops().shop(shop)
  mark = shops().reviews.set(
    shop, order_id, "handled" if state == "handled" else "seen"
  )
  return {"attention": mark["state"], "reviewed_at": mark["at"]}


@app.post("/api/{shop}/orders/{order_id}/cancel")
async def cancel(shop: str, order_id: str) -> dict[str, Any]:
  """Cancel the purchase and refund it, in one go.

  The marketplace's only action on an order: the service is cancelled, the
  card payment goes back through the rail (Stripe, when that is the rail),
  the coins go back to the wallet, the voucher is voided and its slot is
  released.
  """
  client = shops()
  await client.call(shop, "POST", f"/orders/{order_id}/cancel")
  done = await client.call(
    shop,
    "POST",
    f"/orders/{order_id}/refund",
    {"reason": "cancelled by the marketplace"},
  )
  client.reviews.set(shop, order_id, "handled")
  return done


@app.get("/api/{shop}/events")
async def events(shop: str) -> list[dict[str, Any]]:
  """Return the shop's events (or every shop's), newest first."""
  return list(reversed(await shops().events(shop)))


@app.get("/api/{shop}/inventory")
async def inventory(shop: str) -> list[dict[str, Any]]:
  """Each option's codes by status, in one shop or in all of them."""
  client = shops()
  rows = []
  for chosen in client.chosen(shop):
    rows += [
      {
        **row,
        "shop": chosen["id"],
        "shop_name": chosen["name"],
        "shop_color": chosen["color"],
      }
      for row in await client.call(chosen["id"], "GET", "/inventory")
    ]
  return rows


@app.get("/api/{shop}/wallets/{email}")
async def wallet(shop: str, email: str) -> dict[str, Any]:
  """Return a customer's wallet in the shop and every movement behind it."""
  return await shops().call(shop, "GET", f"/wallets/{email}")


@app.post("/api/{shop}/wallets/grant")
async def grant(
  shop: str,
  email: Annotated[str, Body(embed=True)],
  coins: Annotated[int, Body(embed=True, gt=0)],
) -> dict[str, Any]:
  """Give a customer coins."""
  return await shops().call(
    shop, "POST", "/wallets/grant", {"email": email, "coins": coins}
  )


@app.post("/api/{shop}/accounts")
async def create_account(
  shop: str,
  full_name: Annotated[str, Body(embed=True)],
  email: Annotated[str, Body(embed=True)],
  password: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
  """Create a customer's account in the shop."""
  return await shops().call(
    shop,
    "POST",
    "/accounts",
    {"full_name": full_name, "email": email, "password": password},
  )


@app.put("/api/{shop}/accounts/{email}/password")
async def set_password(
  shop: str, email: str, password: Annotated[str, Body(embed=True)]
) -> dict[str, Any]:
  """Set a customer's password in the shop."""
  return await shops().call(
    shop, "PUT", f"/accounts/{email}/password", {"password": password}
  )


@app.post("/api/{shop}/booking-fee")
async def booking_fee(
  shop: str, amount: Annotated[int, Body(embed=True, ge=0)]
) -> dict[str, Any]:
  """Add a fee to every checkout from now on; 0 removes it. For the demo."""
  return await shops().call(
    shop, "POST", "/testing/booking-fee", {"amount": amount}
  )
