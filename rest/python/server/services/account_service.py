"""Accounts: registering, signing in and staying signed in.

Each shop keeps its own accounts. A password is never stored: only a salted
scrypt hash of it. Signing in creates a session with a random token, which is
all the browser's cookie holds; signing out deletes the session.

An order belongs to the account whose email is the order's buyer, whether the
person bought it on the web or an agent bought it for them. The match is by
email alone, and the shop doesn't verify that an account's email is its
owner's.
"""

import datetime
import hashlib
import hmac
import re
import secrets
import uuid

import db
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 8
# scrypt cost: about 50 ms and 16 MB per hash.
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class AccountError(Exception):
  """Raised when a sign-up or sign-in can't go ahead; safe to show."""


def _now() -> datetime.datetime:
  return datetime.datetime.now(datetime.timezone.utc)


def hash_password(password: str) -> str:
  """Return a salted hash of a password, as `salt$hash` in hex."""
  salt = secrets.token_bytes(16)
  digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
  return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
  """Check a password against a stored hash, in constant time."""
  salt, _, expected = stored.partition("$")
  digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT)
  return hmac.compare_digest(digest.hex(), expected)


# Checked when the email is unknown, so that takes as long as a wrong password.
_NO_ACCOUNT_HASH = hash_password(secrets.token_hex(16))


async def register(
  session: AsyncSession, full_name: str, email: str, password: str
) -> db.User:
  """Create an account. Raises AccountError if the details can't be used."""
  full_name, email = full_name.strip(), email.strip().lower()
  if not full_name:
    raise AccountError("Enter your name.")
  if not _EMAIL.fullmatch(email):
    raise AccountError("Enter a valid email address.")
  if len(password) < MIN_PASSWORD_LENGTH:
    raise AccountError(
      f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters."
    )
  if await db.get_user_by_email(session, email):
    raise AccountError("There is already an account with that email.")

  user = db.User(
    id=str(uuid.uuid4()),
    email=email,
    full_name=full_name,
    password_hash=hash_password(password),
    created_at=_now().isoformat(),
  )
  session.add(user)
  # What an agent bought for this email before the account existed.
  for order_id in await db.list_order_ids_bought_by(session, email):
    if not await db.get_order_owner(session, order_id):
      await db.set_order_owner(session, order_id, user.id)
  await session.commit()
  return user


async def set_password(
  session: AsyncSession, email: str, password: str
) -> db.User:
  """Give an account a new password. Raises AccountError if it can't."""
  user = await db.get_user_by_email(session, email.strip().lower())
  if not user:
    raise AccountError("There is no account with that email.")
  if len(password) < MIN_PASSWORD_LENGTH:
    raise AccountError(
      f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters."
    )
  user.password_hash = hash_password(password)
  await session.commit()
  return user


async def link_order(
  session: AsyncSession, order_id: str, buyer_email: str | None
) -> None:
  """Give a new order to the account with the buyer's email, if there is one."""
  user = await db.get_user_by_email(session, buyer_email or "")
  if user:
    await db.set_order_owner(session, order_id, user.id)


async def authenticate(
  session: AsyncSession, email: str, password: str
) -> db.User | None:
  """Return the account for an email and password, or None if they're wrong."""
  user = await db.get_user_by_email(session, email)
  stored = user.password_hash if user else _NO_ACCOUNT_HASH
  return user if verify_password(password, stored) and user else None


async def start_session(session: AsyncSession, user: db.User) -> str:
  """Sign an account in and return the token for the browser's cookie."""
  token = secrets.token_urlsafe(32)
  expires = _now() + datetime.timedelta(days=SESSION_DAYS)
  session.add(
    db.UserSession(token=token, user_id=user.id, expires_at=expires.isoformat())
  )
  await session.commit()
  return token


async def user_for_token(
  session: AsyncSession, token: str | None
) -> db.User | None:
  """Return the account a session token belongs to, if it is still valid."""
  if not token:
    return None
  signed_in = await session.get(db.UserSession, token)
  if not signed_in or signed_in.expires_at < _now().isoformat():
    return None
  return await db.get_user(session, signed_in.user_id)


async def end_session(session: AsyncSession, token: str | None) -> None:
  """Sign a browser out."""
  if token:
    await session.execute(
      delete(db.UserSession).where(db.UserSession.token == token)
    )
    await session.commit()
