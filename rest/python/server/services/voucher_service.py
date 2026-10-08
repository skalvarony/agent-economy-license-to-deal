"""Voucher logic: what turns the sample shop into one that sells services.

A deal (`db.Deal`) is a service sold as vouchers: when and where it happens,
whether it can be refunded and how the voucher is redeemed. It is bought
through one of its options (`db.DealOption`), each a product with its own
price and its own pool of codes (`db.VoucherCode`). The codes left in the pool
are the option's inventory.

The deal's terms travel on the checkout's line items, so the buyer approves
them with the price. A deal is either for a set date or open-dated; an
open-dated deal counts its deadlines in days from the purchase, and they
become dates when the order is placed. Then each deal line takes one code per
unit from its option's pool, and this module moves the voucher through its
life:

  voucher.status      issued -> redeemed | refunded
  redemption.status   unredeemed -> redeemed | redemption_failed |
                      cancelled_by_merchant
  code status         available -> assigned -> redeemed | void

Redeeming and cancelling are the merchant's actions. In this demo they are
endpoints on the shop, standing in for the venue.
"""

import copy
import datetime
from typing import Any

import db
from exceptions import InvalidRequestError
from exceptions import OutOfStockError
from exceptions import PurchaseLimitError
from exceptions import ResourceNotFoundError
from exceptions import VoucherStateError
from services import booking_service
from services import coin_service
from services import ledger
from services.payment_rail import PaymentRail
from sqlalchemy.ext.asyncio import AsyncSession

BOOKING_FEE_SETTING = "booking_fee"


def terms(deal: db.Deal, option: db.DealOption | None = None) -> dict[str, Any]:
  """Return a deal's voucher terms in the shape line items carry them.

  With an option, the terms also say which one was chosen and what it
  includes. Fields that don't apply to the deal are left out.
  """

  def present(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}

  service = {
    "deal_id": deal.id,
    "category": deal.category,
    "merchant": deal.merchant,
    "location": deal.location,
  }
  if deal.service_not_before:
    service["window"] = {
      "not_before": deal.service_not_before,
      "not_after": deal.service_not_after,
    }
  if option:
    service["option"] = option.title
    service["includes"] = option.includes or []
  if booking := booking_service.summary(deal):
    # Booked for a date and time when bought; the slot is added on the
    # checkout and the order (services/booking_service.py).
    service["booking"] = booking
  return {
    "service": service,
    "cancellation": present(
      {
        "refundability": deal.refundability,
        "refundable_until": deal.refundable_until,
        "refund_days": deal.refund_days,
      }
    ),
    "voucher": present(
      {
        "expires_at": deal.voucher_expires_at,
        "valid_days": deal.voucher_valid_days,
        # The promotional value can run out; what was paid never does.
        "amount_paid_expires": False,
      }
    ),
    "redemption": {
      "method": deal.redemption_method,
      "instructions": deal.redemption_instructions,
      "appointment_required": bool(deal.appointment_required),
    },
    "purchase": present(
      {
        "limit_per_person": deal.limit_per_person,
        "repurchase_days": deal.repurchase_days,
      }
    ),
  }


def _on_purchase(deal: db.Deal, line: dict[str, Any]) -> None:
  """Fix what only exists once the deal is bought: dates and the contact."""
  now = datetime.datetime.now().astimezone().replace(microsecond=0)

  def after(days: int) -> str:
    return (now + datetime.timedelta(days=days)).isoformat()

  if deal.voucher_valid_days:
    line["voucher"]["expires_at"] = after(deal.voucher_valid_days)
  if deal.refund_days:
    line["cancellation"]["refundable_until"] = after(deal.refund_days)
  if deal.booking_contact:
    line["redemption"]["booking_contact"] = deal.booking_contact


async def attach_terms(products_session: AsyncSession, line: Any) -> None:
  """Put the voucher terms on a checkout line item, if it is a deal option."""
  found = await db.get_option(products_session, line.item.id)
  if found:
    for key, value in terms(*found).items():
      setattr(line, key, value)


async def is_voucher_checkout(
  products_session: AsyncSession, checkout: Any
) -> bool:
  """Whether every line is a deal option, so nothing needs shipping."""
  for line in checkout.line_items:
    if not await db.get_option(products_session, line.item.id):
      return False
  return True


async def booking_fee(transactions_session: AsyncSession) -> int:
  """Return the fee, in cents, the shop adds to every checkout."""
  return await db.get_setting(transactions_session, BOOKING_FEE_SETTING) or 0


async def check_purchase_limits(
  products_session: AsyncSession,
  transactions_session: AsyncSession,
  checkout: Any,
  count_history: bool,
) -> None:
  """Refuse a checkout that asks for more of a deal than one person may buy.

  The units in the checkout are always counted. With `count_history`, so are
  the ones the buyer already bought, looked up by email; they stop counting
  once the deal's repurchase period has passed, or when refunded.
  """
  units: dict[str, int] = {}
  deals: dict[str, db.Deal] = {}
  for line in checkout.line_items:
    found = await db.get_option(products_session, line.item.id)
    if found and found[0].limit_per_person:
      deal = found[0]
      deals[deal.id] = deal
      units[deal.id] = units.get(deal.id, 0) + line.quantity

  email = getattr(checkout.buyer, "email", None)
  for deal_id, wanted in units.items():
    deal = deals[deal_id]
    rule = f"Limit {deal.limit_per_person} per person for {deal.title}."
    if wanted > deal.limit_per_person:
      raise PurchaseLimitError(rule)
    if not count_history:
      continue
    if not email:
      raise InvalidRequestError(
        f"{rule} The buyer's email is needed to buy this deal."
      )
    since = None
    if deal.repurchase_days:
      since = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=deal.repurchase_days)
      ).isoformat()
    purchases = await db.get_purchases(
      transactions_session, deal_id, str(email), since
    )
    if sum(p.quantity for p in purchases) + wanted > deal.limit_per_person:
      again = "It can't be bought again."
      if deal.repurchase_days:
        first = datetime.datetime.fromisoformat(purchases[0].purchased_at)
        free = first + datetime.timedelta(days=deal.repurchase_days)
        again = f"It can be bought again from {free:%-d %b %Y}."
      raise PurchaseLimitError(f"{rule} You already bought it. {again}")


async def issue_vouchers(
  products_session: AsyncSession,
  transactions_session: AsyncSession,
  order: dict[str, Any],
  buyer_email: str | None,
) -> list[str]:
  """Give each deal line of a new order its codes, one per unit bought.

  The codes come out of the option's pool. Returns every code issued.
  """
  issued = []
  for line in order["line_items"]:
    found = await db.get_option(products_session, line["item"]["id"])
    if not found:
      continue
    deal, _ = found
    units = line["quantity"]["total"]
    codes = await db.assign_codes(
      transactions_session, line["item"]["id"], units, order["id"], line["id"]
    )
    if codes is None:
      raise OutOfStockError(
        f"Item {line['item']['id']} is out of stock", status_code=409
      )
    line.update(terms(*found))
    _on_purchase(deal, line)
    line["voucher"].update(codes=codes, status="issued")
    line["redemption"]["status"] = "unredeemed"
    await db.record_purchase(
      transactions_session, order["id"], deal.id, buyer_email, units
    )
    issued += codes
  return issued


def _release_slot(line: dict[str, Any]) -> None:
  """Mark a line's booking as released, if it had one."""
  booking = (line.get("service") or {}).get("booking")
  if booking and booking.get("starts_at"):
    booking["status"] = "released"


class VoucherService:
  """Merchant and refund actions on the vouchers of a placed order."""

  def __init__(self, transactions_session: AsyncSession, rail: PaymentRail):
    """Initialize VoucherService."""
    self.transactions_session = transactions_session
    self.rail = rail

  async def cancel_by_merchant(self, order_id: str) -> dict[str, Any]:
    """Record that the merchant won't deliver the service."""
    order, lines = await self._load(order_id)
    self._require(lines, "cancel", {"unredeemed", "redemption_failed"})
    for line in lines:
      line["redemption"]["status"] = "cancelled_by_merchant"
      _release_slot(line)
    await booking_service.release(self.transactions_session, order_id)
    await self._save(order, "MERCHANT_CANCELLED")
    return order

  async def redeem(self, order_id: str, honoured: bool) -> dict[str, Any]:
    """Record a redemption attempt at the venue.

    `honoured` is False when the customer showed up and the merchant couldn't
    deliver (closed, overbooked).
    """
    order, lines = await self._load(order_id)
    self._require(lines, "redeem", {"unredeemed", "redemption_failed"})
    for line in lines:
      if honoured:
        line["redemption"]["status"] = "redeemed"
        line["voucher"]["status"] = "redeemed"
      else:
        line["redemption"]["status"] = "redemption_failed"
    if honoured:
      await db.set_order_codes_status(
        self.transactions_session, order_id, "redeemed"
      )
    event = "VOUCHER_REDEEMED" if honoured else "REDEMPTION_FAILED"
    await self._save(order, event)
    return order

  async def refund(self, order_id: str, reason: str) -> dict[str, Any]:
    """Give the payment back and void the vouchers and their codes.

    A voided code does not return to the pool: it was already handed out.

    Whether a refund is due is not decided here; the caller has decided.
    """
    order, lines = await self._load(order_id)
    payment = order.get("payment") or {}
    if payment.get("status") != "captured":
      raise VoucherStateError(
        f"Order {order_id} has no captured payment to refund"
      )

    # Each part goes back the way it was paid: the card's to the card, the
    # coins to the wallet. Coins the order earned are taken back.
    if payment.get("payment_id"):
      await self.rail.refund(payment["payment_id"], payment["amount"])
    coins = await coin_service.refund(self.transactions_session, order_id)
    payment["status"] = "refunded"
    for line in lines:
      line["voucher"]["status"] = "refunded"
      _release_slot(line)
    await booking_service.release(self.transactions_session, order_id)
    await db.set_order_codes_status(self.transactions_session, order_id, "void")
    await db.mark_purchases_refunded(self.transactions_session, order_id)
    detail = {
      "amount": payment["amount"],
      "coins": coins["returned"],
      "reason": reason,
    }
    await self._save(order, "REFUND_AUTHORISED", detail)
    return order

  async def payment(self, order_id: str) -> dict[str, Any]:
    """Return what the rail knows about an order's payment, for the console.

    An order paid with coins alone has no rail payment: `id` is None.
    """
    order, _ = await self._load(order_id)
    payment = order.get("payment") or {}
    if not payment.get("payment_id"):
      return {
        "rail": payment.get("rail"),
        "id": None,
        "mode": None,
        "status": payment.get("status"),
        "amount": payment.get("amount", 0),
        "captured": None,
        "refunded": None,
        "currency": None,
        "card": None,
        "created": None,
        "refunds": [],
        "url": None,
      }
    return await self.rail.inspect(payment["payment_id"])

  async def set_booking_fee(self, amount: int) -> None:
    """Switch the booking fee on (cents) or off (0) for every checkout."""
    await db.set_setting(self.transactions_session, BOOKING_FEE_SETTING, amount)
    await self.transactions_session.commit()

  async def _load(self, order_id: str) -> tuple[dict[str, Any], list[dict]]:
    """Return a copy of the order and its voucher lines."""
    stored = await db.get_order(self.transactions_session, order_id)
    if not stored:
      raise ResourceNotFoundError("Order not found")
    # A copy, so the saved JSON is seen as changed when it is written back.
    order = copy.deepcopy(stored)
    return order, [li for li in order["line_items"] if "voucher" in li]

  def _require(self, lines: list[dict], action: str, allowed: set[str]) -> None:
    """Ensure every voucher is live and its redemption state allows `action`."""
    if not lines:
      raise VoucherStateError("Order has no vouchers")
    for line in lines:
      voucher = line["voucher"]["status"]
      redemption = line["redemption"]["status"]
      if voucher != "issued" or redemption not in allowed:
        raise VoucherStateError(
          f"Cannot {action}: voucher is {voucher}, redemption is {redemption}"
        )

  async def _save(
    self, order: dict[str, Any], event: str, detail: dict | None = None
  ) -> None:
    await db.save_order(self.transactions_session, order["id"], order)
    await self.transactions_session.commit()
    ledger.emit(
      event,
      checkout_id=order.get("checkout_id", ""),
      payment_id=(order.get("payment") or {}).get("payment_id", ""),
      detail={"order_id": order["id"], **(detail or {})},
    )
