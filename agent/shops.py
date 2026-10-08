"""The agent's side of UCP: the calls it makes to a shop.

Every call says who the agent is (the `UCP-Agent` header points at its
profile) and is signed, and every call is reported to `on_call`, so the person
can see what their agent did and where.
"""

from collections.abc import Callable
import dataclasses
import time
from typing import Any
from urllib.parse import quote
import uuid

import httpx

PAGE_SIZE = 50


class ShopError(Exception):
  """A shop refused a call, or didn't answer."""

  def __init__(self, status: int, code: str, message: str) -> None:
    """Keep the HTTP status, the shop's error code and its message."""
    super().__init__(message)
    self.status = status
    self.code = code
    self.message = message


def _refusal(response: httpx.Response) -> ShopError:
  """Read a shop's error, in either of the two shapes the shops use."""
  try:
    body = response.json()
  except ValueError:
    body = {}
  detail = body.get("detail") if isinstance(body, dict) else None
  problems = (body.get("messages") if isinstance(body, dict) else None) or (
    detail.get("errors") if isinstance(detail, dict) else None
  )
  first = problems[0] if problems else {}
  message = first.get("content") or first.get("message")
  if not message:
    message = detail if isinstance(detail, str) else response.reason_phrase
  return ShopError(
    response.status_code, str(first.get("code") or "error"), str(message)
  )


@dataclasses.dataclass
class Shop:
  """One shop the agent may buy from."""

  id: str
  name: str
  url: str
  color: str
  http: httpx.AsyncClient
  agent_profile: str
  on_call: Callable[[dict[str, Any]], None] = lambda call: None
  # Where people reach the shop, when the agent talks to it by another
  # address (inside a private network, say). Links and photos use this one.
  public_url: str | None = None

  def __post_init__(self) -> None:
    """Default the public address to the one the agent uses."""
    self.url = self.url.rstrip("/")
    self.public_url = (self.public_url or self.url).rstrip("/")

  def for_people(self, url: str | None) -> str | None:
    """Turn a URL the shop gave the agent into one a browser can open."""
    if url and url.startswith(self.url):
      return self.public_url + url[len(self.url) :]
    return url

  async def call(
    self,
    method: str,
    path: str,
    body: Any = None,
    *,
    note: str | None = "",
  ) -> dict[str, Any]:
    """Call the shop and return its answer; `note` says what the call is for.

    `note=None` is a quiet call: the page's own housekeeping (reading the
    coins for the sidebar), not a step of the conversation, so it isn't
    reported.
    """
    headers = {
      "UCP-Agent": f'profile="{self.agent_profile}"',
      "request-id": str(uuid.uuid4()),
      "idempotency-key": str(uuid.uuid4()),
    }
    started = time.monotonic()
    status = 0
    try:
      response = await self.http.request(
        method, self.url + path, json=body, headers=headers
      )
      status = response.status_code
    except httpx.HTTPError as error:
      raise ShopError(0, "unreachable", "The shop did not answer.") from error
    finally:
      if note is not None:
        self.on_call(
          {
            "shop": self.id,
            "method": method,
            "path": path.split("?")[0],
            "status": status,
            "ms": round((time.monotonic() - started) * 1000),
            "note": note,
          }
        )
    if status >= 400:
      raise _refusal(response)
    return response.json()

  async def search(
    self,
    query: str | None = None,
    category: str | None = None,
    max_price: int | None = None,
  ) -> list[dict[str, Any]]:
    """Return the shop's deals that match; prices are in cents."""
    filters: dict[str, Any] = {}
    if category:
      filters["categories"] = [category]
    if max_price is not None:
      filters["price"] = {"max": max_price}
    body: dict[str, Any] = {"pagination": {"limit": PAGE_SIZE}}
    if query:
      body["query"] = query
    if filters:
      body["filters"] = filters
    wanted = " ".join(filter(None, [category, query])) or "everything"
    found = await self.call(
      "POST", "/catalog/search", body, note=f"Search: {wanted}"
    )
    return found.get("products", [])

  async def wallet(
    self, email: str, note: str | None = "Read the coin wallet"
  ) -> dict[str, Any]:
    """Return the customer's coins in this shop and what the shop gives back."""
    return await self.call("GET", f"/wallet?email={quote(email)}", note=note)

  async def open_checkout(
    self,
    item_id: str,
    quantity: int,
    buyer: dict[str, str],
    coins: int = 0,
    promo_code: str | None = None,
  ) -> dict[str, Any]:
    """Open a checkout for one option. Nothing is paid yet."""
    body: dict[str, Any] = {
      "line_items": [{"item": {"id": item_id}, "quantity": quantity}],
      "buyer": buyer,
    }
    if coins:
      body["coins"] = {"use": coins}
    if promo_code:
      body["discounts"] = {"codes": [promo_code]}
    return await self.call(
      "POST",
      "/checkout-sessions",
      body,
      note="Open a checkout to get the total",
    )

  async def checkout(self, checkout_id: str) -> dict[str, Any]:
    """Fetch a checkout as the shop has it now."""
    return await self.call(
      "GET", f"/checkout-sessions/{checkout_id}", note="Read the new total"
    )

  async def complete(
    self,
    checkout_id: str,
    card: dict[str, str],
    context: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    """Pay a checkout. The shop refuses if its total moved since it was read.

    `card` is the agent's tokenised card: the shop's payment `handler` and
    the `token` for it (a mock token, or a Stripe PaymentMethod id).
    `context` is what the agent tells the shop about how its customer
    decided; the shop keeps it on the order for the customer to see.
    """
    body: dict[str, Any] = {
      "payment": {
        "instruments": [
          {
            "id": "instr_1",
            "handler_id": card["handler"],
            "type": "card",
            "credential": {"type": "token", "token": card["token"]},
          }
        ]
      },
      "risk_signals": {},
    }
    if context:
      body["agent_context"] = context
    return await self.call(
      "POST",
      f"/checkout-sessions/{checkout_id}/complete",
      body,
      note="Pay the approved checkout",
    )

  async def order(self, order_id: str) -> dict[str, Any]:
    """Fetch an order with its vouchers and its payment."""
    return await self.call(
      "GET", f"/orders/{order_id}", note="Collect the voucher"
    )
