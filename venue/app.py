"""The merchants' world: what happens at the venue, and what follows.

The shops are the intermediary; the service is delivered by a merchant the
shops don't control (a spa, a brewery). This page simulates that world for
every shop, every deal and every purchase: the customer arrives, the venue
can't honour the voucher, the venue cancels the slot, the customer never
shows up. Each action is sent to the shop as the merchant would, and the
shop's response to it (refund, goodwill coins) is applied and shown, so a
purchase has the consequences it would have in real life.

  GET  /                                   the page
  GET  /api/world                          every shop, its deals, its purchases
  POST /api/{shop}/orders/{id}/happen      {"what": "arrived" | "refused" |
                                            "cancelled" | "no_show", "note"}
  GET  /api/log                            what happened so far, newest first

SIMULATION_SECRET is the shops' merchant secret. SHOP_<ID>_URL overrides the
addresses in venue.json. VENUE_RUN_DIR is where the log is kept. The team
signs in on /login (teamlogin.py) when LOGIN_USERS or AGENT_USER +
AGENT_PASSWORD_HASH are set; otherwise the page runs open.
"""

import contextlib
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
CONFIG = pathlib.Path(os.environ.get("VENUE_CONFIG", HERE / "venue.json"))
RUN_DIR = pathlib.Path(
  os.environ.get("VENUE_RUN_DIR", HERE.parent / ".run" / "venue")
)
SECRET = os.environ.get("SIMULATION_SECRET", "demo-secret")
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


# The marketplace's response when the venue lets a customer down: a full
# refund (the shop does it) and these coins as goodwill. A policy to tune.
GOODWILL_COINS = 5

# What can happen at the venue, and what it means.
HAPPENINGS = {
  "arrived": {
    "label": "The customer arrives and the venue honours the voucher",
    "venue": "redeemed",
  },
  "refused": {
    "label": "The customer arrives and the venue can't honour it",
    "venue": "not honoured",
  },
  "cancelled": {
    "label": "The venue cancels the slot before the visit",
    "venue": "cancelled",
  },
  "no_show": {
    "label": "The customer never shows up",
    "venue": "no-show",
  },
}


def load_settings() -> dict[str, Any]:
  """Read venue.json and apply the environment's overrides."""
  settings = json.loads(CONFIG.read_text())
  for shop in settings["shops"]:
    shop["url"] = os.environ.get(
      f"SHOP_{shop['id'].upper()}_URL", shop["url"]
    ).rstrip("/")
  return settings


class Shops:
  """Calls to the shops, as the merchant."""

  def __init__(self, settings: dict[str, Any], http: httpx.AsyncClient):
    """Keep the addresses and the client."""
    self.by_id = {shop["id"]: shop for shop in settings["shops"]}
    self.http = http

  def shop(self, shop_id: str) -> dict[str, Any]:
    """Return a shop's settings, or 404."""
    if shop_id not in self.by_id:
      raise HTTPException(status_code=404, detail=f"No shop {shop_id}")
    return self.by_id[shop_id]

  async def call(
    self, shop_id: str, method: str, path: str, body: Any = None
  ) -> Any:
    """Call a shop as the merchant; a refusal becomes the same HTTP error."""
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
      detail = response.text[:200]
      with contextlib.suppress(ValueError):
        found = response.json().get("detail")
        detail = found if isinstance(found, str) else str(found or detail)
      raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


def _purchase(order: dict[str, Any]) -> dict[str, Any] | None:
  """Reduce an order to the purchase a venue sees. None if nothing to see."""
  lines = order.get("line_items") or []
  if not lines:
    return None
  line = lines[0]
  voucher = line.get("voucher") or {}
  payment = order.get("payment") or {}
  service = line.get("service") or {}
  redemption = line.get("redemption") or {}
  return {
    "order_id": order["id"],
    "placed_at": order.get("placed_at"),
    "item_id": line["item"].get("id"),
    "title": line["item"]["title"],
    "option": service.get("option"),
    "quantity": (line.get("quantity") or {}).get("total", 1)
    if isinstance(line.get("quantity"), dict)
    else line.get("quantity", 1),
    "buyer": order.get("buyer") or {},
    "codes": voucher.get("codes") or [],
    "voucher": voucher.get("status"),
    "expires_at": voucher.get("expires_at"),
    "redemption": redemption.get("status"),
    "redemption_method": redemption.get("method"),
    "instructions": redemption.get("instructions"),
    "window": service.get("window") or {},
    "booking": service.get("booking") or {},
    "cancellation": line.get("cancellation") or {},
    "payment": payment.get("status"),
    "paid": payment.get("amount", 0),
    "coins": payment.get("coins", 0),
    "rail": payment.get("rail"),
    "channel": order.get("channel"),
    "signature": (order.get("signature") or {}).get("status"),
  }


def _log_path() -> pathlib.Path:
  RUN_DIR.mkdir(parents=True, exist_ok=True)
  return RUN_DIR / "happened.jsonl"


def read_log() -> list[dict[str, Any]]:
  """Return everything that happened, newest first."""
  path = _log_path()
  if not path.exists():
    return []
  lines = [json.loads(line) for line in path.read_text().splitlines() if line]
  return list(reversed(lines))


def write_log(entry: dict[str, Any]) -> None:
  """Append one happening and its consequences."""
  with _log_path().open("a") as log:
    log.write(json.dumps(entry, ensure_ascii=False) + "\n")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
  """Open the client to the shops."""
  settings = load_settings()
  async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
    app.state.settings = settings
    app.state.shops = Shops(settings, http)
    yield


app = FastAPI(title="At the venue", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
# The team signs in (LOGIN_USERS, or AGENT_USER + AGENT_PASSWORD_HASH).
teamlogin.install(app, "At the venue", RUN_DIR)


@app.get("/", include_in_schema=False)
async def page() -> HTMLResponse:
  """Serve the page."""
  return page_html("index.html")


@app.get("/api/{shop}/logo.svg", include_in_schema=False)
async def shop_logo(shop: str) -> Response:
  """Serve a marketplace's mark through the venue; shops may be internal."""
  shops: Shops = app.state.shops
  url = shops.shop(shop)["url"] + "/logo.svg"
  try:
    answer = await shops.http.get(url)
  except httpx.HTTPError as error:
    raise HTTPException(status_code=502, detail="No logo") from error
  if answer.status_code != 200:
    raise HTTPException(status_code=404, detail="No logo")
  return Response(
    answer.content,
    media_type="image/svg+xml",
    headers={"Cache-Control": "max-age=3600"},
  )


@app.get("/api/world")
async def world() -> dict[str, Any]:
  """Every shop with its deals and the purchases of each deal."""
  shops: Shops = app.state.shops
  out = []
  for shop in app.state.settings["shops"]:
    entry: dict[str, Any] = {
      "id": shop["id"],
      "name": shop["name"],
      "url": shop["url"],
      "color": shop["color"],
      "deals": [],
      "error": None,
    }
    try:
      deals = await shops.call(shop["id"], "GET", "/deals")
      orders = await shops.call(shop["id"], "GET", "/orders")
    except HTTPException as refused:
      entry["error"] = refused.detail
      out.append(entry)
      continue
    purchases = [p for p in map(_purchase, orders) if p]
    for deal in deals:
      option_ids = {option["id"] for option in deal["options"]}
      deal["purchases"] = [p for p in purchases if p["item_id"] in option_ids]
      entry["deals"].append(deal)
    out.append(entry)
  return {
    "name": app.state.settings["name"],
    "shops": out,
    "happenings": {key: value["label"] for key, value in HAPPENINGS.items()},
    "goodwill_coins": GOODWILL_COINS,
  }


@app.post("/api/{shop}/orders/{order_id}/happen")
async def happen(
  shop: str,
  order_id: str,
  what: Annotated[str, Body(embed=True)],
  note: Annotated[str, Body(embed=True)] = "",
) -> dict[str, Any]:
  """Something happens at the venue; the shop responds; both are recorded."""
  if what not in HAPPENINGS:
    raise HTTPException(status_code=400, detail=f"Unknown happening {what}")
  shops: Shops = app.state.shops
  seller = shops.shop(shop)
  orders = await shops.call(shop, "GET", "/orders")
  order = next((o for o in orders if o["id"] == order_id), None)
  if not order:
    raise HTTPException(status_code=404, detail="Order not found")
  purchase = _purchase(order)
  buyer = (order.get("buyer") or {}).get("email")
  consequences: list[str] = []

  async def refund_and_goodwill(reason: str) -> None:
    refunded = await shops.call(
      shop, "POST", f"/orders/{order_id}/refund", {"reason": reason}
    )
    payment = refunded.get("payment") or {}
    consequences.append(
      "The marketplace refunded the customer:"
      f" ${payment.get('amount', 0) / 100:.2f} to the card"
      + (
        f" and {payment.get('coins', 0)} coins to the wallet"
        if payment.get("coins")
        else ""
      )
      + ". The voucher's code is void and never sold again."
    )
    if buyer and GOODWILL_COINS:
      await shops.call(
        shop,
        "POST",
        "/wallets/grant",
        {"email": buyer, "coins": GOODWILL_COINS},
      )
      consequences.append(
        f"Goodwill: {GOODWILL_COINS} coins added to {buyer}'s wallet at"
        f" {seller['name']}."
      )

  if what == "arrived":
    await shops.call(
      shop, "POST", f"/orders/{order_id}/redeem", {"honoured": True}
    )
    consequences.append(
      "The voucher is used. The sale is final: no refund is possible now, and"
      " the coins the purchase earned stay with the customer."
    )
  elif what == "refused":
    await shops.call(
      shop, "POST", f"/orders/{order_id}/redeem", {"honoured": False}
    )
    consequences.append(
      "The shop recorded a failed visit: the customer came and was turned"
      " away. That is the merchant's failure, not the customer's."
    )
    await refund_and_goodwill("not honoured at the venue")
  elif what == "cancelled":
    await shops.call(shop, "POST", f"/orders/{order_id}/cancel")
    consequences.append(
      "The shop recorded that the merchant cancelled the service before the"
      " visit."
    )
    await refund_and_goodwill("merchant cancelled")
  elif what == "no_show":
    consequences.append(
      "Nothing changes at the shop: the voucher stays unredeemed and keeps its"
      " validity. A no-show is the customer's; there is no refund for it."
    )
    consequences.append(
      "Not built yet: expiring the voucher when its validity ends."
    )

  entry = {
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(
      timespec="seconds"
    ),
    "shop": shop,
    "shop_name": seller["name"],
    "order_id": order_id,
    "title": purchase["title"] if purchase else "",
    "buyer": buyer,
    "what": what,
    "label": HAPPENINGS[what]["label"],
    "venue": HAPPENINGS[what]["venue"],
    "note": note,
    "consequences": consequences,
  }
  write_log(entry)
  return entry


@app.get("/api/log")
async def log() -> list[dict[str, Any]]:
  """Return what happened so far, newest first."""
  return read_log()
