#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Order management routes for the UCP server."""

import re
from typing import Annotated, Any

import config
import dependencies
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Path
from models import UnifiedOrder
from routes.voucher import get_voucher_service
from services.checkout_service import CheckoutService
from services.voucher_service import VoucherService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


@router.get(
  "/orders/{id}",
  response_model=dict[str, Any],
  operation_id="get_order",
)
async def get_order(
  order_id: Annotated[str, Path(..., alias="id")],
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  checkout_service: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
  ],
  simulation_secret: Annotated[
    str | None, Header(alias="Simulation-Secret")
  ] = None,
) -> dict[str, Any]:
  """Get an order by ID.

  An order is shown to the agent that placed it (the UCP-Agent profile the
  order recorded) or to the shop itself (the Simulation-Secret header). With
  --require_signatures the profile is proven, not just claimed.
  """
  order = await checkout_service.get_order(order_id)
  if simulation_secret != config.FLAGS.simulation_secret:
    asked_by = _profile(common_headers.ucp_agent)
    if not asked_by or asked_by != order.get("agent"):
      raise HTTPException(
        status_code=403,
        detail="Only the agent that placed this order, or the shop, may read"
        " it",
      )
  return order


def _only_the_agent(order: dict[str, Any], ucp_agent: str) -> None:
  """Refuse anyone but the agent that placed the order."""
  asked_by = _profile(ucp_agent)
  if not asked_by or asked_by != order.get("agent"):
    raise HTTPException(
      status_code=403,
      detail="Only the agent that placed this order may change it",
    )


@router.put(
  "/orders/{id}/booking",
  response_model=dict[str, Any],
  operation_id="reschedule_order",
)
async def reschedule_order(
  order_id: Annotated[str, Path(..., alias="id")],
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  checkout_service: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
  ],
  vouchers: Annotated[VoucherService, Depends(get_voucher_service)],
  products_session: Annotated[
    AsyncSession, Depends(dependencies.get_products_db)
  ],
  starts_at: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
  """Move the order's visit to another open slot, as the customer's agent.

  What the customer can do from the order's page, the agent that placed the
  order can do here: `starts_at` is a slot from the deal's availability.
  """
  order = await checkout_service.get_order(order_id)
  _only_the_agent(order, common_headers.ucp_agent)
  return await vouchers.reschedule(products_session, order_id, starts_at)


@router.post(
  "/orders/{id}/cancellation",
  response_model=dict[str, Any],
  operation_id="cancel_order_by_shopper",
)
async def cancel_order_by_shopper(
  order_id: Annotated[str, Path(..., alias="id")],
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  checkout_service: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
  ],
  vouchers: Annotated[VoucherService, Depends(get_voucher_service)],
) -> dict[str, Any]:
  """Cancel the order for a refund, as the customer's agent, under the terms.

  Refundable deals only, before the refund deadline, while the voucher is
  unused: the same as the customer's own button on the order's page.
  """
  order = await checkout_service.get_order(order_id)
  _only_the_agent(order, common_headers.ucp_agent)
  return await vouchers.cancel_by_shopper(order_id)


def _profile(ucp_agent: str) -> str | None:
  """Return the profile URL in a UCP-Agent header."""
  found = re.search(r'profile="([^"]+)"', ucp_agent or "")
  return found.group(1) if found else None


@router.post(
  "/testing/simulate-shipping/{id}",
  response_model=dict[str, Any],
  operation_id="ship_order",
  dependencies=[Depends(dependencies.verify_simulation_secret)],
)
async def ship_order(
  order_id: Annotated[str, Path(..., alias="id")],
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  checkout_service: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
  ],
) -> dict[str, Any]:
  """Simulate shipping an order."""
  del common_headers  # Unused
  await checkout_service.ship_order(order_id)
  return {"status": "shipped"}


@router.put(
  "/orders/{id}",
  response_model=dict[str, Any],
  operation_id="update_order",
  # Only the shop changes an order.
  dependencies=[Depends(dependencies.verify_simulation_secret)],
)
async def update_order(
  order_id: Annotated[str, Path(..., alias="id")],
  order: Annotated[UnifiedOrder, Body(...)],
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  checkout_service: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
  ],
) -> dict[str, Any]:
  """Update an order."""
  del common_headers  # Unused
  # We convert to dict to match service signature and DB storage which expects
  # JSON-able dict
  order_data = order.model_dump(mode="json", by_alias=True)
  return await checkout_service.update_order(order_id, order_data)
