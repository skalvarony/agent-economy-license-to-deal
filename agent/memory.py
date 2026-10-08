"""What the agent knows about its person, and the rules it must keep.

The memory lives in the agent's run directory, next to its key: it is the
person's, not the model's. Three parts:

- `profile`: a few facts the person gives (city, how many people they usually
  buy for, a free note).
- `preferences`: short facts the person said or the agent noticed ("prefers
  refundable deals", "no beer"). The brain can add to them with `remember`.
- `rules`: hard limits the person sets on the page. The brain can read them
  but never change them, and `Session.propose_purchase` enforces them in
  code: a proposal that breaks a rule is refused before a checkout is opened,
  whatever the brain thinks.
"""

import dataclasses
import json
import pathlib
from typing import Any

MAX_PREFERENCES = 40

# How the person likes a proposal shown. The shop's data, the person's layout.
DEFAULT_PRESENTATION: dict[str, Any] = {
  # How many to show: the best one, or the three best to pick from.
  "shortlist": 1,
  # What comes first: "photo", "price" or "terms".
  "lead": "photo",
  # "brief" (title, price, button) or "full" (gallery, highlights, reviews).
  "detail": "full",
  # The language the agent writes in: "en" or "es".
  "language": "en",
}

DEFAULT_RULES: dict[str, Any] = {
  # Never propose a purchase above this, in cents. None: no limit.
  "max_total": None,
  # Only deals the person can get their money back on.
  "refundable_only": False,
  # Categories the agent must never propose.
  "avoid_categories": [],
}


@dataclasses.dataclass
class Memory:
  """The person's memory, read from and written to one JSON file."""

  path: pathlib.Path | None
  profile: dict[str, Any] = dataclasses.field(default_factory=dict)
  preferences: list[str] = dataclasses.field(default_factory=list)
  rules: dict[str, Any] = dataclasses.field(
    default_factory=lambda: dict(DEFAULT_RULES)
  )
  presentation: dict[str, Any] = dataclasses.field(
    default_factory=lambda: dict(DEFAULT_PRESENTATION)
  )

  @classmethod
  def load(cls, path: pathlib.Path | None) -> "Memory":
    """Read the memory file; an absent file is an empty memory."""
    memory = cls(path)
    if path and path.is_file():
      data = json.loads(path.read_text())
      memory.profile = data.get("profile") or {}
      memory.preferences = list(data.get("preferences") or [])
      memory.rules = {**DEFAULT_RULES, **(data.get("rules") or {})}
      memory.presentation = {
        **DEFAULT_PRESENTATION,
        **(data.get("presentation") or {}),
      }
    return memory

  def save(self) -> None:
    """Write the memory file."""
    if not self.path:
      return
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.path.write_text(
      json.dumps(self.as_dict(), ensure_ascii=False, indent=1)
    )

  def as_dict(self) -> dict[str, Any]:
    """Return the memory as the page and the brain see it."""
    return {
      "profile": self.profile,
      "preferences": self.preferences,
      "rules": self.rules,
      "presentation": self.presentation,
    }

  def remember(self, fact: str) -> bool:
    """Keep a short fact about the person. Returns False if nothing new."""
    fact = " ".join(fact.split())[:160]
    if not fact or fact.lower() in (p.lower() for p in self.preferences):
      return False
    self.preferences.append(fact)
    del self.preferences[:-MAX_PREFERENCES]
    self.save()
    return True

  def forget(self, fact: str) -> bool:
    """Drop a fact. Returns False if it wasn't there."""
    before = len(self.preferences)
    self.preferences = [p for p in self.preferences if p != fact]
    if len(self.preferences) != before:
      self.save()
      return True
    return False

  def update(
    self,
    profile: dict[str, Any] | None,
    rules: dict[str, Any] | None,
    presentation: dict[str, Any] | None = None,
  ) -> None:
    """Apply what the person set on the page. Rules are the person's only."""
    if presentation is not None:
      shown = dict(DEFAULT_PRESENTATION)
      shown["shortlist"] = 3 if str(presentation.get("shortlist")) == "3" else 1
      for key, allowed in (
        ("lead", ("photo", "price", "terms")),
        ("detail", ("brief", "full")),
        ("language", ("en", "es")),
      ):
        if presentation.get(key) in allowed:
          shown[key] = presentation[key]
      self.presentation = shown
    if profile is not None:
      self.profile = {
        key: str(value).strip()
        for key, value in profile.items()
        if key in ("city", "party", "note") and str(value).strip()
      }
    if rules is not None:
      cleaned = dict(DEFAULT_RULES)
      if rules.get("max_total") not in (None, "", 0, "0"):
        cleaned["max_total"] = max(0, int(rules["max_total"]))
      cleaned["refundable_only"] = bool(rules.get("refundable_only"))
      cleaned["avoid_categories"] = sorted(
        {
          str(c).strip().lower()
          for c in rules.get("avoid_categories") or []
          if str(c).strip()
        }
      )
      self.rules = cleaned
    self.save()

  def breaks(self, deal: dict[str, Any], option: dict[str, Any]) -> str | None:
    """Say which rule a deal's option would break, or None if it keeps them."""
    limit = self.rules.get("max_total")
    if limit is not None and option.get("price", 0) > limit:
      return f"your rule: never above ${limit / 100:g}"
    if (
      self.rules.get("refundable_only")
      and deal.get("refundability") != "refundable"
    ):
      return "your rule: refundable deals only"
    if (deal.get("category") or "").lower() in self.rules.get(
      "avoid_categories", []
    ):
      return f"your rule: no {deal.get('category')} deals"
    return None

  def brief(self, history: list[dict[str, Any]]) -> str:
    """Put the memory into words for a brain, with the recent history."""
    lines = []
    profile = self.profile
    if profile:
      lines.append(
        "About the customer: "
        + "; ".join(
          f"{label}: {profile[key]}"
          for key, label in (
            ("city", "city"),
            ("party", "usually buys for"),
            ("note", "note"),
          )
          if profile.get(key)
        )
        + "."
      )
    if self.preferences:
      lines.append("Preferences: " + "; ".join(self.preferences) + ".")
    rules = []
    if self.rules.get("max_total") is not None:
      rules.append(
        f"never propose anything above ${self.rules['max_total'] / 100:g}"
      )
    if self.rules.get("refundable_only"):
      rules.append("refundable deals only")
    if self.rules.get("avoid_categories"):
      rules.append(
        "never these categories: " + ", ".join(self.rules["avoid_categories"])
      )
    if rules:
      lines.append(
        "Hard rules the customer set (enforced by the app, not up to you): "
        + "; ".join(rules)
        + "."
      )
    if history:
      lines.append("Recent history: " + "; ".join(history[-6:]) + ".")
    shown = self.presentation
    lines.append(
      "How to present: "
      + (
        "the three best to pick from"
        if shown.get("shortlist") == 3
        else "the best one"
      )
      + f", {shown.get('lead', 'photo')} first,"
      + f" {shown.get('detail', 'full')} detail."
      + (
        " Write to the customer in Spanish."
        if shown.get("language") == "es"
        else ""
      )
    )
    return "\n".join(lines)
