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

"""Database management and persistence layer for the UCP sample REST server.

This module provides the schema definitions, database session management, and
asynchronous data access helpers used by the server. It utilizes SQLAlchemy with
SQLite (via aiosqlite) and implements a multi-database architecture separating
product catalog data from transactional session and order data.

Key features include:
- `DatabaseManager`: Handles asynchronous engine initialization and session
factory
  setup for both 'Products' and 'Transactions' databases.
- WAL Mode: Automatically enables SQLite Write-Ahead Logging to support
concurrent
  access from the main server and the webhook server.
- Declarative Models: Defines tables for products, inventory, checkout sessions,
  orders, request logging, and idempotency tracking.
- Data Access Helpers: A suite of asynchronous functions for CRUD operations on
  the database models.
"""

import datetime
import logging
from typing import Any
import uuid

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import func
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

ProductBase = declarative_base()
TransactionBase = declarative_base()


class DatabaseManager:
  """Manages database engines and sessions without using global variables."""

  def __init__(self) -> None:
    """Initialize DatabaseManager."""
    self.products_engine: AsyncEngine | None = None
    self.transactions_engine: AsyncEngine | None = None
    self.products_session_factory: sessionmaker | None = None
    self.transactions_session_factory: sessionmaker | None = None

  async def init_dbs(self, products_path: str, transactions_path: str) -> None:
    """Initialize database engines and creates tables."""
    # Products DB Setup
    prod_url = f"sqlite+aiosqlite:///{products_path}"
    self.products_engine = create_async_engine(prod_url, echo=False)

    # Enable WAL mode for Products DB
    async with self.products_engine.connect() as conn:
      await conn.execute(text("PRAGMA journal_mode=WAL"))

    self.products_session_factory = sessionmaker(
      self.products_engine, expire_on_commit=False, class_=AsyncSession
    )

    async with self.products_engine.begin() as conn:
      await conn.run_sync(ProductBase.metadata.create_all)

    # Transactions DB Setup (includes Inventory)
    trans_url = f"sqlite+aiosqlite:///{transactions_path}"
    self.transactions_engine = create_async_engine(trans_url, echo=False)

    # Enable WAL mode for Transactions DB
    async with self.transactions_engine.connect() as conn:
      await conn.execute(text("PRAGMA journal_mode=WAL"))

    self.transactions_session_factory = sessionmaker(
      self.transactions_engine, expire_on_commit=False, class_=AsyncSession
    )

    async with self.transactions_engine.begin() as conn:
      await conn.run_sync(TransactionBase.metadata.create_all)

  async def close(self) -> None:
    """Close all database engines."""
    if self.products_engine:
      await self.products_engine.dispose()
    if self.transactions_engine:
      await self.transactions_engine.dispose()


# Global manager instance (to be initialized via lifespan)
manager = DatabaseManager()


class Product(ProductBase):
  """Product database model."""

  __tablename__ = "products"

  id = Column(String, primary_key=True)
  title = Column(String)
  price = Column(Integer)  # Price in cents
  image_url = Column(String, nullable=True)


class Promotion(ProductBase):
  """Promotion database model."""

  __tablename__ = "promotions"

  id = Column(String, primary_key=True)
  type = Column(String)  # e.g., 'free_shipping'
  min_subtotal = Column(Integer, nullable=True)  # In cents
  eligible_item_ids = Column(JSON, nullable=True)  # List of item IDs
  description = Column(String)


class Deal(ProductBase):
  """A service sold as vouchers: what, where, when and on what terms.

  A deal isn't bought directly. It has one or more options (`DealOption`), and
  each option is a product with its own price and its own pool of codes.
  """

  __tablename__ = "deals"

  id = Column(String, primary_key=True)
  title = Column(String)
  merchant = Column(String)  # Who delivers the service
  category = Column(String)
  description = Column(String)
  location = Column(String)
  # What the options differ in, e.g. 'Duration'.
  option_label = Column(String, nullable=True)
  # A deal is either for a set date (a service window and fixed deadlines) or
  # open-dated (deadlines counted in days from the purchase). All times are
  # ISO 8601 with an offset.
  service_not_before = Column(String, nullable=True)
  service_not_after = Column(String, nullable=True)
  # 'refundable', 'partially_refundable' or 'non_refundable'
  refundability = Column(String)
  refundable_until = Column(String, nullable=True)
  refund_days = Column(Integer, nullable=True)  # Open-dated: days to refund
  voucher_expires_at = Column(String, nullable=True)
  voucher_valid_days = Column(Integer, nullable=True)  # Open-dated: validity
  redemption_method = Column(String)
  redemption_instructions = Column(String)
  appointment_required = Column(Boolean, default=False)
  # How to book. Only shown on the voucher, once the deal is bought.
  booking_contact = Column(String, nullable=True)
  # Most units one person may buy, and after how many days they may buy again
  # (never again when unset).
  limit_per_person = Column(Integer, nullable=True)
  repurchase_days = Column(Integer, nullable=True)
  highlights = Column(JSON, nullable=True)  # Short selling points, as a list
  about_merchant = Column(String, nullable=True)
  # What shoppers see next to the deal. Simulated, like the rest of it.
  rating = Column(Float, nullable=True)  # Out of 5
  reviews = Column(Integer, nullable=True)
  bought = Column(Integer, nullable=True)


class DealOption(ProductBase):
  """One way to buy a deal, e.g. '60 minutes'. Its price is the product's."""

  __tablename__ = "deal_options"

  product_id = Column(String, primary_key=True)  # The product that is bought
  deal_id = Column(String, index=True)
  title = Column(String)
  description = Column(String, nullable=True)
  includes = Column(JSON, nullable=True)  # What the buyer gets, as a list
  list_price = Column(Integer, nullable=True)  # Price before the deal, cents
  position = Column(Integer, default=0)  # Order within the deal


class DealReview(ProductBase):
  """A customer's review of a deal. Simulated, like the ratings."""

  __tablename__ = "deal_reviews"

  id = Column(Integer, primary_key=True, autoincrement=True)
  deal_id = Column(String, index=True)
  author = Column(String)
  rating = Column(Integer)  # Out of 5
  date = Column(String)  # ISO 8601 day
  text = Column(String)
  reply = Column(String, nullable=True)  # The merchant's answer, if any


class Inventory(TransactionBase):
  """Inventory database model."""

  __tablename__ = "inventory"

  product_id = Column(String, primary_key=True)
  quantity = Column(Integer, default=0)


class Customer(TransactionBase):
  """Customer database model."""

  __tablename__ = "customers"

  id = Column(String, primary_key=True)
  name = Column(String)
  email = Column(String, index=True)

  addresses = relationship("CustomerAddress", back_populates="customer")


class CustomerAddress(TransactionBase):
  """Customer address database model."""

  __tablename__ = "customer_addresses"

  id = Column(String, primary_key=True)
  customer_id = Column(String, ForeignKey("customers.id"))
  street_address = Column(String)
  city = Column(String)
  state = Column(String)
  postal_code = Column(String)
  country = Column(String)

  customer = relationship("Customer", back_populates="addresses")


class CheckoutSession(TransactionBase):
  """Checkout session database model."""

  __tablename__ = "checkouts"

  id = Column(String, primary_key=True)
  status = Column(String)
  # SQLAlchemy JSON type handles serialization automatically
  data = Column(JSON)


class CartSession(TransactionBase):
  """Cart session database model."""

  __tablename__ = "carts"

  id = Column(String, primary_key=True)
  data = Column(JSON)


class Order(TransactionBase):
  """Order database model."""

  __tablename__ = "orders"

  id = Column(String, primary_key=True)
  data = Column(JSON)


class RequestLog(TransactionBase):
  """HTTP request log database model."""

  __tablename__ = "request_logs"

  id = Column(Integer, primary_key=True, autoincrement=True)
  timestamp = Column(String)
  method = Column(String)
  url = Column(String)
  checkout_id = Column(String, nullable=True)
  payload = Column(JSON, nullable=True)


class IdempotencyRecord(TransactionBase):
  """Idempotency record database model."""

  __tablename__ = "idempotency_records"

  key = Column(String, primary_key=True)
  request_hash = Column(String)
  response_status = Column(Integer)
  response_body = Column(JSON)
  created_at = Column(String)


class PaymentInstrument(TransactionBase):
  """Payment instrument database model."""

  __tablename__ = "payment_instruments"

  id = Column(String, primary_key=True)
  type = Column(String)
  brand = Column(String)
  last_digits = Column(String)
  token = Column(String)
  handler_id = Column(String)


class Discount(TransactionBase):
  """Discount database model."""

  __tablename__ = "discounts"

  code = Column(String, primary_key=True)
  type = Column(String)  # 'percentage' or 'fixed_amount'
  value = Column(Integer)  # Percentage (e.g., 10) or Amount in cents
  description = Column(String)


class ShippingRate(TransactionBase):
  """Shipping rate database model."""

  __tablename__ = "shipping_rates"

  id = Column(String, primary_key=True)
  country_code = Column(String)  # e.g., 'US', 'default'
  service_level = Column(String)  # e.g., 'standard', 'express'
  price = Column(Integer)  # In cents
  title = Column(String)


class VoucherCode(TransactionBase):
  """One code of an option's pool. The available codes are its inventory.

  status: 'available' -> 'assigned' (sold) -> 'redeemed' (used) or 'void'
  (refunded). A code is sold once and never returns to the pool.
  """

  __tablename__ = "voucher_codes"

  code = Column(String, primary_key=True)
  product_id = Column(String, index=True)
  status = Column(String, default="available", index=True)
  order_id = Column(String, nullable=True, index=True)
  line_id = Column(String, nullable=True)


class DealPurchase(TransactionBase):
  """Units of a deal a buyer bought in an order, for per-person limits."""

  __tablename__ = "deal_purchases"

  id = Column(Integer, primary_key=True, autoincrement=True)
  order_id = Column(String, index=True)
  deal_id = Column(String, index=True)
  buyer_email = Column(String, index=True)
  quantity = Column(Integer)
  purchased_at = Column(String)  # ISO 8601, UTC
  refunded = Column(Boolean, default=False)


class User(TransactionBase):
  """A person with an account in this shop."""

  __tablename__ = "users"

  id = Column(String, primary_key=True)
  email = Column(String, unique=True, index=True)  # Lower case
  full_name = Column(String)
  password_hash = Column(String)  # Never the password itself
  created_at = Column(String)  # ISO 8601, UTC


class UserSession(TransactionBase):
  """A signed-in browser. Its token is what the session cookie holds."""

  __tablename__ = "user_sessions"

  token = Column(String, primary_key=True)
  user_id = Column(String, index=True)
  expires_at = Column(String)  # ISO 8601, UTC


class OrderOwner(TransactionBase):
  """Which account placed an order on the web. Agents' orders have none."""

  __tablename__ = "order_owners"

  order_id = Column(String, primary_key=True)
  user_id = Column(String, index=True)
  created_at = Column(String)  # ISO 8601, UTC


class CoinEntry(TransactionBase):
  """One movement in a customer's coin wallet in this shop.

  There is no stored balance: a wallet's balance is the sum of its entries,
  so every coin can be traced to the order that earned or spent it.
  """

  __tablename__ = "coin_entries"

  id = Column(Integer, primary_key=True, autoincrement=True)
  email = Column(String, index=True)  # Lower case; the wallet's owner
  coins = Column(Integer)  # Positive in, negative out
  # 'granted', 'earned', 'spent', 'refunded' (spent coins given back) or
  # 'reversed' (earned coins taken back)
  reason = Column(String)
  order_id = Column(String, nullable=True, index=True)
  at = Column(String)  # ISO 8601, UTC


class ShopSetting(TransactionBase):
  """Key-value switch for this shop, e.g. the demo's booking fee."""

  __tablename__ = "shop_settings"

  key = Column(String, primary_key=True)
  value = Column(JSON)


# --- Data Access Helpers ---


async def get_deal(session: AsyncSession, deal_id: str) -> Deal | None:
  """Retrieve a deal by ID."""
  return await session.get(Deal, deal_id)


async def get_option(
  session: AsyncSession, product_id: str
) -> tuple[Deal, DealOption] | None:
  """Retrieve the deal and option a product is, if it is sold as a voucher."""
  option = await session.get(DealOption, product_id)
  if not option:
    return None
  return await session.get(Deal, option.deal_id), option


async def list_deals(session: AsyncSession) -> list[Deal]:
  """Retrieve every deal, ordered by ID."""
  result = await session.execute(select(Deal).order_by(Deal.id))
  return list(result.scalars().all())


async def list_reviews(session: AsyncSession, deal_id: str) -> list[DealReview]:
  """Retrieve a deal's reviews, newest first."""
  result = await session.execute(
    select(DealReview)
    .where(DealReview.deal_id == deal_id)
    .order_by(DealReview.date.desc())
  )
  return list(result.scalars().all())


async def list_options(session: AsyncSession) -> list[DealOption]:
  """Retrieve every deal option, in the order each deal shows them."""
  result = await session.execute(
    select(DealOption).order_by(DealOption.deal_id, DealOption.position)
  )
  return list(result.scalars().all())


async def list_products(session: AsyncSession) -> list[Product]:
  """Retrieve every product in the catalog, ordered by ID."""
  result = await session.execute(select(Product).order_by(Product.id))
  return list(result.scalars().all())


async def count_codes(session: AsyncSession, product_id: str) -> dict[str, int]:
  """Count an option's codes by status. Empty when it has no pool."""
  result = await session.execute(
    select(VoucherCode.status, func.count())
    .where(VoucherCode.product_id == product_id)
    .group_by(VoucherCode.status)
  )
  return dict(map(tuple, result.all()))


async def list_codes(
  session: AsyncSession, product_id: str
) -> list[VoucherCode]:
  """Retrieve every code of an option's pool."""
  result = await session.execute(
    select(VoucherCode)
    .where(VoucherCode.product_id == product_id)
    .order_by(VoucherCode.code)
  )
  return list(result.scalars().all())


async def add_codes(
  session: AsyncSession, product_id: str, codes: list[str]
) -> int:
  """Add codes to an option's pool; codes already known are skipped.

  Returns how many were added.
  """
  wanted = list(dict.fromkeys(code.strip() for code in codes if code.strip()))
  known = await session.execute(
    select(VoucherCode.code).where(VoucherCode.code.in_(wanted))
  )
  taken = set(known.scalars().all())
  fresh = [code for code in wanted if code not in taken]
  session.add_all(
    VoucherCode(code=code, product_id=product_id) for code in fresh
  )
  return len(fresh)


async def assign_codes(
  session: AsyncSession,
  product_id: str,
  quantity: int,
  order_id: str,
  line_id: str,
) -> list[str] | None:
  """Take codes from an option's pool for an order line.

  Returns the codes, or None when the pool has fewer than `quantity` left.
  Each code is claimed with a conditional update, so two orders can never
  get the same one.
  """
  result = await session.execute(
    select(VoucherCode.code)
    .where(VoucherCode.product_id == product_id)
    .where(VoucherCode.status == "available")
    .order_by(VoucherCode.code)
    .limit(quantity)
  )
  codes = list(result.scalars().all())
  if len(codes) < quantity:
    return None
  claimed = await session.execute(
    update(VoucherCode)
    .where(VoucherCode.code.in_(codes))
    .where(VoucherCode.status == "available")
    .values(status="assigned", order_id=order_id, line_id=line_id)
  )
  return codes if claimed.rowcount == quantity else None


async def set_order_codes_status(
  session: AsyncSession, order_id: str, status: str
) -> None:
  """Move every code sold in an order to a new status."""
  await session.execute(
    update(VoucherCode)
    .where(VoucherCode.order_id == order_id)
    .values(status=status)
  )


async def record_purchase(
  session: AsyncSession,
  order_id: str,
  deal_id: str,
  buyer_email: str | None,
  quantity: int,
) -> None:
  """Remember that a buyer bought units of a deal."""
  session.add(
    DealPurchase(
      order_id=order_id,
      deal_id=deal_id,
      buyer_email=(buyer_email or "").lower(),
      quantity=quantity,
      purchased_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
  )


async def get_purchases(
  session: AsyncSession, deal_id: str, buyer_email: str, since: str | None
) -> list[DealPurchase]:
  """Retrieve a buyer's purchases of a deal that still count, oldest first.

  Refunded purchases don't count. `since` (ISO 8601, UTC) leaves out older
  ones.
  """
  query = (
    select(DealPurchase)
    .where(DealPurchase.deal_id == deal_id)
    .where(DealPurchase.buyer_email == buyer_email.lower())
    .where(DealPurchase.refunded.is_(False))
    .order_by(DealPurchase.purchased_at)
  )
  if since:
    query = query.where(DealPurchase.purchased_at >= since)
  return list((await session.execute(query)).scalars().all())


async def mark_purchases_refunded(session: AsyncSession, order_id: str) -> None:
  """Stop an order's purchases from counting towards per-person limits."""
  await session.execute(
    update(DealPurchase)
    .where(DealPurchase.order_id == order_id)
    .values(refunded=True)
  )


async def get_user(session: AsyncSession, user_id: str) -> User | None:
  """Retrieve an account by ID."""
  return await session.get(User, user_id)


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
  """Retrieve an account by email, whatever its letter case."""
  result = await session.execute(
    select(User).where(User.email == email.strip().lower())
  )
  return result.scalar_one_or_none()


async def set_order_owner(
  session: AsyncSession, order_id: str, user_id: str
) -> None:
  """Record which account placed an order."""
  session.add(
    OrderOwner(
      order_id=order_id,
      user_id=user_id,
      created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
  )


async def list_order_ids_bought_by(
  session: AsyncSession, email: str
) -> list[str]:
  """Retrieve the orders whose buyer has this email, whoever placed them."""
  result = await session.execute(
    select(Order.id).where(
      Order.data["buyer"]["email"].as_string() == email.lower()
    )
  )
  return list(result.scalars().all())


async def get_order_owner(session: AsyncSession, order_id: str) -> str | None:
  """Retrieve the ID of the account that placed an order, if any."""
  owner = await session.get(OrderOwner, order_id)
  return owner.user_id if owner else None


async def list_order_ids(session: AsyncSession, user_id: str) -> list[str]:
  """Retrieve the orders an account placed, newest first."""
  result = await session.execute(
    select(OrderOwner.order_id)
    .where(OrderOwner.user_id == user_id)
    .order_by(OrderOwner.created_at.desc())
  )
  return list(result.scalars().all())


async def coin_balance(session: AsyncSession, email: str | None) -> int:
  """Return how many coins a customer has in this shop."""
  if not email:
    return 0
  result = await session.execute(
    select(func.coalesce(func.sum(CoinEntry.coins), 0)).where(
      CoinEntry.email == email.lower()
    )
  )
  return result.scalar_one()


async def add_coins(
  session: AsyncSession,
  email: str,
  coins: int,
  reason: str,
  order_id: str | None = None,
) -> None:
  """Record coins going into a wallet (positive) or out of it (negative)."""
  if coins:
    session.add(
      CoinEntry(
        email=email.lower(),
        coins=coins,
        reason=reason,
        order_id=order_id,
        at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
      )
    )


async def list_coin_entries(
  session: AsyncSession, email: str | None = None, order_id: str | None = None
) -> list[CoinEntry]:
  """Retrieve a wallet's movements, or an order's, newest first."""
  query = select(CoinEntry).order_by(CoinEntry.id.desc())
  if email is not None:
    query = query.where(CoinEntry.email == email.lower())
  if order_id is not None:
    query = query.where(CoinEntry.order_id == order_id)
  return list((await session.execute(query)).scalars().all())


async def get_setting(session: AsyncSession, key: str) -> Any | None:
  """Retrieve a shop setting by key."""
  setting = await session.get(ShopSetting, key)
  return setting.value if setting else None


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
  """Save or update a shop setting."""
  existing = await session.get(ShopSetting, key)
  if existing:
    existing.value = value
  else:
    session.add(ShopSetting(key=key, value=value))


async def get_shipping_rates(
  session: AsyncSession, country_code: str
) -> list[ShippingRate]:
  """Retrieve shipping rates for a specific country and default rates.

  Args:
    session: The database session to use.
    country_code: The ISO country code (e.g., 'US') to fetch rates for.

  Returns:
    A list of ShippingRate objects matching the country or 'default'.

  """
  result = await session.execute(
    select(ShippingRate).where(
      ShippingRate.country_code.in_([country_code, "default"])
    )
  )
  return list(result.scalars().all())


async def get_discount(session: AsyncSession, code: str) -> Discount | None:
  """Retrieve a discount by code.

  Args:
    session: The database session to use.
    code: The discount code to look up.

  Returns:
    The Discount object if found, otherwise None.

  """
  result = await session.execute(
    select(Discount).where(func.upper(Discount.code) == code.upper())
  )
  return result.scalars().first()


async def get_discounts_by_codes(
  session: AsyncSession, codes: list[str]
) -> list[Discount]:
  """Retrieve multiple discounts by their codes in a single query.

  Args:
    session: The database session to use.
    codes: A list of discount codes to look up.

  Returns:
    A list of matching Discount objects.

  """
  result = await session.execute(
    select(Discount).where(
      func.upper(Discount.code).in_([c.upper() for c in codes])
    )
  )
  return list(result.scalars().all())


async def get_active_promotions(session: AsyncSession) -> list[Promotion]:
  """Retrieve all active promotions."""
  result = await session.execute(select(Promotion))
  return list(result.scalars().all())


async def get_product(session: AsyncSession, product_id: str) -> Product | None:
  """Retrieve a product by ID."""
  return await session.get(Product, product_id)


async def get_inventory(session: AsyncSession, product_id: str) -> int | None:
  """Retrieve the inventory quantity for a product.

  A product with a pool of voucher codes has as many units as codes left.
  """
  codes = await count_codes(session, product_id)
  if codes:
    return codes.get("available", 0)
  result = await session.execute(
    select(Inventory.quantity).where(Inventory.product_id == product_id)
  )
  return result.scalar_one_or_none()


async def get_customer_addresses(
  session: AsyncSession, email: str
) -> list[CustomerAddress]:
  """Retrieve addresses for a customer by email."""
  # First find customer by email
  result = await session.execute(
    select(Customer).where(Customer.email == email)
  )
  customer = result.scalar_one_or_none()
  if not customer:
    return []

  # Then get their addresses
  # Using explicit join or select if lazy loading is an issue with async session
  # But simple select on CustomerAddress is easier
  result = await session.execute(
    select(CustomerAddress).where(CustomerAddress.customer_id == customer.id)
  )
  return list(result.scalars().all())


async def get_customer(session: AsyncSession, email: str) -> Customer | None:
  """Retrieve a customer by email."""
  result = await session.execute(
    select(Customer).where(Customer.email == email)
  )
  return result.scalar_one_or_none()


async def save_customer_address(
  session: AsyncSession, email: str, address: dict[str, Any]
) -> str:
  """Save a customer address, reusing existing ID if content matches.

  Args:
    session: The database session.
    email: The customer's email.
    address: The address dictionary containing 'street_address', 'city', etc.

  Returns:
    The ID of the saved or existing address.

  """
  customer = await get_customer(session, email)
  if not customer:
    # Create customer if missing
    customer = Customer(id=str(uuid.uuid4()), email=email, name="Unknown")
    session.add(customer)
    # Flush to get ID if needed, though we set it manually
    await session.flush()

  # Check for existing address with same content
  stmt = select(CustomerAddress).where(
    CustomerAddress.customer_id == customer.id,
    CustomerAddress.street_address == address.get("street_address"),
    # Map locality to city
    CustomerAddress.city == address.get("address_locality"),
    # Map region to state
    CustomerAddress.state == address.get("address_region"),
    CustomerAddress.postal_code == address.get("postal_code"),
    CustomerAddress.country == address.get("address_country"),
  )
  result = await session.execute(stmt)
  existing_addr = result.scalar_one_or_none()

  if existing_addr:
    return existing_addr.id

  # Create new address
  new_id = address.get("id") or str(uuid.uuid4())
  new_addr = CustomerAddress(
    id=new_id,
    customer_id=customer.id,
    street_address=address.get("street_address"),
    # Map locality to city
    city=address.get("address_locality"),
    state=address.get("address_region"),
    postal_code=address.get("postal_code"),
    country=address.get("address_country"),
  )
  session.add(new_addr)
  return new_id


async def reserve_stock(
  session: AsyncSession, product_id: str, quantity: int
) -> bool:
  """Atomically decrements inventory if sufficient stock exists.

  A product with a pool of voucher codes isn't counted down here: its codes
  are claimed one by one when the order's vouchers are issued.
  """
  codes = await count_codes(session, product_id)
  if codes:
    return codes.get("available", 0) >= quantity
  stmt = (
    update(Inventory)
    .where(Inventory.product_id == product_id)
    .where(Inventory.quantity >= quantity)
    .values(quantity=Inventory.quantity - quantity)
  )
  result = await session.execute(stmt)
  return result.rowcount > 0


async def save_checkout(
  session: AsyncSession,
  checkout_id: str,
  status: str,
  checkout_obj: dict[str, Any],
) -> None:
  """Save or update a checkout session."""
  existing = await session.get(CheckoutSession, checkout_id)
  if existing:
    existing.status = status
    existing.data = checkout_obj
  else:
    new_checkout = CheckoutSession(
      id=checkout_id, status=status, data=checkout_obj
    )
    session.add(new_checkout)


async def get_checkout_session(
  session: AsyncSession, checkout_id: str
) -> dict[str, Any] | None:
  """Retrieve a checkout session by ID."""
  result = await session.get(CheckoutSession, checkout_id)
  if result:
    return result.data
  return None


async def save_cart(
  session: AsyncSession,
  cart_id: str,
  cart_obj: dict[str, Any],
) -> None:
  """Save or update a cart session."""
  existing = await session.get(CartSession, cart_id)
  if existing:
    existing.data = cart_obj
  else:
    new_cart = CartSession(id=cart_id, data=cart_obj)
    session.add(new_cart)


async def get_cart_session(
  session: AsyncSession, cart_id: str
) -> dict[str, Any] | None:
  """Retrieve a cart session by ID."""
  result = await session.get(CartSession, cart_id)
  if result:
    return result.data
  return None


async def delete_cart_session(session: AsyncSession, cart_id: str) -> None:
  """Delete a cart session by ID."""
  existing = await session.get(CartSession, cart_id)
  if existing:
    await session.delete(existing)


async def get_checkouts_by_cart_id(
  session: AsyncSession, cart_id: str
) -> list[dict[str, Any]]:
  """Retrieve all checkout sessions by cart ID."""
  stmt = select(CheckoutSession).where(
    CheckoutSession.data["cart_id"].as_string() == cart_id
  )
  result = await session.execute(stmt)
  return [r.data for r in result.scalars().all()]


async def save_order(
  session: AsyncSession, order_id: str, order_obj: dict[str, Any]
) -> None:
  """Save or update an order."""
  existing = await session.get(Order, order_id)
  if existing:
    existing.data = order_obj
  else:
    new_order = Order(id=order_id, data=order_obj)
    session.add(new_order)


async def list_orders(session: AsyncSession) -> list[dict[str, Any]]:
  """Return every order, newest first."""
  result = await session.execute(select(Order))
  orders = [order.data for order in result.scalars().all()]
  orders.sort(key=lambda order: order.get("placed_at") or "", reverse=True)
  return orders


async def get_order(
  session: AsyncSession, order_id: str
) -> dict[str, Any] | None:
  """Retrieve an order by ID."""
  result = await session.get(Order, order_id)
  if result:
    return result.data
  return None


async def log_request(
  session: AsyncSession,
  method: str,
  url: str,
  checkout_id: str | None = None,
  payload: dict[str, Any] | None = None,
) -> None:
  """Log an HTTP request to the database."""
  log_entry = RequestLog(
    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    method=method,
    url=url,
    checkout_id=checkout_id,
    payload=payload,
  )
  session.add(log_entry)


async def get_idempotency_record(
  session: AsyncSession, key: str
) -> IdempotencyRecord | None:
  """Retrieve an idempotency record by key."""
  return await session.get(IdempotencyRecord, key)


async def save_idempotency_record(
  session: AsyncSession,
  key: str,
  request_hash: str,
  response_status: int,
  response_body: dict[str, Any],
) -> None:
  """Save a new idempotency record."""
  record = IdempotencyRecord(
    key=key,
    request_hash=request_hash,
    response_status=response_status,
    response_body=response_body,
    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
  )
  session.add(record)
