"""Wallet routes: a customer's coins in this shop.

`/wallet` is for the buyer's agent: it tells how many coins a buyer holds
here and what the shop gives back, so the agent can weigh this shop against
another. The `/wallets` routes are the shop's own and need the
`Simulation-Secret` header: they grant coins and show a wallet's movements.
"""

from typing import Annotated, Any

import config
import db
import dependencies
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from services import coin_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

TransactionsDb = Annotated[
  AsyncSession, Depends(dependencies.get_transactions_db)
]
SimulationSecret = Depends(dependencies.verify_simulation_secret)


@router.get(
  "/wallet", response_model=dict[str, Any], operation_id="read_wallet"
)
async def read_wallet(
  email: str,
  common_headers: Annotated[
    dependencies.CommonHeaders, Depends(dependencies.common_headers)
  ],
  transactions_session: TransactionsDb,
) -> dict[str, Any]:
  """Tell a buyer's agent what the buyer's wallet holds in this shop."""
  del common_headers  # Unused
  return {
    "email": email.lower(),
    "balance": await db.coin_balance(transactions_session, email),
    "coin_value": {
      "amount": coin_service.COIN_VALUE,
      "currency": config.get_default_currency(),
    },
    "back_percent": coin_service.back_percent(),
  }


@router.post(
  "/wallets/grant",
  response_model=dict[str, Any],
  operation_id="grant_coins",
  dependencies=[SimulationSecret],
)
async def grant_coins(
  transactions_session: TransactionsDb,
  email: Annotated[str, Body(embed=True)],
  coins: Annotated[int, Body(embed=True, gt=0)],
) -> dict[str, Any]:
  """Give a customer coins, e.g. as an incentive to come back."""
  await db.add_coins(transactions_session, email, coins, "granted")
  await transactions_session.commit()
  return {
    "email": email.lower(),
    "balance": await db.coin_balance(transactions_session, email),
  }


@router.get(
  "/wallets/{email}",
  response_model=dict[str, Any],
  operation_id="read_wallet_history",
  dependencies=[SimulationSecret],
)
async def read_wallet_history(
  email: str, transactions_session: TransactionsDb
) -> dict[str, Any]:
  """Show a wallet's balance and every movement behind it, newest first."""
  entries = await db.list_coin_entries(transactions_session, email=email)
  return {
    "email": email.lower(),
    "balance": await db.coin_balance(transactions_session, email),
    "entries": [
      {
        "coins": entry.coins,
        "reason": entry.reason,
        "order_id": entry.order_id,
        "at": entry.at,
      }
      for entry in entries
    ],
  }
