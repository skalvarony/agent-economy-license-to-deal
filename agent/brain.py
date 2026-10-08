"""The agent's brain: what decides the next step of a conversation.

A brain reads the conversation so far and answers with a step: words for the
person, calls to the agent's tools, or both. The session (session.py) runs the
calls and hands their results back, until the brain answers with words alone.

The conversation is a list of turns, oldest first:

  {"role": "person", "text": ...}                 what the person wrote
  {"role": "agent", "text": ..., "calls": [...]}  a step the brain took
  {"role": "tool", "call_id", "name", "result"}   what a call returned
  {"role": "event", "text": ...}                  what happened outside the
                                                  brain: an approval, a
                                                  purchase, a refusal
  {"role": "memory", "text", "memory"}            what the agent knows about
                                                  the person; always first

`ScriptedBrain` is a stand-in that follows fixed rules, so the whole purchase
can be used and tested with no model. A brain backed by a language model
implements the same `step` with the same tools (model_brain.py), and nothing
else changes.

No brain can pay. Its strongest tool is `propose_purchase`, which shows the
person a checkout to approve.
"""

from collections.abc import Awaitable, Callable
import dataclasses
import datetime
import re
from typing import Any, Protocol
import uuid

from texts import say


@dataclasses.dataclass
class Tool:
  """Something the brain may ask the session to do."""

  name: str
  description: str
  # JSON Schema of the arguments, in the shape language models take tools in.
  parameters: dict[str, Any]
  run: Callable[..., Awaitable[Any]]


@dataclasses.dataclass
class Call:
  """One request to run a tool."""

  name: str
  args: dict[str, Any]
  id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclasses.dataclass
class Step:
  """What the brain does next: say something, call tools, or both."""

  text: str = ""
  calls: list[Call] = dataclasses.field(default_factory=list)


class BrainError(Exception):
  """The brain couldn't decide: its model is down, or refused the request."""


class Brain(Protocol):
  """Anything that can decide the agent's next step."""

  # Shown to the person, so they know what is deciding.
  name: str
  # Whether a language model is behind it.
  is_model: bool

  async def step(self, turns: list[dict[str, Any]], tools: list[Tool]) -> Step:
    """Return the next step, given the conversation and the tools on offer."""
    ...


def money(cents: int) -> str:
  """Format an amount in cents: $99, or $49.10."""
  sign = "-" if cents < 0 else ""
  dollars, rest = divmod(abs(cents), 100)
  return f"{sign}${dollars}" + (f".{rest:02d}" if rest else "")


def moment(iso: str) -> str:
  """Format an ISO date and time: Fri 9 Oct, 10:00."""
  at = datetime.datetime.fromisoformat(iso)
  return f"{at:%a} {at.day} {at:%b}, {at:%H:%M}"


# The words that give away each of the shops' categories.
_CATEGORY_WORDS = {
  "wellness": {
    "spa",
    "massage",
    "sauna",
    "wellness",
    "relax",
    "relaxing",
    "float",
    "steam",
  },
  "food": {
    "dinner",
    "food",
    "beer",
    "tasting",
    "eat",
    "lunch",
    "restaurant",
    "cruise",
    "drink",
  },
  "activities": {
    "escape",
    "class",
    "baking",
    "activity",
    "activities",
    "fun",
    "game",
  },
}
_BUDGET = [
  r"(?:under|below|max(?:imum)?|up to|at most|less than|no more than|within"
  r"|budget(?: of)?|<)\s*\$?\s*(\d+)",
  r"\$\s*(\d+)",
  r"(\d+)\s*(?:dollars|usd)",
]
_WEEKDAYS = {
  "monday": 0, "mon": 0, "lunes": 0,
  "tuesday": 1, "tue": 1, "tues": 1, "martes": 1,
  "wednesday": 2, "wed": 2, "miércoles": 2, "miercoles": 2,
  "thursday": 3, "thu": 3, "thurs": 3, "jueves": 3,
  "friday": 4, "fri": 4, "viernes": 4,
  "saturday": 5, "sat": 5, "sábado": 5, "sabado": 5,
  "sunday": 6, "sun": 6, "domingo": 6,
}
_MONTHS = {
  "jan": 1, "ene": 1, "feb": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5,
  "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "oct": 10, "nov": 11,
  "dec": 12, "dic": 12,
}
_MONTH_WORD = r"(jan|ene|feb|mar|apr|abr|may|jun|jul|aug|ago|sep|oct|nov|dec|dic)[a-z]*"
_DAY_PATTERNS = [
  r"\b(\d{4})-(\d{2})-(\d{2})\b",
  r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+|de\s+)?" + _MONTH_WORD + r"\b",
  r"\b" + _MONTH_WORD + r"\s+(\d{1,2})\b",
]
_TIME_PATTERNS = [
  r"\b(?:at|a las|a la)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|h)?\b",
  r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b",
  r"\b(\d{1,2})\s*(am|pm)\b",
  r"\b(\d{1,2})\s*(h)\b",
]
_REFUNDABLE = r"refund|cancel|change my mind"
_NO_COINS = r"(?:no|without|don'?t use|do not use|keep(?: my)?) coins"
# Words of a request that say nothing about which deal is wanted.
_FILLER = frozenset(
  re.findall(
    r"\w+",
    "a an the for under below with and or to me my i we us our want find buy"
    " book get something anything please refundable refund in of on at this"
    " that up max most less than more usd dollars coins coin use can could you"
    " need looking look like would some one is it be deal deals prague cheap"
    " cheapest best good nice no not without keep free cancellation cancel"
    " budget within if able today tomorrow hoy mañana next this coming at"
    " am pm h oclock o'clock time hour hours hora horas las el de por la"
    " monday tuesday wednesday thursday friday saturday sunday mon tue wed thu"
    " fri sat sun lunes martes miercoles jueves viernes sabado domingo jan"
    " feb mar apr may jun jul aug sep oct nov dec january february march"
    " april june july august september october november december enero"
    " febrero marzo abril mayo junio julio agosto septiembre octubre"
    " noviembre diciembre",
  )
)


def _when(
  lowered: str, now: datetime.datetime
) -> tuple[datetime.datetime | None, bool]:
  """Read a day and an hour from a request, as local wall-clock time.

  Returns the moment and whether an hour was given. A day alone is the day
  at 00:00; an hour alone is today, or tomorrow if that hour has passed.
  """
  day = None
  if re.search(r"\b(today|hoy)\b", lowered):
    day = now.date()
  elif re.search(r"\b(tomorrow|mañana)\b", lowered):
    day = now.date() + datetime.timedelta(days=1)
  elif found := re.search(_DAY_PATTERNS[0], lowered):
    day = datetime.date(*map(int, found.groups()))
  elif found := re.search(_DAY_PATTERNS[1], lowered):
    day = _day_of(now, int(found.group(1)), _MONTHS[found.group(2)])
  elif found := re.search(_DAY_PATTERNS[2], lowered):
    day = _day_of(now, int(found.group(2)), _MONTHS[found.group(1)])
  else:
    for word, weekday in _WEEKDAYS.items():
      if re.search(rf"\b{word}\b", lowered):
        day = now.date() + datetime.timedelta(
          days=(weekday - now.weekday()) % 7
        )
        break
  hour = None
  for pattern in _TIME_PATTERNS:
    if found := re.search(pattern, lowered):
      groups = found.groups()
      h = int(groups[0])
      minute = int(groups[1]) if len(groups) > 2 and groups[1] else 0
      suffix = groups[-1] or ""
      if suffix == "pm" and h < 12:
        h += 12
      if suffix == "am" and h == 12:
        h = 0
      if 0 <= h < 24 and 0 <= minute < 60:
        hour = datetime.time(h, minute)
        break
  if day is None and hour is None:
    return None, False
  if day is None:
    day = now.date()
    if datetime.datetime.combine(day, hour) <= now:
      day += datetime.timedelta(days=1)
  return datetime.datetime.combine(day, hour or datetime.time(0, 0)), (
    hour is not None
  )


def _day_of(now: datetime.datetime, day: int, month: int) -> datetime.date:
  """The next occurrence of a day and month, this year or the next."""
  try:
    date = datetime.date(now.year, month, day)
  except ValueError:
    return now.date()
  if date < now.date():
    date = datetime.date(now.year + 1, month, day)
  return date


@dataclasses.dataclass
class Ask:
  """What a person's request asks for, as far as the script understands it."""

  category: str | None
  budget: int | None  # in cents
  refundable: bool
  use_coins: bool
  words: set[str]
  # The day and hour asked for, as local wall-clock time, if any.
  when: datetime.datetime | None = None
  hour_given: bool = False

  @property
  def understood(self) -> bool:
    """Whether there is anything to search for."""
    return bool(self.category or self.words)


def _words(text: str) -> set[str]:
  return set(re.findall(r"[a-z]+", text.lower()))


def parse(text: str, now: datetime.datetime | None = None) -> Ask:
  """Read a request such as "a spa day for two, refundable, under $120".

  `now` fixes what "Saturday" or "tomorrow" mean (the tests set it).
  """
  lowered = text.lower()
  now = now or datetime.datetime.now()
  when, hour_given = _when(lowered, now)
  words = _words(lowered)
  category = next(
    (name for name, tells in _CATEGORY_WORDS.items() if words & tells), None
  )
  budget = None
  for pattern in _BUDGET:
    if found := re.search(pattern, lowered):
      budget = int(found.group(1)) * 100
      break
  return Ask(
    category=category,
    budget=budget,
    refundable=bool(re.search(_REFUNDABLE, lowered)),
    use_coins=not re.search(_NO_COINS, lowered),
    words=words - _FILLER - set(re.findall(r"\d+", lowered)),
    when=when,
    hour_given=hour_given,
  )


def openings(listed: dict[str, Any], days: int = 3, per_day: int = 4) -> str:
  """Say the free slots of an availability answer, a few per day."""
  parts = []
  for day in listed.get("days") or []:
    free = [s for s in day["slots"] if s.get("left", 1) > 0]
    if not free:
      continue
    first = datetime.datetime.fromisoformat(free[0]["starts_at"])
    hours = ", ".join(
      f"{datetime.datetime.fromisoformat(s['starts_at']):%H:%M}"
      for s in free[:per_day]
    )
    more = f" and {len(free) - per_day} more" if len(free) > per_day else ""
    parts.append(f"{first:%a %-d %b}: {hours}{more}")
    if len(parts) == days:
      break
  return "; ".join(parts)


def slot_at(listed: dict[str, Any], when: datetime.datetime) -> dict | None:
  """Return the free slot that starts at a local wall-clock time."""
  for day in listed.get("days") or []:
    for slot in day["slots"]:
      starts = datetime.datetime.fromisoformat(slot["starts_at"]).replace(
        tzinfo=None
      )
      if starts == when and slot.get("left", 1) > 0:
        return slot
  return None


@dataclasses.dataclass
class Choice:
  """The script's pick, and everything it weighed to get there."""

  deal: dict[str, Any] | None
  option: dict[str, Any] | None
  reason: str
  considered: list[dict[str, Any]]


def _row(deal, option, verdict: str, reason: str) -> dict[str, Any]:
  title = deal["title"]
  if option and option.get("label"):
    title += f" · {option['label']}"
  return {
    "shop": deal["shop"],
    "title": title,
    "price": option["price"] if option else None,
    "option_id": option["id"] if option else None,
    "verdict": verdict,
    "reason": reason,
  }


def _refund_text(deal: dict[str, Any]) -> str:
  if deal.get("refundable_until"):
    return f"refundable until {moment(deal['refundable_until'])}"
  if deal.get("refund_days"):
    return f"refundable for {deal['refund_days']} days"
  return "refundable"


# How many other deals a proposal names besides the chosen one.
MAX_ALTERNATIVES = 2
MAX_REJECTED = 3


def _match(ask: Ask, deal: dict[str, Any]) -> int:
  """Count the words of the request the deal mentions."""
  text = f"{deal['title']} {deal['description']} {deal['merchant']}"
  return len(ask.words & _words(text))


def _sentence(text: str) -> str:
  return text[0].upper() + text[1:] + "."


def choose(
  ask: Ask,
  found: dict[str, Any],
  wallets: list[dict],
  avoid: set[str] | None = None,
) -> Choice:
  """Pick the deal and option that fit the request best, and say why."""
  avoid = avoid or set()
  fits = []
  rejected = []
  for deal in found["deals"]:
    match = _match(ask, deal)
    on_sale = [o for o in deal["options"] if o["available"]]
    cheapest = min(deal["options"], key=lambda o: o["price"])
    if (deal.get("category") or "").lower() in avoid:
      rejected.append(
        (
          match,
          _row(deal, cheapest, "rejected", "Your rule: no " + deal["category"]),
        )
      )
      continue
    if not on_sale:
      rejected.append((match, _row(deal, cheapest, "rejected", "Sold out")))
      continue
    if ask.refundable and deal["refundability"] != "refundable":
      partly = deal["refundability"] == "partially_refundable"
      why = "Only partly refundable" if partly else "Non-refundable"
      rejected.append((match, _row(deal, cheapest, "rejected", why)))
      continue
    within = [
      o for o in on_sale if ask.budget is None or o["price"] <= ask.budget
    ]
    if not within:
      why = f"Over your {money(ask.budget)}"
      rejected.append((match, _row(deal, cheapest, "rejected", why)))
      continue
    # With a budget, the fullest option it allows; without one, the cheapest.
    pick = max if ask.budget is not None else min
    option = pick(within, key=lambda o: o["price"])
    rating = (deal.get("rating") or {}).get("value") or 0
    fits.append((match, rating, -option["price"], deal, option, len(within)))

  # Of the deals turned down, name the ones closest to the request.
  rejected.sort(key=lambda entry: -entry[0])
  turned_down = [row for _, row in rejected[:MAX_REJECTED]]
  for shop in found["searched"]:
    why = None
    if shop.get("error"):
      # The shop's own words: it was down, or it turned the agent away.
      why = shop["error"]
    elif not shop["found"]:
      why = f"No {ask.category} deals" if ask.category else "Nothing matching"
    if why:
      turned_down.append(
        {
          "shop": shop["shop"],
          "title": "",
          "price": None,
          "verdict": "rejected",
          "reason": why,
        }
      )

  if not fits:
    return Choice(None, None, "", turned_down)

  fits.sort(key=lambda fit: fit[:3], reverse=True)
  match, _, _, deal, option, choices = fits[0]
  reasons = ["the closest match to what you asked for" if match else "it fits"]
  if choices > 1 and ask.budget is not None:
    reasons.append(f"the fullest option within {money(ask.budget)}")
  if deal["refundability"] == "refundable":
    reasons.append(_refund_text(deal))
  savings = []
  if deal.get("promo"):
    savings.append(f"code {deal['promo']['code']} lowers the price")
  balance = _balance(wallets, deal["shop"])
  if ask.use_coins and balance:
    savings.append(f"your {balance} coins there pay part of it")
  if savings:
    reasons.append(" and ".join(savings))
  considered = [_row(deal, option, "chosen", "")]
  for _, rating, _, other, other_option, _ in fits[1 : 1 + MAX_ALTERNATIVES]:
    why = f"Also fits; rated {rating}" if rating else "Also fits"
    considered.append(_row(other, other_option, "alternative", why))
  return Choice(
    deal, option, " ".join(map(_sentence, reasons)), considered + turned_down
  )


def _balance(wallets: list[dict], shop: str) -> int:
  return next((w["balance"] for w in wallets if w["shop"] == shop), 0)


class ScriptedBrain:
  """A brain without a model: it follows the same steps for every request.

  Search every shop and read the wallets, pick by fixed rules, propose the
  purchase, and tell the person. It reads what it needs from the turns, as a
  model would, and keeps nothing between steps.
  """

  name = "Scripted stand-in"
  is_model = False

  def __init__(self, now: Callable[[], datetime.datetime] | None = None):
    """`now` is what the clock says; the tests fix it."""
    self.now = now or datetime.datetime.now

  async def step(self, turns: list[dict[str, Any]], tools: list[Tool]) -> Step:
    """Return the next step of the fixed routine."""
    del tools  # The script knows the tools by name.
    now = self.now()
    asked = max(i for i, turn in enumerate(turns) if turn["role"] == "person")
    ask = parse(turns[asked]["text"], now)
    if not ask.understood and ask.when is not None:
      # "Saturday at 11": the answer to the question of when. The request
      # it answers is the person's words before it.
      earlier = [
        i for i, turn in enumerate(turns[:asked]) if turn["role"] == "person"
      ]
      if earlier:
        ask, answer = parse(turns[earlier[-1]]["text"], now), ask
        ask.when, ask.hour_given = answer.when, answer.hour_given
    # The person's rules fill in what the request didn't say.
    memory = next((t["memory"] for t in turns if t["role"] == "memory"), {})
    rules = memory.get("rules", {})
    lang = (memory.get("presentation") or {}).get("language", "en")
    if rules.get("max_total"):
      # A stated budget never goes above the person's rule.
      ask.budget = min(ask.budget or rules["max_total"], rules["max_total"])
    if rules.get("refundable_only"):
      ask.refundable = True
    if not ask.understood:
      return Step(text=say("help", lang))
    results = {
      turn["name"]: turn["result"]
      for turn in turns[asked + 1 :]
      if turn["role"] == "tool"
    }

    if "search_deals" not in results:
      search: dict[str, Any] = {}
      if ask.category:
        search["category"] = ask.category
      else:
        search["query"] = " ".join(sorted(ask.words))
      # No price filter: a deal over the budget is worth naming as rejected.
      return Step(
        calls=[Call("search_deals", search), Call("read_wallets", {})]
      )

    found, wallets = results["search_deals"], results["read_wallets"]
    if "propose_purchase" not in results:
      choice = choose(
        ask, found, wallets, set(rules.get("avoid_categories") or [])
      )
      if not choice.deal:
        # No shop sells anything like it: likely not a request to buy.
        if not found["deals"] and not ask.category:
          return Step(text=say("nothing_like_it", lang) + say("help", lang))
        return Step(text=self._nothing(found, choice, lang))
      deal, option = choice.deal, choice.option
      starts_at = None
      if (deal.get("booking") or {}).get("required"):
        # Booked for a date and time when bought: the person has to name
        # them, and the shop has to have the slot free.
        if "check_availability" not in results:
          call = {"shop": deal["shop"], "deal_id": deal["deal_id"]}
          if ask.when is not None:
            call["from_day"] = ask.when.date().isoformat()
            call["days"] = 1
          else:
            call["days"] = 7
          return Step(calls=[Call("check_availability", call)])
        listed = results["check_availability"]
        if listed.get("error"):
          return Step(
            text=say("shop_refused", lang, message=listed["message"])
          )
        free = openings(listed)
        if ask.when is None or not ask.hour_given:
          if not free:
            return Step(
              text=say(
                "no_openings", lang, title=deal["title"], shop=deal["shop_name"]
              )
            )
          return Step(
            text=say(
              "ask_when",
              lang,
              title=deal["title"],
              shop=deal["shop_name"],
              openings=free,
            )
          )
        slot = slot_at(listed, ask.when)
        if not slot:
          return Step(
            text=say(
              "no_such_slot",
              lang,
              title=deal["title"],
              day=f"{ask.when:%a %-d %b}",
              openings=free or "nothing free",
            )
          )
        starts_at = slot["starts_at"]
      balance = _balance(wallets, deal["shop"]) if ask.use_coins else 0
      proposal = {
        "shop": deal["shop"],
        "option_id": option["id"],
        "quantity": 1,
        # The shop caps the coins at what the wallet holds and the price allows.
        "coins": min(balance, option["price"] // 100),
        "reason": choice.reason,
        "considered": choice.considered,
      }
      if deal.get("promo"):
        proposal["promo_code"] = deal["promo"]["code"]
      if starts_at:
        proposal["starts_at"] = starts_at
      return Step(calls=[Call("propose_purchase", proposal)])

    proposed = results["propose_purchase"]
    if proposed.get("error") == "rule":
      return Step(text=say("rule", lang, message=proposed["message"]))
    if proposed.get("error"):
      return Step(text=say("shop_refused", lang, message=proposed["message"]))
    paying = say("card_pays", lang, total=money(proposed["total"]))
    if not proposed["total"]:
      paying = say("coins_cover", lang)
    booking = proposed.get("booking") or {}
    if booking.get("starts_at"):
      return Step(
        text=say(
          "proposal_booked",
          lang,
          title=proposed["title"],
          shop=proposed["shop_name"],
          when=moment(booking["starts_at"]),
          paying=paying,
        )
      )
    return Step(
      text=say(
        "proposal",
        lang,
        title=proposed["title"],
        shop=proposed["shop_name"],
        paying=paying,
      )
    )

  def _nothing(
    self, found: dict[str, Any], choice: Choice, lang: str = "en"
  ) -> str:
    shops = ", ".join(shop["name"] for shop in found["searched"])
    names = {shop["shop"]: shop["name"] for shop in found["searched"]}
    whys = [
      f"{row['title'] or names.get(row['shop'], row['shop'])}:"
      f" {row['reason'].lower()}"
      for row in choice.considered[:4]
    ]
    return say("nothing_fits", lang, shops=shops, whys="; ".join(whys))
