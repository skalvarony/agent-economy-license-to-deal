"""Voucher routes: what happens to an order after it is paid.

None of these are called by the buyer's agent. Cancelling and redeeming are
the merchant's actions, a refund is ordered by whoever decides a complaint,
`/inventory` is the shop's own view of its code pools, and the booking fee is
a switch for the demo. All of them need the `Simulation-Secret` header.
`/ledger` shows the events they produce.
"""

from typing import Annotated, Any

import db
import dependencies
from exceptions import ResourceNotFoundError
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import Path
from routes import catalog
from services import ledger
from services import payment_rail
from services.voucher_service import VoucherService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def get_voucher_service(
  transactions_session: Annotated[
    AsyncSession, Depends(dependencies.get_transactions_db)
  ],
) -> VoucherService:
  """Dependency provider for VoucherService."""
  return VoucherService(transactions_session, payment_rail.get_rail())


SimulationSecret = Depends(dependencies.verify_simulation_secret)
Vouchers = Annotated[VoucherService, Depends(get_voucher_service)]
ProductsDb = Annotated[AsyncSession, Depends(dependencies.get_products_db)]
TransactionsDb = Annotated[
  AsyncSession, Depends(dependencies.get_transactions_db)
]
CODE_STATUSES = ("available", "assigned", "redeemed", "void")


@router.get(
  "/deals",
  response_model=list[dict[str, Any]],
  operation_id="list_deals",
  dependencies=[SimulationSecret],
)
async def list_deals(
  products_session: Annotated[
    AsyncSession, Depends(dependencies.get_products_db)
  ],
  transactions_session: Annotated[
    AsyncSession, Depends(dependencies.get_transactions_db)
  ],
) -> list[dict[str, Any]]:
  """Return every deal with its merchant and options, for the shop's tools."""
  listings = await catalog.load_catalog(products_session, transactions_session)
  deals = []
  for listing in listings:
    deal = listing.deal
    if not deal:
      continue
    deals.append(
      {
        "id": deal.id,
        "title": deal.title,
        "merchant": deal.merchant,
        "category": deal.category,
        "location": deal.location,
        "window": {
          "not_before": deal.service_not_before,
          "not_after": deal.service_not_after,
        }
        if deal.service_not_before
        else None,
        "refundability": deal.refundability,
        "options": [
          {
            "id": offer.product.id,
            "title": offer.option.title if offer.option else "",
            "price": offer.product.price,
            "stock": offer.stock,
          }
          for offer in listing.offers
        ],
      }
    )
  return deals


@router.get(
  "/orders",
  response_model=list[dict[str, Any]],
  operation_id="list_orders",
  dependencies=[SimulationSecret],
)
async def list_orders(
  transactions_session: Annotated[
    AsyncSession, Depends(dependencies.get_transactions_db)
  ],
) -> list[dict[str, Any]]:
  """Return every order the shop took, newest first. For the shop's console."""
  return await db.list_orders(transactions_session)


@router.get(
  "/orders/{id}/payment",
  response_model=dict[str, Any],
  operation_id="order_payment",
  dependencies=[SimulationSecret],
)
async def order_payment(
  order_id: Annotated[str, Path(..., alias="id")],
  vouchers: Vouchers,
) -> dict[str, Any]:
  """Return what the payment rail knows about the order's payment."""
  return await vouchers.payment(order_id)


@router.post(
  "/orders/{id}/cancel",
  response_model=dict[str, Any],
  operation_id="cancel_order_by_merchant",
  dependencies=[SimulationSecret],
)
async def cancel_order_by_merchant(
  order_id: Annotated[str, Path(..., alias="id")],
  vouchers: Vouchers,
) -> dict[str, Any]:
  """Record that the merchant cancelled the service."""
  return await vouchers.cancel_by_merchant(order_id)


@router.post(
  "/orders/{id}/redeem",
  response_model=dict[str, Any],
  operation_id="redeem_order",
  dependencies=[SimulationSecret],
)
async def redeem_order(
  order_id: Annotated[str, Path(..., alias="id")],
  vouchers: Vouchers,
  honoured: Annotated[bool, Body(embed=True)] = True,
) -> dict[str, Any]:
  """Record a redemption at the venue, or a failed one (`honoured: false`)."""
  return await vouchers.redeem(order_id, honoured)


@router.post(
  "/orders/{id}/refund",
  response_model=dict[str, Any],
  operation_id="refund_order",
  dependencies=[SimulationSecret],
)
async def refund_order(
  order_id: Annotated[str, Path(..., alias="id")],
  vouchers: Vouchers,
  reason: Annotated[str, Body(embed=True)] = "",
) -> dict[str, Any]:
  """Refund the order's payment and void its vouchers."""
  return await vouchers.refund(order_id, reason)


@router.post(
  "/testing/booking-fee",
  response_model=dict[str, Any],
  operation_id="set_booking_fee",
  dependencies=[SimulationSecret],
)
async def set_booking_fee(
  vouchers: Vouchers,
  amount: Annotated[int, Body(embed=True, ge=0)],
) -> dict[str, Any]:
  """Add a booking fee (cents) to every checkout from now on; 0 removes it."""
  await vouchers.set_booking_fee(amount)
  return {"booking_fee": amount}


@router.get(
  "/inventory",
  response_model=list[dict[str, Any]],
  operation_id="read_inventory",
  dependencies=[SimulationSecret],
)
async def read_inventory(
  products_session: ProductsDb, transactions_session: TransactionsDb
) -> list[dict[str, Any]]:
  """Count each option's codes by status. `available` is what can be sold."""
  deals = {deal.id: deal for deal in await db.list_deals(products_session)}
  rows = []
  for option in await db.list_options(products_session):
    counts = await db.count_codes(transactions_session, option.product_id)
    rows.append(
      {
        "deal_id": option.deal_id,
        "deal": deals[option.deal_id].title,
        "option_id": option.product_id,
        "option": option.title,
        "codes": {
          "total": sum(counts.values()),
          **{status: counts.get(status, 0) for status in CODE_STATUSES},
        },
      }
    )
  return rows


async def _require_option(products_session: AsyncSession, option_id: str):
  if not await db.get_option(products_session, option_id):
    raise ResourceNotFoundError(f"Option {option_id} not found")


@router.get(
  "/inventory/{option_id}/codes",
  response_model=list[dict[str, Any]],
  operation_id="read_codes",
  dependencies=[SimulationSecret],
)
async def read_codes(
  option_id: str,
  products_session: ProductsDb,
  transactions_session: TransactionsDb,
) -> list[dict[str, Any]]:
  """List an option's codes with their status and the order that took them."""
  await _require_option(products_session, option_id)
  return [
    {"code": code.code, "status": code.status, "order_id": code.order_id}
    for code in await db.list_codes(transactions_session, option_id)
  ]


@router.post(
  "/inventory/{option_id}/codes",
  response_model=dict[str, Any],
  operation_id="add_codes",
  dependencies=[SimulationSecret],
)
async def add_codes(
  option_id: str,
  products_session: ProductsDb,
  transactions_session: TransactionsDb,
  codes: Annotated[list[str], Body(embed=True)],
) -> dict[str, Any]:
  """Add codes to an option's pool. Codes the shop already has are skipped."""
  await _require_option(products_session, option_id)
  added = await db.add_codes(transactions_session, option_id, codes)
  await transactions_session.commit()
  return {"added": added, "skipped": len(codes) - added}


@router.get(
  "/ledger",
  response_model=list[dict[str, Any]],
  operation_id="read_ledger",
  dependencies=[SimulationSecret],
)
async def read_ledger(after: int = 0) -> list[dict[str, Any]]:
  """Return ledger events, skipping the first `after`."""
  return ledger.read(after)
