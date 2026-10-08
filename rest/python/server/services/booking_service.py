"""Bookings: a date and time for the service, chosen when buying.

A deal that is booked at purchase says on which days of the week it takes
customers (`slot_days`), between which hours (`slot_hours`), how far apart
its start times are (`slot_minutes`), how many units each start time takes
(`slot_capacity`) and how far ahead it can be booked (`booking_days_ahead`).
From that the shop lists its open slots (`GET /deals/{id}/availability`),
the buyer picks one, the checkout checks it against the calendar and the
order keeps it under `service.booking`.

The slot travels on the checkout as `bookings: {"<item id>": "<start>"}`,
one entry per deal line, so an agent sends it with the line items and the
web checkout sets it from the page. A slot's capacity is counted in units
bought; a refund or the merchant's cancellation gives them back. Deals
without a calendar need no booking. All times are ISO 8601 with an offset,
in the shop's timezone (`timezone` in shop.json, Europe/Prague by default).
"""

import datetime
from typing import Any
import zoneinfo

import config
import db
from exceptions import BookingRequiredError
from exceptions import InvalidRequestError
from exceptions import SlotUnavailableError
from sqlalchemy.ext.asyncio import AsyncSession

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_DAYS_AHEAD = 30
# A slot can be booked until this long before it starts.
NOTICE = datetime.timedelta(minutes=30)
# The most days one availability answer covers.
MAX_DAYS = 14


def timezone() -> datetime.tzinfo:
  """Return the shop's timezone: the one its slots are expressed in."""
  return zoneinfo.ZoneInfo(config.get_shop().get("timezone") or "Europe/Prague")


def required(deal: db.Deal) -> bool:
  """Whether the deal is booked for a date and time when bought."""
  return bool(deal.slot_hours and deal.slot_minutes)


def days_of(deal: db.Deal) -> list[str]:
  """Return the days of the week the deal takes bookings on."""
  if not deal.slot_days:
    return list(DAYS)
  return [d.strip().lower()[:3] for d in deal.slot_days.split(",") if d.strip()]


def ranges_of(deal: db.Deal) -> list[tuple[datetime.time, datetime.time]]:
  """Return the deal's hours as (first start, last start) pairs."""
  ranges = []
  for part in (deal.slot_hours or "").split(";"):
    first, last = (piece.strip() for piece in part.split("-"))
    ranges.append(
      (datetime.time.fromisoformat(first), datetime.time.fromisoformat(last))
    )
  return ranges


def summary(deal: db.Deal) -> dict[str, Any] | None:
  """Return the deal's calendar in the shape the terms carry it."""
  if not required(deal):
    return None
  return {
    "required": True,
    "days": days_of(deal),
    "hours": deal.slot_hours,
    "slot_minutes": deal.slot_minutes,
    "capacity_per_slot": deal.slot_capacity or 1,
    "days_ahead": deal.booking_days_ahead or DEFAULT_DAYS_AHEAD,
    "timezone": str(timezone()),
  }


def _aware(iso: str | None) -> datetime.datetime | None:
  if not iso:
    return None
  when = datetime.datetime.fromisoformat(iso)
  if when.tzinfo is None:
    when = when.replace(tzinfo=timezone())
  return when


def starts_on(deal: db.Deal, day: datetime.date) -> list[datetime.datetime]:
  """Return every start time the deal offers on a day, in the shop's zone."""
  if not required(deal) or DAYS[day.weekday()] not in days_of(deal):
    return []
  tz = timezone()
  step = datetime.timedelta(minutes=deal.slot_minutes)
  not_before = _aware(deal.service_not_before)
  not_after = _aware(deal.service_not_after)
  starts = []
  for first, last in ranges_of(deal):
    start = datetime.datetime.combine(day, first, tzinfo=tz)
    end = datetime.datetime.combine(day, last, tzinfo=tz)
    while start <= end:
      if (not_before is None or start >= not_before) and (
        not_after is None or start <= not_after
      ):
        starts.append(start)
      start += step
  return starts


def _now(now: datetime.datetime | None) -> datetime.datetime:
  return (now or datetime.datetime.now(timezone())).astimezone(timezone())


def horizon(
  deal: db.Deal, now: datetime.datetime | None = None
) -> tuple[datetime.date, datetime.date]:
  """Return the first and last day the deal can be booked for, today on."""
  today = _now(now).date()
  last = today + datetime.timedelta(
    days=(deal.booking_days_ahead or DEFAULT_DAYS_AHEAD) - 1
  )
  not_after = _aware(deal.service_not_after)
  if not_after:
    last = min(last, not_after.astimezone(timezone()).date())
  return today, last


async def availability(
  transactions_session: AsyncSession,
  deal: db.Deal,
  start: datetime.date | None = None,
  days: int = 7,
  now: datetime.datetime | None = None,
) -> dict[str, Any]:
  """List the deal's slots from a day on, with the places left in each."""
  now = _now(now)
  first, last = horizon(deal, now)
  begin = max(start or first, first)
  days = max(1, min(days, MAX_DAYS))
  end = min(begin + datetime.timedelta(days=days - 1), last)
  step = datetime.timedelta(minutes=deal.slot_minutes or 0)
  capacity = deal.slot_capacity or 1
  listed: list[tuple[datetime.date, list[datetime.datetime]]] = []
  day = begin
  while day <= end:
    starts = [s for s in starts_on(deal, day) if s >= now + NOTICE]
    listed.append((day, starts))
    day += datetime.timedelta(days=1)
  taken = await db.booked_units(
    transactions_session,
    deal.id,
    [s.isoformat() for _, starts in listed for s in starts],
  )
  return {
    "deal_id": deal.id,
    "timezone": str(timezone()),
    "slot_minutes": deal.slot_minutes,
    "capacity_per_slot": capacity,
    "from": begin.isoformat(),
    "to": end.isoformat(),
    "bookable_until": last.isoformat(),
    "days": [
      {
        "date": day.isoformat(),
        "slots": [
          {
            "starts_at": s.isoformat(),
            "ends_at": (s + step).isoformat(),
            "left": max(0, capacity - taken.get(s.isoformat(), 0)),
          }
          for s in starts
        ],
      }
      for day, starts in listed
    ],
  }


def parse_start(deal: db.Deal, text: Any) -> datetime.datetime:
  """Read a requested start time; without an offset it is the shop's."""
  try:
    when = datetime.datetime.fromisoformat(str(text))
  except (TypeError, ValueError) as e:
    raise InvalidRequestError(
      f"The booking for {deal.title} must be an ISO 8601 time, not {text!r}."
    ) from e
  if when.tzinfo is None:
    when = when.replace(tzinfo=timezone())
  return when.astimezone(timezone())


async def check(
  transactions_session: AsyncSession,
  deal: db.Deal,
  starts_at: Any,
  units: int,
  now: datetime.datetime | None = None,
) -> dict[str, Any]:
  """Return the slot at `starts_at` if the deal offers it and it has room."""
  now = _now(now)
  when = parse_start(deal, starts_at)
  first, last = horizon(deal, now)
  label = f"{when:%a %-d %b} at {when:%H:%M}"
  if not (first <= when.date() <= last) or when not in starts_on(
    deal, when.date()
  ):
    raise SlotUnavailableError(
      f"{deal.title} is not offered on {label}. Ask for its availability."
    )
  if when < now + NOTICE:
    raise SlotUnavailableError(
      f"The slot on {label} has started or starts too soon to book."
    )
  key = when.isoformat()
  taken = (await db.booked_units(transactions_session, deal.id, [key])).get(
    key, 0
  )
  capacity = deal.slot_capacity or 1
  if taken + units > capacity:
    left = capacity - taken
    raise SlotUnavailableError(
      f"{deal.title} is full on {label}: {left} of {capacity} places left."
    )
  step = datetime.timedelta(minutes=deal.slot_minutes)
  return {
    "starts_at": key,
    "ends_at": (when + step).isoformat(),
    "left": capacity - taken,
  }


def wanted(checkout: Any, item_id: str) -> str | None:
  """Return the start the checkout asks for a line, if any."""
  mapping = getattr(checkout, "bookings", None) or {}
  return mapping.get(item_id) if isinstance(mapping, dict) else None


async def attach(
  products_session: AsyncSession,
  transactions_session: AsyncSession,
  checkout: Any,
  now: datetime.datetime | None = None,
) -> None:
  """Write each line's booking on its terms, checked against the calendar.

  A deal booked at purchase always carries its calendar under
  `service.booking`; once the checkout names a start for its line, the slot
  is checked and added there. A start the shop does not offer, or a full
  one, is refused at once, so the buyer learns before paying.
  """
  for line in checkout.line_items:
    found = await db.get_option(products_session, line.item.id)
    if not found or not required(found[0]):
      continue
    deal = found[0]
    service = dict(getattr(line, "service", None) or {})
    booking = dict(service.get("booking") or summary(deal) or {})
    starts_at = wanted(checkout, line.item.id)
    if starts_at:
      slot = await check(transactions_session, deal, starts_at, line.quantity, now)
      booking.update(starts_at=slot["starts_at"], ends_at=slot["ends_at"])
    service["booking"] = booking
    line.service = service


async def ensure(
  products_session: AsyncSession,
  transactions_session: AsyncSession,
  checkout: Any,
  now: datetime.datetime | None = None,
) -> None:
  """Before anything is charged: every deal that needs a slot has one."""
  for line in checkout.line_items:
    found = await db.get_option(products_session, line.item.id)
    if not found or not required(found[0]):
      continue
    deal = found[0]
    starts_at = wanted(checkout, line.item.id)
    if not starts_at:
      raise BookingRequiredError(
        f"{deal.title} is booked for a date and time: choose a slot"
        f" (bookings[{line.item.id!r}]) before paying."
      )
    await check(transactions_session, deal, starts_at, line.quantity, now)


async def book(
  products_session: AsyncSession,
  transactions_session: AsyncSession,
  checkout: Any,
  order: dict[str, Any],
  buyer_email: str | None,
  now: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
  """Record the slots a placed order takes and write them on its lines."""
  booked = []
  for line in order["line_items"]:
    found = await db.get_option(products_session, line["item"]["id"])
    if not found or not required(found[0]):
      continue
    deal = found[0]
    starts_at = wanted(checkout, line["item"]["id"])
    if not starts_at:
      raise BookingRequiredError(
        f"{deal.title} is booked for a date and time: choose a slot first."
      )
    units = line["quantity"]["total"]
    slot = await check(transactions_session, deal, starts_at, units, now)
    await db.add_booking(
      transactions_session,
      order_id=order["id"],
      line_id=line["id"],
      deal_id=deal.id,
      product_id=line["item"]["id"],
      buyer_email=buyer_email,
      starts_at=slot["starts_at"],
      ends_at=slot["ends_at"],
      units=units,
    )
    line.setdefault("service", {})["booking"] = {
      **(summary(deal) or {}),
      "starts_at": slot["starts_at"],
      "ends_at": slot["ends_at"],
      "status": "booked",
    }
    booked.append(line["service"]["booking"])
  return booked


async def release(transactions_session: AsyncSession, order_id: str) -> None:
  """Give an order's slots back to the calendar."""
  await db.release_bookings(transactions_session, order_id)
