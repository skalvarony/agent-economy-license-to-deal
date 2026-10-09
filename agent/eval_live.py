#!/usr/bin/env python3
"""Drive a running agent through the demo's conversations and check them.

The unit tests prove the agent against fakes. This runs the real thing: an
agent (scripted or on a model) in front of real shops, the way a person
would use it, and checks what it proposed, what it bought and what it
changed. With a model behind the agent it is the flow simulator for its
instructions: run it after changing them, and before a demo.

  scripts/shops.sh start && scripts/agent.sh start
  cd agent && uv run python eval_live.py                # http://localhost:8190
  cd agent && uv run python eval_live.py http://localhost:9190

It needs an agent with a fresh thread (restart it with an empty run
directory), the three shops with free slots on the coming Saturday, and,
with a model, a key with credits. Stdlib only. Exits 1 when a step fails.
"""

import datetime
import json
import sys
import time
import urllib.error
import urllib.request

AGENT = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8190"
WAIT_SECONDS = 150


def call(method: str, path: str, body: dict | None = None) -> dict | list:
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request(
    AGENT + path,
    data=data,
    method=method,
    headers={"Content-Type": "application/json"},
  )
  with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read().decode("utf-8")
  try:
    return json.loads(raw)
  except ValueError:
    # The message and approval endpoints stream the turn's events, one JSON
    # object per line; the state is read separately, so the stream is enough.
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def state() -> dict:
  return call("GET", "/api/state")


def saturday() -> datetime.date:
  """The coming Saturday, the day the requests below name."""
  today = datetime.date.today()
  return today + datetime.timedelta(days=(5 - today.weekday()) % 7)


def wait_for_turn(seq_before: int) -> list[dict]:
  """Wait until the agent has spoken since `seq_before` and gone quiet.

  The agent's words may be followed by a wallet refresh or a note; the
  turn is over when nothing new has arrived for a moment after them.
  """
  spoken_at = None
  for _ in range(WAIT_SECONDS):
    time.sleep(1)
    events = [e for e in state()["events"] if e.get("seq", 0) > seq_before]
    if any(e["type"] == "agent" for e in events):
      last = events[-1].get("seq", 0)
      if spoken_at == last:
        return events
      spoken_at = last
  raise TimeoutError("the agent did not answer in time")


def last_seq() -> int:
  events = state()["events"]
  return events[-1].get("seq", 0) if events else 0


def say(text: str) -> list[dict]:
  before = last_seq()
  call("POST", "/api/messages", {"text": text})
  return wait_for_turn(before)


def pending() -> dict | None:
  waiting = call("GET", "/api/approvals")["pending"]
  return waiting[-1] if waiting else None


def approve(proposal_id: str) -> list[dict]:
  before = last_seq()
  call("POST", f"/api/proposals/{proposal_id}/approve")
  return wait_for_turn(before)


def decline(proposal_id: str) -> list[dict]:
  before = last_seq()
  call("POST", f"/api/proposals/{proposal_id}/decline")
  return wait_for_turn(before)


def said(events: list[dict]) -> str:
  return " ".join(e["text"] for e in events if e["type"] == "agent")


def proposals(events: list[dict]) -> list[dict]:
  return [e["proposal"] for e in events if e["type"] == "proposal"]


def shop_calls(events: list[dict]) -> list[str]:
  return [f"{e['method']} {e['path']}" for e in events if e["type"] == "activity"]


def purchases() -> list[dict]:
  rows = call("GET", "/api/purchases")
  return rows if isinstance(rows, list) else rows.get("purchases", rows)


failures = 0
usage_before = None


def usage() -> dict:
  return (state()["brain"].get("usage") or {}) if state()["brain"].get("is_model") else {}


def step(label: str, run) -> None:
  """Run one step, time it, say what the model cost, and check it."""
  global failures, usage_before
  before = usage()
  started = time.monotonic()
  try:
    ok, detail = run()
  except Exception as error:  # noqa: BLE001
    ok, detail = False, f"{type(error).__name__}: {error}"
  seconds = time.monotonic() - started
  after = usage()
  cost = ""
  if before and after:
    cost = (
      f"  model: {int(after['requests'] - before['requests'])} req,"
      f" {int(after['completion_tokens'] - before['completion_tokens'])} out tok,"
      f" ${after['cost'] - before['cost']:.4f}"
    )
  failures += not ok
  print(f"{'ok  ' if ok else 'FAIL'} {label}  [{seconds:.1f}s]{cost}")
  if not ok:
    print(f"       {detail}")


def main() -> int:
  sat = saturday()
  info = state()
  brain = info["brain"]
  print(f"agent {info['name']} · brain {brain['name']} · Saturday is {sat:%a %-d %b}")
  if info["events"]:
    print("note: the thread is not empty; a fresh agent gives the cleanest run")

  # 1. No time given: the agent must ask for one, not propose.
  def no_time():
    ev = say("A spa day for two, refundable, under $120")
    words = said(ev).lower()
    asks = any(w in words for w in ("when", "what time", "date", "day"))
    return (asks and not proposals(ev)), f"said: {said(ev)[:160]} | proposals: {len(proposals(ev))}"
  step("a request without a time asks when", no_time)

  # 2. The time alone completes the request: a purchase proposal with the slot.
  def with_time():
    ev = say("Saturday at 11:00")
    ps = [p for p in proposals(ev) if p.get("kind", "purchase") == "purchase"]
    slot = ((ps[-1].get("service") or {}).get("booking") or {}).get("starts_at") if ps else None
    want = f"{sat.isoformat()}T11:00"
    return (bool(ps) and (slot or "").startswith(want)), f"slot: {slot} | calls: {shop_calls(ev)}"
  step("the time alone becomes a proposal booked for Saturday 11:00", with_time)

  # 3. The yes buys, once.
  def buy():
    p = pending()
    if not p:
      return False, "nothing pending"
    ev = approve(p["id"])
    rows = purchases()
    return (any(e["type"] == "receipt" for e in ev) and rows and rows[0]["voucher"] == "issued"), said(ev)[:160]
  step("approving buys and the voucher is issued", buy)

  # 4. A move is proposed, not done, until the yes.
  def move():
    ev = say("Move my spa day to Saturday at 12:00")
    ps = [p for p in proposals(ev) if p.get("kind") == "reschedule"]
    untouched = purchases()[0]["service"]["booking"]["starts_at"].startswith(f"{sat.isoformat()}T11:00")
    to = ps[-1]["change"]["to"] if ps else None
    return (bool(ps) and untouched and (to or "").startswith(f"{sat.isoformat()}T12:00")), f"to: {to} | untouched: {untouched} | said: {said(ev)[:120]}"
  step("a move is proposed and nothing moves before the yes", move)

  def move_yes():
    p = pending()
    if not p or p.get("kind") != "reschedule":
      return False, "no move pending"
    ev = approve(p["id"])
    booked = purchases()[0]["service"]["booking"]["starts_at"]
    return booked.startswith(f"{sat.isoformat()}T12:00"), f"booked: {booked} | said: {said(ev)[:120]}"
  step("approving the move changes the booking", move_yes)

  # 5. An hour the deal does not offer: no proposal, an explanation.
  def bad_hour():
    ev = say("Actually, make it Saturday at 9:00")
    return (not proposals(ev) and not pending()), f"said: {said(ev)[:160]}"
  step("an hour the spa has no slot for gets no proposal", bad_hour)

  # 6. A cancellation is proposed; a no leaves it; a yes refunds.
  def cancel():
    ev = say("Cancel my spa day, please")
    ps = [p for p in proposals(ev) if p.get("kind") == "cancel"]
    row = purchases()[0]
    return (bool(ps) and row["payment"] == "captured"), f"proposals: {[p.get('kind') for p in proposals(ev)]} | payment: {row['payment']}"
  step("a cancellation is proposed and nothing is refunded before the yes", cancel)

  def cancel_no():
    p = pending()
    if not p or p.get("kind") != "cancel":
      return False, "no cancellation pending"
    decline(p["id"])
    row = purchases()[0]
    return (row["payment"] == "captured" and pending() is None), f"payment: {row['payment']}"
  step("declining the cancellation leaves the purchase as it was", cancel_no)

  def cancel_yes():
    say("Yes, cancel my spa day")
    p = pending()
    if not p or p.get("kind") != "cancel":
      return False, f"no cancellation pending after asking again: {p}"
    ev = approve(p["id"])
    row = purchases()[0]
    return (row["payment"] == "refunded" and row["redemption"] == "cancelled_by_shopper"), f"payment: {row['payment']} | said: {said(ev)[:120]}"
  step("asking again and approving cancels and refunds", cancel_yes)

  # 7. Spanish, all in one message, another shop.
  def spanish():
    ev = say("Una cata de cerveza para dos por menos de 50 $, el sábado a las 17:00")
    ps = [p for p in proposals(ev) if p.get("kind", "purchase") == "purchase"]
    slot = ((ps[-1].get("service") or {}).get("booking") or {}).get("starts_at") if ps else None
    return (bool(ps) and "beer" in ps[-1]["title"].lower() and (slot or "").startswith(f"{sat.isoformat()}T17:00")), f"title: {ps[-1]['title'] if ps else None} | slot: {slot}"
  step("a Spanish request with a time proposes the beer tasting at 17:00", spanish)

  def spanish_no():
    p = pending()
    if not p:
      return False, "nothing pending"
    ev = decline(p["id"])
    return (pending() is None and not any(e["type"] == "receipt" for e in ev)), said(ev)[:120]
  step("declining buys nothing", spanish_no)

  total = usage()
  if total:
    print(f"model total: {int(total['requests'])} requests, {int(total['prompt_tokens'])} in / {int(total['completion_tokens'])} out tokens, ${total['cost']:.4f}, {total['seconds']:.1f}s waiting")
  print(f"{'all steps passed' if not failures else f'{failures} step(s) failed'}")
  return 1 if failures else 0


if __name__ == "__main__":
  try:
    sys.exit(main())
  except urllib.error.URLError as error:
    print(f"the agent at {AGENT} did not answer: {error}")
    sys.exit(2)
