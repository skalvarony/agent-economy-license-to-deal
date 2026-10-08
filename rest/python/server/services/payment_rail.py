"""The payment rail: the one place where money is taken and given back.

The checkout and the refund code only ever call `lock`, `capture` and
`refund`, so swapping the rail touches nothing else. --payment_rail picks
the rail, and its name is what the ledger and each order's `payment.rail`
show:

- `mock`: a labelled simulation. Every call succeeds and no money moves.
- `stripe`: Stripe, in whichever mode the key in STRIPE_SECRET_KEY is (a
  test key moves no money; test cards and test payment methods work). The
  lock is a PaymentIntent with manual capture; the capture takes it; a
  refund gives it back, or cancels it if it was never captured.

`assess` tells what the rail's fraud check made of a locked payment (Stripe
Radar); the checkout code decides what to do with it.
"""

import contextlib
import datetime
import os
from typing import Any, Protocol
import uuid

import config
from exceptions import InvalidRequestError, PaymentFailedError
import httpx

STRIPE_API = "https://api.stripe.com/v1"
STRIPE_TIMEOUT_SECONDS = 20

# One connection pool to Stripe for the whole server. Tests point TRANSPORT
# at a fake Stripe before the first call.
TRANSPORT: httpx.AsyncBaseTransport | None = None

# Error codes with which Stripe says its fraud check (Radar) blocked a charge.
FRAUD_CODES = {"fraudulent", "fraud"}
_http: httpx.AsyncClient | None = None


def stripe_http() -> httpx.AsyncClient:
  """Return the shared client to Stripe's API."""
  global _http  # noqa: PLW0603
  if _http is None or _http.is_closed:
    _http = httpx.AsyncClient(
      timeout=STRIPE_TIMEOUT_SECONDS, transport=TRANSPORT
    )
  return _http


class PaymentRail(Protocol):
  """What a rail must do. Amounts are in minor units (cents)."""

  name: str

  async def lock(
    self,
    amount: int,
    currency: str,
    reference: str,
    credential: str | None = None,
  ) -> str:
    """Authorise `amount` with `credential` and return the payment ID."""

  async def assess(self, payment_id: str) -> dict[str, Any] | None:
    """Return the rail's fraud check of a locked payment, or None.

    Keys: `level` (normal, elevated, highest, not_assessed, unknown), `score`
    (0 to 99, or None: Stripe gives it only on some plans), `outcome`
    (authorized, manual_review, issuer_declined, blocked, invalid), `reason`
    and `seller_message`. A rail with no fraud check answers None.
    """

  async def capture(self, payment_id: str) -> None:
    """Take a locked payment."""

  async def refund(self, payment_id: str, amount: int) -> None:
    """Give `amount` back, or release the lock if it was never captured."""

  async def inspect(self, payment_id: str) -> dict[str, Any]:
    """Return what the rail knows about a payment, in the console's shape.

    Keys: `rail`, `id`, `mode` (test/live/simulated), `status` (authorised,
    captured, refunded, partly_refunded, canceled, failed), `amount`,
    `captured`, `refunded` (cents), `currency`, `card` ({brand, last4,
    exp_month, exp_year} or None), `created` (ISO), `refunds` ([{id, amount,
    status, reason, created}]), `url` (the rail's own page for it, or None),
    `risk` (what `assess` returns, or None).
    """


class MockRail:
  """A simulation: every call succeeds at once and no money moves."""

  name = "mock"

  async def lock(
    self,
    amount: int,
    currency: str,
    reference: str,
    credential: str | None = None,
  ) -> str:
    """Pretend to authorise the payment."""
    del amount, currency, reference, credential  # Unused.
    return f"mock_pay_{uuid.uuid4().hex[:12]}"

  async def assess(self, payment_id: str) -> dict[str, Any] | None:
    """Say there is no fraud check: a simulation has none."""
    del payment_id  # Unused.
    return None

  async def capture(self, payment_id: str) -> None:
    """Pretend to capture the payment."""
    del payment_id  # Unused.

  async def refund(self, payment_id: str, amount: int) -> None:
    """Pretend to refund the payment."""
    del payment_id, amount  # Unused.

  async def inspect(self, payment_id: str) -> dict[str, Any]:
    """There is nothing behind a mock payment; say so."""
    return {
      "rail": self.name,
      "id": payment_id,
      "mode": "simulated",
      "status": None,
      "amount": None,
      "captured": None,
      "refunded": None,
      "currency": None,
      "card": None,
      "created": None,
      "refunds": [],
      "url": None,
      "risk": None,
    }


class StripeRail:
  """Stripe, through its REST API, in the mode of the key.

  The credential a buyer hands over is a PaymentMethod (`pm_…`): the
  storefront makes one in the browser with Stripe's card form, an agent
  uses a test one like `pm_card_visa`. The shop never sees a card number.
  """

  name = "stripe"

  def __init__(self) -> None:
    """Read the secret key from the environment."""
    self.secret = os.environ.get("STRIPE_SECRET_KEY", "")
    if not self.secret:
      raise InvalidRequestError(
        "The stripe rail needs STRIPE_SECRET_KEY in the environment"
      )
    self.http = stripe_http()
    # What Radar said at the lock, by PaymentIntent, so `assess` needs no
    # second call to Stripe for a payment this instance locked.
    self._risk: dict[str, dict[str, Any] | None] = {}

  @property
  def mode(self) -> str:
    """Return 'test' or 'live', from the key."""
    return "test" if self.secret.startswith("sk_test_") else "live"

  async def _call(
    self, method: str, path: str, data: dict[str, Any] | None = None, **headers
  ) -> dict[str, Any]:
    try:
      response = await self.http.request(
        method,
        f"{STRIPE_API}{path}",
        data=data,
        auth=(self.secret, ""),
        headers=headers,
      )
    except httpx.HTTPError as error:
      raise PaymentFailedError(
        "Stripe did not answer", code="RAIL_UNAVAILABLE", status_code=502
      ) from error
    body = response.json()
    if response.status_code >= 400:
      problem = body.get("error") or {}
      if {problem.get("decline_code"), problem.get("code")} & FRAUD_CODES:
        # Radar refused the charge itself. Say so, not "card declined".
        raise PaymentFailedError(
          "Stripe's fraud check blocked this payment", code="RISK_BLOCKED"
        )
      raise PaymentFailedError(
        f"Stripe declined: {problem.get('message', response.reason_phrase)}",
        code=(problem.get("decline_code") or problem.get("code") or "stripe")
        .upper()
        .replace("-", "_"),
      )
    return body

  async def lock(
    self,
    amount: int,
    currency: str,
    reference: str,
    credential: str | None = None,
  ) -> str:
    """Authorise the amount with manual capture; return the PaymentIntent."""
    if not credential or not credential.startswith("pm_"):
      raise PaymentFailedError(
        "Stripe takes a PaymentMethod (pm_…) as the credential",
        code="MISSING_CREDENTIAL",
      )
    intent = await self._call(
      "POST",
      "/payment_intents",
      {
        "amount": amount,
        "currency": currency.lower(),
        "payment_method": credential,
        # Cards only: a redirect-based method would need a return URL, and
        # this confirmation happens on the server, with nobody to redirect.
        "payment_method_types[]": "card",
        # The charge with its fraud check (`outcome`), in this one answer.
        "expand[]": "latest_charge",
        "confirm": "true",
        "capture_method": "manual",
        "description": f"Checkout {reference}",
        "metadata[checkout_id]": reference,
      },
      **{"Idempotency-Key": f"lock-{reference}-{amount}-{credential}"},
    )
    risk = _risk(intent.get("latest_charge"))
    if risk and risk["outcome"] == "blocked":
      await self._release(intent["id"])
      raise PaymentFailedError(
        "Stripe's fraud check blocked this payment", code="RISK_BLOCKED"
      )
    if intent.get("status") == "requires_action":
      # The card's bank wants the person to confirm (3-D Secure). The shop
      # confirms on the server with nobody at the screen, and an agent
      # cannot do this step for the person, so the payment stops here.
      await self._release(intent["id"])
      raise PaymentFailedError(
        "The card's bank asks the person to confirm this payment (3-D"
        " Secure). An agent cannot do this step for the person.",
        code="SHOPPER_ACTION_REQUIRED",
      )
    if intent.get("status") != "requires_capture":
      raise PaymentFailedError(
        f"The payment is {intent.get('status')}, not authorised",
        code="NOT_AUTHORISED",
      )
    self._risk[intent["id"]] = risk
    return intent["id"]

  async def _release(self, payment_id: str) -> None:
    """Cancel a PaymentIntent that will not be used; never fail the caller.

    The caller is already refusing the payment with a better reason than a
    failed cancel could give, and an unconfirmed intent holds no money.
    """
    with contextlib.suppress(PaymentFailedError):
      await self._call("POST", f"/payment_intents/{payment_id}/cancel")

  async def assess(self, payment_id: str) -> dict[str, Any] | None:
    """Return Radar's verdict on the charge, as read when it was locked."""
    if payment_id not in self._risk:
      intent = await self._call(
        "GET", f"/payment_intents/{payment_id}?expand[]=latest_charge"
      )
      self._risk[payment_id] = _risk(intent.get("latest_charge"))
    return self._risk[payment_id]

  async def capture(self, payment_id: str) -> None:
    """Take the authorised amount."""
    await self._call("POST", f"/payment_intents/{payment_id}/capture")

  async def inspect(self, payment_id: str) -> dict[str, Any]:
    """Read the PaymentIntent, its charge's card and its refunds."""
    intent = await self._call(
      "GET",
      f"/payment_intents/{payment_id}"
      "?expand[]=latest_charge&expand[]=latest_charge.refunds",
    )
    charge = intent.get("latest_charge") or {}
    if not isinstance(charge, dict):
      charge = {}
    card = ((charge.get("payment_method_details") or {}).get("card")) or None
    refunds = [
      {
        "id": r.get("id"),
        "amount": r.get("amount"),
        "status": r.get("status"),
        "reason": r.get("reason"),
        "created": _iso(r.get("created")),
      }
      for r in ((charge.get("refunds") or {}).get("data") or [])
    ]
    refunded = charge.get("amount_refunded") or 0
    captured = charge.get("amount_captured") or 0
    raw = intent.get("status")
    if raw == "requires_capture":
      status = "authorised"
    elif raw == "canceled":
      status = "canceled"
    elif raw == "succeeded":
      if refunded and refunded >= captured:
        status = "refunded"
      elif refunded:
        status = "partly_refunded"
      else:
        status = "captured"
    else:
      status = "failed" if charge.get("failure_code") else (raw or "unknown")
    return {
      "rail": self.name,
      "id": payment_id,
      "mode": self.mode,
      "status": status,
      "amount": intent.get("amount"),
      "captured": captured,
      "refunded": refunded,
      "currency": (intent.get("currency") or "").upper() or None,
      "card": card
      and {
        "brand": card.get("brand"),
        "last4": card.get("last4"),
        "exp_month": card.get("exp_month"),
        "exp_year": card.get("exp_year"),
      },
      "created": _iso(intent.get("created")),
      "risk": _risk(intent.get("latest_charge")),
      "refunds": refunds,
      "url": (
        "https://dashboard.stripe.com/"
        + ("test/" if self.mode == "test" else "")
        + f"payments/{payment_id}"
      ),
    }

  async def refund(self, payment_id: str, amount: int) -> None:
    """Refund a captured payment, or cancel one that was only authorised."""
    intent = await self._call("GET", f"/payment_intents/{payment_id}")
    if intent.get("status") == "requires_capture":
      await self._call("POST", f"/payment_intents/{payment_id}/cancel")
      return
    await self._call(
      "POST",
      "/refunds",
      {"payment_intent": payment_id, "amount": amount},
      **{"Idempotency-Key": f"refund-{payment_id}-{amount}"},
    )


def _risk(charge: Any) -> dict[str, Any] | None:
  """Read Radar's verdict from an expanded charge, or None if not expanded."""
  if not isinstance(charge, dict):
    return None
  outcome = charge.get("outcome") or {}
  return {
    "level": outcome.get("risk_level") or "unknown",
    # Only some Stripe plans give a score; then it is missing, not zero.
    "score": outcome.get("risk_score"),
    "outcome": outcome.get("type"),
    "reason": outcome.get("reason"),
    "seller_message": outcome.get("seller_message"),
  }


def _iso(epoch: int | None) -> str | None:
  """Turn Stripe's epoch seconds into an ISO timestamp, UTC."""
  if not epoch:
    return None
  return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).isoformat(
    timespec="seconds"
  )


RAILS: dict[str, type] = {MockRail.name: MockRail, StripeRail.name: StripeRail}


def rail_name() -> str:
  """Return the name of the rail --payment_rail picks."""
  flags = config.FLAGS
  return flags.payment_rail if flags.is_parsed() else MockRail.name


def get_rail() -> PaymentRail:
  """Return the rail named by --payment_rail."""
  name = rail_name()
  if name not in RAILS:
    raise InvalidRequestError(f"Unknown payment rail: {name}")
  return RAILS[name]()


def publishable_key() -> str:
  """Return Stripe's publishable key for the storefront's form, if any."""
  return os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
