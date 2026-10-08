"""Append-only ledger of what happens in the shop.

Each event is one JSON line in the file given by --ledger_path. The demo's
shops all write to the same file, so one stream shows every purchase, refund
and merchant action in order.
"""

import datetime
import json
from pathlib import Path
from typing import Any

import config


def _path() -> Path | None:
  flags = config.FLAGS
  if not flags.is_parsed() or not flags.ledger_path:
    return None
  return Path(flags.ledger_path)


def emit(
  event: str,
  checkout_id: str = "",
  payment_id: str = "",
  detail: dict[str, Any] | None = None,
) -> None:
  """Append one event to the ledger. Does nothing when no ledger is set."""
  path = _path()
  if path is None:
    return
  line = {
    "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "seller": config.get_shop()["name"],
    "event": event,
    "rail": config.FLAGS.payment_rail,
    "checkout_id": checkout_id,
    "payment_id": payment_id,
    "detail": detail or {},
  }
  path.parent.mkdir(parents=True, exist_ok=True)
  # One short write in append mode, so shops sharing the file don't interleave.
  with path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(line) + "\n")


def read(after: int = 0) -> list[dict[str, Any]]:
  """Return the ledger's events, skipping the first `after` of them."""
  path = _path()
  if path is None or not path.exists():
    return []
  with path.open(encoding="utf-8") as f:
    return [json.loads(line) for line in f.readlines()[after:] if line.strip()]
