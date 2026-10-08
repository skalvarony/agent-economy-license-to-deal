"""Coins: each shop's own currency, held in a wallet per customer.

A coin is worth one unit of the shop's currency. A customer earns coins as a
share of what they pay by card in the shop (the shop's `coins_back_percent`)
and can pay with them, alone or together with a card. The checkout says how
many coins are used and what is left for the card, so the buyer approves the
exact split. A refund goes back the way it was paid: coins to the wallet, the
rest to the card.

A wallet belongs to the buyer's email. The shop takes that email on trust.
"""

from typing import Any

import config
import db
from sqlalchemy.ext.asyncio import AsyncSession
from ucp_sdk.models.schemas.shopping.types.total import Total

# What one coin is worth, in the currency's minor unit (cents).
COIN_VALUE = 100


def back_percent() -> int:
  """Return the share of a card payment this shop gives back in coins."""
  return config.get_shop()["coins_back_percent"]


def earned_for(card_amount: int) -> int:
  """Return the whole coins a card payment of this many cents earns."""
  return card_amount * back_percent() // 100 // COIN_VALUE


def wanted(checkout: Any) -> int:
  """Return how many coins the checkout asks to pay with."""
  asked = getattr(checkout, "coins", None) or {}
  try:
    return max(0, int(asked.get("use") or 0))
  except (TypeError, ValueError):
    return 0


def applied(checkout: Any) -> int:
  """Return how many coins the checkout's totals take off."""
  return (getattr(checkout, "coins", None) or {}).get("applied", 0)


async def apply(
  transactions_session: AsyncSession, checkout: Any, amount_due: int
) -> int:
  """Pay part of a checkout with the buyer's coins.

  Takes off as many coins as were asked for, the wallet holds and the amount
  allows, adds the line to the checkout's totals and returns what is left for
  the card. The checkout also says what the wallet holds and what the
  purchase will earn.
  """
  email = getattr(checkout.buyer, "email", None)
  balance = await db.coin_balance(transactions_session, email and str(email))
  use = wanted(checkout)
  spend = min(use, balance, amount_due // COIN_VALUE)
  left = amount_due - spend * COIN_VALUE
  earns = earned_for(left)
  # A shop without coins, or a buyer without any, says nothing about them.
  if use or balance or earns:
    checkout.coins = {
      "use": use,
      "applied": spend,
      "balance": balance,
      "earns": earns,
    }
  if spend:
    checkout.totals.append(
      Total(
        type="coins",
        display_text=f"{spend} coins",
        amount=-spend * COIN_VALUE,
      )
    )
  return left


async def settle(
  transactions_session: AsyncSession,
  order_id: str,
  email: str | None,
  spent: int,
  card_amount: int,
) -> int:
  """Move the coins of a paid order: out what was spent, in what it earned.

  Returns the coins earned.
  """
  if not email:
    return 0
  earned = earned_for(card_amount)
  await db.add_coins(transactions_session, email, -spent, "spent", order_id)
  await db.add_coins(transactions_session, email, earned, "earned", order_id)
  return earned


async def refund(
  transactions_session: AsyncSession, order_id: str
) -> dict[str, int]:
  """Undo an order's coins: give back what was spent, take back what it earned.

  Earned coins already spent elsewhere can't be taken back; the wallet never
  goes below zero. Returns the coins returned and reversed.
  """
  entries = await db.list_coin_entries(transactions_session, order_id=order_id)
  if not entries:
    return {"returned": 0, "reversed": 0}
  email = entries[0].email
  spent = -sum(e.coins for e in entries if e.reason == "spent")
  earned = sum(e.coins for e in entries if e.reason == "earned")
  balance = await db.coin_balance(transactions_session, email) + spent
  await db.add_coins(transactions_session, email, spent, "refunded", order_id)
  taken = min(earned, max(balance, 0))
  await db.add_coins(transactions_session, email, -taken, "reversed", order_id)
  return {"returned": spent, "reversed": taken}
