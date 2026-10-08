"""Checks the shop runs on the buyer's approval before completing a checkout.

A stub: every checkout passes. The real checks replace `run_checks` and keep
its signature, so nothing else in the shop changes:

  1. Agent signature    401 signature_missing / signature_invalid /
                        signature_expired / signature_replayed
  2. Buyer's approval   403 approval_invalid
  3. Unchanged since    409 requires_consent
  4. Capacity           409 sold_out
  5. Payment mandate    402 payment_invalid

Two of these already have an unsigned counterpart in the shop. With
--require_signatures the server rejects agents whose request signature is
missing or invalid, and the checkout service answers `requires_consent` when
the total moved after the platform last fetched the checkout.
"""

from typing import Any


def run_checks(
  headers: dict[str, str],
  body: dict[str, Any],
  checkout_jwt: str | None,
  seller_state: dict[str, Any],
) -> dict[str, Any] | None:
  """Return None when the checkout may complete, else {status, error}.

  Args:
    headers: The complete request's HTTP headers.
    body: The complete request's JSON body.
    checkout_jwt: The checkout as the shop signed it; None until shops sign.
    seller_state: What the shop knows; today only `checkout_id`.

  """
  del headers, body, checkout_jwt, seller_state  # Unused by the stub.
  return None
