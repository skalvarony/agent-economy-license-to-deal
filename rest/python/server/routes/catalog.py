"""Catalog routes: how an agent sees what the shop sells.

`/catalog/search` and `/catalog/lookup` are UCP's catalog capability. A deal is
one product and each of its options is a variant, the item an agent puts in a
checkout. The product carries the deal's voucher terms (service window, refund
policy, redemption), the same fields the checkout's line items carry. People
see the same catalog in the storefront (routes/storefront.py).
"""

import dataclasses
import json
import datetime
import pathlib
import re
from typing import Annotated, Any

import config
import db
import dependencies
from exceptions import InvalidRequestError
from exceptions import ResourceNotFoundError
from fastapi import APIRouter
from fastapi import Body
from fastapi import Query
from fastapi import Depends
from fastapi import Request
from fastapi.responses import PlainTextResponse
from services import booking_service
from services import coin_service
from services import voucher_service
from sqlalchemy.ext.asyncio import AsyncSession
from ucp_sdk.models.schemas.capability import ResponseSchema as Response
from ucp_sdk.models.schemas.shopping import catalog_lookup
from ucp_sdk.models.schemas.shopping import catalog_search
from ucp_sdk.models.schemas.shopping.types.product import Product
from ucp_sdk.models.schemas.ucp import ResponseCatalogSchema

router = APIRouter()

_ROUTES_DIR = pathlib.Path(__file__).parent
SEARCH_CAPABILITY = "dev.ucp.shopping.catalog.search"
LOOKUP_CAPABILITY = "dev.ucp.shopping.catalog.lookup"
DEFAULT_PAGE_SIZE = 10
RATING_SCALE_MAX = 5


@dataclasses.dataclass
class Offer:
  """One thing a buyer can put in a checkout, and how many are left."""

  product: db.Product
  option: db.DealOption | None
  stock: int


@dataclasses.dataclass
class Listing:
  """One catalog entry: a deal with its options, or a plain product."""

  id: str
  title: str
  deal: db.Deal | None
  offers: list[Offer]

  @property
  def stock(self) -> int:
    """How many units are left across the listing's offers."""
    return sum(offer.stock for offer in self.offers)


def image_file(listing_id: str) -> pathlib.Path | None:
  """Return the shop's photo of a deal or product, if it has one."""
  flags = config.FLAGS
  if not flags.is_parsed() or not flags.shop_dir:
    return None
  path = pathlib.Path(flags.shop_dir) / "images" / f"{listing_id}.jpg"
  return path if path.is_file() else None


async def load_catalog(
  products_session: AsyncSession, transactions_session: AsyncSession
) -> list[Listing]:
  """Return every deal with its options, and every product sold on its own."""
  products = {p.id: p for p in await db.list_products(products_session)}
  # Products that are not an option of any deal are listed by themselves.
  products_alone = dict(products)

  async def offer(product_id: str, option: db.DealOption | None) -> Offer:
    stock = await db.get_inventory(transactions_session, product_id) or 0
    return Offer(products[product_id], option, stock)

  options: dict[str, list[db.DealOption]] = {}
  for option in await db.list_options(products_session):
    options.setdefault(option.deal_id, []).append(option)
    del products_alone[option.product_id]
  listings = [
    Listing(
      deal.id,
      deal.title,
      deal,
      [await offer(o.product_id, o) for o in options.get(deal.id, [])],
    )
    for deal in await db.list_deals(products_session)
  ]
  listings += [
    Listing(product.id, product.title, None, [await offer(product.id, None)])
    for product in products_alone.values()
  ]
  return sorted(listings, key=lambda listing: listing.id)


def _ucp(capability: str) -> ResponseCatalogSchema:
  version = config.get_server_version()
  return ResponseCatalogSchema(
    version=version,
    capabilities={capability: [Response(name=capability, version=version)]},
  )


async def _promotions(transactions_session: AsyncSession) -> list[dict]:
  """Return the promo code the shop advertises, as agents see it."""
  code = config.get_shop().get("promo_code")
  promo = await db.get_discount(transactions_session, code) if code else None
  if not promo:
    return []
  return [
    {
      "code": promo.code,
      "type": promo.type,
      "value": promo.value,
      "description": promo.description,
    }
  ]


def gallery_files(listing_id: str) -> list[str]:
  """Return the file names of a listing's photos, the main one first."""
  flags = config.FLAGS
  if not flags.is_parsed() or not flags.shop_dir:
    return []
  folder = pathlib.Path(flags.shop_dir) / "images"
  if not (folder / f"{listing_id}.jpg").is_file():
    return []
  extra = sorted(
    p.name for p in folder.glob(f"{listing_id}_*.jpg") if p.is_file()
  )
  return [f"{listing_id}.jpg", *extra]


async def top_reviews(
  products_session: AsyncSession, listings: list[Listing], limit: int = 3
) -> dict[str, list[dict[str, Any]]]:
  """Return each deal's best-rated recent reviews, as the catalog shows them."""
  out: dict[str, list[dict[str, Any]]] = {}
  for listing in listings:
    if not listing.deal:
      continue
    rows = await db.list_reviews(products_session, listing.id)
    rows = sorted(rows, key=lambda r: (-(r.rating or 0), r.date or ""))[:limit]
    out[listing.id] = [
      {
        "author": r.author,
        "rating": r.rating,
        "date": r.date,
        "text": r.text,
        **({"reply": r.reply} if r.reply else {}),
      }
      for r in rows
    ]
  return out


def _to_product(
  listing: Listing,
  base_url: str,
  promotions: list[dict],
  reviews: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
  """Build a UCP product from a listing, with one variant per offer."""
  deal = listing.deal
  currency = config.get_default_currency()

  def price(amount: int) -> dict[str, Any]:
    return {"amount": amount, "currency": currency}

  description = {"plain": deal.description if deal else listing.title}
  variants = []
  for offer in listing.offers:
    variant = {
      "id": offer.product.id,
      "title": offer.product.title,
      "description": description,
      "price": price(offer.product.price),
      "availability": {
        "available": offer.stock > 0,
        "status": "in_stock" if offer.stock > 0 else "out_of_stock",
      },
    }
    if deal:
      variant["seller"] = {"name": deal.merchant}
    if coin_service.back_percent():
      variant["coins_earned"] = coin_service.earned_for(offer.product.price)
    if offer.option:
      label = deal.option_label or "Option"
      variant["options"] = [{"name": label, "label": offer.option.title}]
      if offer.option.description:
        variant["description"] = {"plain": offer.option.description}
      variant["includes"] = offer.option.includes or []
      if offer.option.list_price:
        variant["list_price"] = price(offer.option.list_price)
    variants.append(variant)

  prices = [offer.product.price for offer in listing.offers]
  body = {
    "id": listing.id,
    "title": listing.title,
    "description": description,
    "price_range": {"min": price(min(prices)), "max": price(max(prices))},
    "variants": variants,
  }
  # Every photo the shop's own page shows, so an agent can present the deal
  # its person's way instead of the shop's.
  body["media"] = [
    {
      "type": "image",
      "url": f"{base_url}/images/{name}",
      "alt_text": listing.title,
    }
    for name in gallery_files(listing.id)
  ]
  if deal:
    body["categories"] = [{"value": deal.category}]
    body.update(voucher_service.terms(deal))
    # What the shop's page says about the deal, as data: the agent decides
    # how to show it.
    if deal.highlights:
      body["highlights"] = list(deal.highlights)
    if deal.about_merchant:
      body["seller"] = {
        "name": deal.merchant,
        "description": deal.about_merchant,
      }
    if reviews is not None and reviews.get(deal.id):
      body["reviews"] = reviews[deal.id]
    if promotions:
      # Codes an agent may send in a checkout's `discounts.codes`.
      body["promotions"] = promotions
    if coin_service.back_percent():
      # What paying by card here gives back, to weigh against other shops.
      body["rewards"] = {
        "coins_back_percent": coin_service.back_percent(),
        "coin_value": price(coin_service.COIN_VALUE),
      }
    body["options"] = [
      {
        "name": deal.option_label or "Option",
        "values": [{"label": o.option.title} for o in listing.offers],
      }
    ]
    list_prices = [
      o.option.list_price for o in listing.offers if o.option.list_price
    ]
    if list_prices:
      body["list_price_range"] = {
        "min": price(min(list_prices)),
        "max": price(max(list_prices)),
      }
    if deal.rating:
      body["rating"] = {
        "value": deal.rating,
        "scale_max": RATING_SCALE_MAX,
        "count": deal.reviews,
      }
  return body


def _words(text: str) -> set[str]:
  return set(re.findall(r"\w+", text.lower()))


def _query_score(query: str | None, listing: Listing) -> int:
  """Count how many words of the query the listing mentions."""
  deal = listing.deal
  text = " ".join(
    [listing.title]
    + ([deal.description, deal.category, deal.merchant] if deal else [])
    + [offer.option.title for offer in listing.offers if offer.option]
  )
  return len(_words(query or "") & _words(text))


@router.post(
  "/catalog/search",
  response_model=catalog_search.SearchResponse,
  response_model_exclude_none=True,
  operation_id="search_catalog",
)
async def search_catalog(
  request: Request,
  search: Annotated[catalog_search.SearchRequest, Body(...)],
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  products_session: Annotated[
    AsyncSession, Depends(dependencies.get_products_db)
  ],
  transactions_session: Annotated[
    AsyncSession, Depends(dependencies.get_transactions_db)
  ],
) -> catalog_search.SearchResponse:
  """Search the catalog by free text, category and price."""
  del common_headers  # Unused
  filters = search.filters
  categories = {c.lower() for c in (filters and filters.categories) or []}
  price = filters.price if filters else None
  lowest = price.min if price and price.min is not None else 0
  highest = price.max if price and price.max is not None else float("inf")

  matches = []
  for listing in await load_catalog(products_session, transactions_session):
    deal = listing.deal
    score = _query_score(search.query, listing)
    if search.query and not score:
      continue
    if categories and (not deal or deal.category.lower() not in categories):
      continue
    # A deal matches a price filter when any of its options does.
    if not any(lowest <= o.product.price <= highest for o in listing.offers):
      continue
    matches.append((score, listing))
  # Best text match first; the catalog's own order breaks ties.
  matches.sort(key=lambda match: -match[0])

  page = search.pagination
  offset = int(page.cursor) if page and page.cursor else 0
  limit = (page.limit if page else None) or DEFAULT_PAGE_SIZE
  has_next_page = offset + limit < len(matches)
  base_url = str(request.base_url).rstrip("/")
  promotions = await _promotions(transactions_session)
  page_listings = [listing for _, listing in matches[offset : offset + limit]]
  reviews = await top_reviews(products_session, page_listings)
  return catalog_search.SearchResponse(
    ucp=_ucp(SEARCH_CAPABILITY),
    products=[
      Product(**_to_product(listing, base_url, promotions, reviews))
      for listing in page_listings
    ],
    pagination={
      "cursor": str(offset + limit) if has_next_page else None,
      "has_next_page": has_next_page,
      "total_count": len(matches),
    },
  )


@router.post(
  "/catalog/lookup",
  response_model=catalog_lookup.LookupResponse,
  response_model_exclude_none=True,
  operation_id="lookup_catalog",
)
async def lookup_catalog(
  request: Request,
  lookup: Annotated[catalog_lookup.LookupRequest, Body(...)],
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  products_session: Annotated[
    AsyncSession, Depends(dependencies.get_products_db)
  ],
  transactions_session: Annotated[
    AsyncSession, Depends(dependencies.get_transactions_db)
  ],
) -> catalog_lookup.LookupResponse:
  """Return the products for the given deal or option IDs.

  An option ID returns its deal with that one variant. A deal ID returns the
  deal with all its variants. Unknown IDs are left out.
  """
  del common_headers  # Unused
  base_url = str(request.base_url).rstrip("/")
  promotions = await _promotions(transactions_session)
  products = []
  for listing in await load_catalog(products_session, transactions_session):
    body = _to_product(listing, base_url, promotions)
    asked = []
    for variant in body["variants"]:
      inputs = []
      if variant["id"] in lookup.ids:
        inputs.append({"id": variant["id"], "match": "exact"})
      elif listing.id in lookup.ids:
        inputs.append({"id": listing.id, "match": "featured"})
      if inputs:
        asked.append({**variant, "inputs": inputs})
    if asked:
      products.append(catalog_lookup.Product(**{**body, "variants": asked}))
  return catalog_lookup.LookupResponse(
    ucp=_ucp(LOOKUP_CAPABILITY), products=products
  )


@router.get("/deals/{deal_id}/availability", include_in_schema=False)
async def deal_availability(
  deal_id: str,
  products_session: Annotated[
    AsyncSession, Depends(dependencies.get_products_db)
  ],
  transactions_session: Annotated[
    AsyncSession, Depends(dependencies.get_transactions_db)
  ],
  start: Annotated[str | None, Query(alias="from")] = None,
  days: Annotated[int, Query(ge=1, le=booking_service.MAX_DAYS)] = 7,
) -> dict[str, Any]:
  """List a deal's open slots from a day on, with the places left in each.

  Open to anyone, like the catalog: an agent reads it before proposing a
  time, and the shop's own checkout page reads it to offer the choice.
  """
  deal = await db.get_deal(products_session, deal_id)
  if not deal:
    raise ResourceNotFoundError(f"Deal {deal_id} not found")
  if not booking_service.required(deal):
    return {"deal_id": deal_id, "required": False, "days": []}
  begin = None
  if start:
    try:
      begin = datetime.date.fromisoformat(start)
    except ValueError as e:
      raise InvalidRequestError(f"from must be a day (YYYY-MM-DD): {start!r}") from e
  return await booking_service.availability(
    transactions_session, deal, begin, days
  )


@router.get("/schemas/service_voucher.json", include_in_schema=False)
async def service_voucher_schema() -> dict[str, Any]:
  """Serve the JSON Schema of the voucher terms this shop adds to UCP."""
  with (_ROUTES_DIR / "service_voucher.schema.json").open() as f:
    return json.load(f)


@router.get(
  "/specs/service_voucher",
  response_class=PlainTextResponse,
  include_in_schema=False,
)
async def service_voucher_spec() -> str:
  """Serve the human-readable spec of the voucher terms."""
  return (_ROUTES_DIR / "service_voucher.md").read_text(encoding="utf-8")
