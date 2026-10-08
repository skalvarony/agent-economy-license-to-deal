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

"""Database initialization script for the UCP sample server.

This script imports product and inventory data from CSV files into the
configured SQLite databases. It clears any existing data in the 'products'
and 'inventory' tables before populating them with the new dataset.

Usage:
  uv run import_csv.py --products_db_path=... --transactions_db_path=...
  --data_dir=...
"""

import os
import asyncio
import csv
import json
import logging
from pathlib import Path
from absl import app as absl_app
from absl import flags
import db
from db import Customer
from db import CustomerAddress
from db import CoinEntry
from db import Deal
from db import DealOption
from db import DealReview
from db import Discount
from db import Inventory
from db import PaymentInstrument
from db import Product
from db import Promotion
from db import ShippingRate
from db import User
from db import UserSession
from db import VoucherCode
from services import account_service
from sqlalchemy import delete

FLAGS = flags.FLAGS
flags.DEFINE_string("products_db_path", "products.db", "Path to products DB")
flags.DEFINE_string(
  "transactions_db_path", "transactions.db", "Path to transactions DB"
)
flags.DEFINE_string(
  "data_dir",
  str(Path(__file__).resolve().parent / "data"),
  "Directory with a shop's data: products.csv and inventory.csv for goods,"
  " or deals.csv, options.csv and codes.csv for vouchers",
)

# Deal columns that aren't text, and how to read each from its CSV cell.
DEAL_NUMBERS = {
  "rating": float,
  "reviews": int,
  "bought": int,
  "refund_days": int,
  "voucher_valid_days": int,
  "limit_per_person": int,
  "repurchase_days": int,
  "slot_minutes": int,
  "slot_capacity": int,
  "booking_days_ahead": int,
  "appointment_required": lambda cell: cell.lower() == "true",
  # One cell, items separated by '|'.
  "highlights": lambda cell: [item.strip() for item in cell.split("|")],
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def import_csv_data() -> None:
  """Read CSV files and populate the database."""
  data_dir = Path(FLAGS.data_dir)
  # Ensure tables exist
  await db.manager.init_dbs(FLAGS.products_db_path, FLAGS.transactions_db_path)

  try:
    # Import Products and Promotions to Products DB
    async with db.manager.products_session_factory() as session:
      logger.info("Clearing existing products...")
      await session.execute(delete(Product))

      logger.info("Importing Products from CSV...")
      products = []
      products_path = data_dir / "products.csv"
      if products_path.exists():
        with products_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            products.append(
              Product(
                id=row["id"],
                title=row["title"],
                price=int(row["price"]),
                image_url=row["image_url"],
              )
            )
      session.add_all(products)

      logger.info("Clearing existing deals and options...")
      await session.execute(delete(Deal))
      await session.execute(delete(DealOption))

      logger.info("Importing Deals from CSV...")
      deal_titles = {}
      deals_path = data_dir / "deals.csv"
      if deals_path.exists():
        with deals_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            # Empty cells (e.g. no refund deadline) are stored as NULL.
            deal = {k: v or None for k, v in row.items()}
            for column, cast in DEAL_NUMBERS.items():
              if deal.get(column) is not None:
                deal[column] = cast(deal[column])
            deal_titles[deal["id"]] = deal["title"]
            session.add(Deal(**deal))

      logger.info("Importing Deal Reviews from CSV...")
      await session.execute(delete(DealReview))
      reviews_path = data_dir / "reviews.csv"
      if reviews_path.exists():
        with reviews_path.open() as f:
          session.add_all(
            DealReview(
              deal_id=row["deal_id"],
              author=row["author"],
              rating=int(row["rating"]),
              date=row["date"],
              text=row["text"],
              reply=row["reply"] or None,
            )
            for row in csv.DictReader(f)
          )

      logger.info("Importing Deal Options from CSV...")
      options_path = data_dir / "options.csv"
      if options_path.exists():
        with options_path.open() as f:
          reader = csv.DictReader(f)
          for position, row in enumerate(reader):
            # Each option is the product a buyer puts in the cart.
            session.add(
              Product(
                id=row["id"],
                title=f"{deal_titles[row['deal_id']]} · {row['title']}",
                price=int(row["price"]),
              )
            )
            session.add(
              DealOption(
                product_id=row["id"],
                deal_id=row["deal_id"],
                title=row["title"],
                description=row.get("description") or None,
                # One cell, items separated by '|'.
                includes=[
                  item.strip()
                  for item in (row.get("includes") or "").split("|")
                  if item.strip()
                ],
                list_price=int(row["list_price"])
                if row.get("list_price")
                else None,
                position=position,
              )
            )

      logger.info("Clearing existing promotions...")
      await session.execute(delete(Promotion))

      logger.info("Importing Promotions from CSV...")
      promotions = []
      promotions_path = data_dir / "promotions.csv"
      if promotions_path.exists():
        with promotions_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            min_subtotal = (
              int(row["min_subtotal"]) if row.get("min_subtotal") else None
            )
            eligible_item_ids = (
              json.loads(row["eligible_item_ids"])
              if row.get("eligible_item_ids")
              else None
            )
            promotions.append(
              Promotion(
                id=row["id"],
                type=row["type"],
                min_subtotal=min_subtotal,
                eligible_item_ids=eligible_item_ids,
                description=row["description"],
              )
            )
        session.add_all(promotions)

      await session.commit()

    # Import Inventory and Customers to Transactions DB
    async with db.manager.transactions_session_factory() as session:
      logger.info("Clearing existing inventory...")
      await session.execute(delete(Inventory))

      logger.info("Importing Inventory from CSV...")
      inventory = []
      inventory_path = data_dir / "inventory.csv"
      if inventory_path.exists():
        with inventory_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            inventory.append(
              Inventory(
                product_id=row["product_id"], quantity=int(row["quantity"])
              )
            )
      session.add_all(inventory)

      logger.info("Clearing existing accounts...")
      await session.execute(delete(UserSession))
      await session.execute(delete(User))

      logger.info("Importing Accounts from CSV...")
      users_path = data_dir / "users.csv"
      if users_path.exists():
        with users_path.open() as f:
          for row in csv.DictReader(f):
            # Seed accounts for the demo; only the hash is stored.
            await account_service.register(
              session,
              row["full_name"],
              row["email"],
              # On a shared server, the demo password comes from the
              # environment, not from the file in the repository.
              os.environ.get("DEMO_PASSWORD") or row["password"],
            )

      logger.info("Clearing existing coin wallets...")
      await session.execute(delete(CoinEntry))

      logger.info("Importing Coin Wallets from CSV...")
      wallets_path = data_dir / "wallets.csv"
      if wallets_path.exists():
        with wallets_path.open() as f:
          for row in csv.DictReader(f):
            await db.add_coins(
              session, row["email"], int(row["coins"]), "granted"
            )

      logger.info("Clearing existing voucher codes...")
      await session.execute(delete(VoucherCode))

      logger.info("Importing Voucher Codes from CSV...")
      codes_path = data_dir / "codes.csv"
      if codes_path.exists():
        with codes_path.open() as f:
          reader = csv.DictReader(f)
          # An option's pool of codes is its inventory.
          session.add_all(
            VoucherCode(code=row["code"], product_id=row["option_id"])
            for row in reader
          )

      logger.info("Clearing existing customers and addresses...")
      await session.execute(delete(CustomerAddress))
      await session.execute(delete(Customer))

      logger.info("Importing Customers from CSV...")
      customers = []
      customers_path = data_dir / "customers.csv"
      if customers_path.exists():
        with customers_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            customers.append(
              Customer(
                id=row["id"],
                name=row["name"],
                email=row["email"],
              )
            )
        session.add_all(customers)

      logger.info("Importing Customer Addresses from CSV...")
      addresses = []
      addresses_path = data_dir / "addresses.csv"
      if addresses_path.exists():
        with addresses_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            addresses.append(
              CustomerAddress(
                id=row["id"],
                customer_id=row["customer_id"],
                street_address=row["street_address"],
                city=row["city"],
                state=row["state"],
                postal_code=row["postal_code"],
                country=row["country"],
              )
            )
        session.add_all(addresses)

      await session.commit()

      logger.info("Clearing existing payment instruments...")
      await session.execute(delete(PaymentInstrument))

      logger.info("Importing Payment Instruments from CSV...")
      instruments = []
      pi_path = data_dir / "payment_instruments.csv"
      if pi_path.exists():
        with pi_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            instruments.append(
              PaymentInstrument(
                id=row["id"],
                type=row["type"],
                brand=row["brand"],
                last_digits=row["last_digits"],
                token=row["token"],
                handler_id=row["handler_id"],
              )
            )
      session.add_all(instruments)
      await session.commit()

      logger.info("Clearing existing discounts...")
      await session.execute(delete(Discount))

      logger.info("Importing Discounts from CSV...")
      discounts = []
      discounts_path = data_dir / "discounts.csv"
      if discounts_path.exists():
        with discounts_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            discounts.append(
              Discount(
                code=row["code"],
                type=row["type"],
                value=int(row["value"]),
                description=row["description"],
              )
            )
        session.add_all(discounts)
        await session.commit()

      logger.info("Clearing existing shipping rates...")
      await session.execute(delete(ShippingRate))

      logger.info("Importing Shipping Rates from CSV...")
      rates = []
      shipping_path = data_dir / "shipping_rates.csv"
      if shipping_path.exists():
        with shipping_path.open() as f:
          reader = csv.DictReader(f)
          for row in reader:
            rates.append(
              ShippingRate(
                id=row["id"],
                country_code=row["country_code"],
                service_level=row["service_level"],
                price=int(row["price"]),
                title=row["title"],
              )
            )
        session.add_all(rates)
        await session.commit()

    logger.info("Database populated from CSVs.")
  finally:
    await db.manager.close()


def main(argv) -> None:
  """Run the CSV import script."""
  del argv
  asyncio.run(import_csv_data())


if __name__ == "__main__":
  absl_app.run(main)
