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

"""Discovery routes for the UCP server."""

import json
import pathlib
import uuid
import config
from fastapi import APIRouter
from fastapi import Request
from fastapi import Response
from services import payment_rail
import webhook_signer

router = APIRouter()

PROFILE_TEMPLATE_PATH = pathlib.Path(__file__).parent / "discovery_profile.json"

# Profiles are stable, non-sensitive documents; the spec requires a cacheable
# response (overview.md, Discovery: profile caching): `public` with `max-age`
# of at least 60 seconds, and never `private`/`no-store`/`no-cache`.
PROFILE_CACHE_CONTROL = "public, max-age=3600"

# Generate a unique shop ID for this server instance
SHOP_ID = str(uuid.uuid4())


@router.get(
  "/.well-known/ucp",
  response_model=dict,
  summary="Get Merchant Profile",
)
async def get_merchant_profile(request: Request, response: Response):
  """Return the merchant profile and capabilities."""
  response.headers["Cache-Control"] = PROFILE_CACHE_CONTROL
  # Read template and perform simple substitution
  with PROFILE_TEMPLATE_PATH.open(encoding="utf-8") as f:
    template = f.read()

  # Replace placeholders
  profile_json = template.replace(
    "{{ENDPOINT}}", str(request.base_url).rstrip("/")
  ).replace("{{SHOP_ID}}", SHOP_ID)
  profile_json = profile_json.replace(
    "{{SHOP_NAME}}", config.get_shop()["name"]
  )

  profile_data = json.loads(profile_json)

  # Preserve the spec-required top-level `ucp` wrapper. The UCP discovery
  # profile schema (discovery/profile.json) defines `$defs.base.required =
  # ["ucp"]`, so the served body MUST be `{"ucp": {...}}`. Default
  # payment_handlers INSIDE the ucp object rather than at the document root.
  ucp = profile_data.setdefault("ucp", {})
  handlers = ucp.setdefault("payment_handlers", {})

  # With Stripe as the rail, say so: an agent pays with a Stripe payment
  # method (`pm_…`) under the `stripe` handler. The publishable key lets a
  # buyer tokenise a card with Stripe; the shop never sees the number.
  if payment_rail.rail_name() == payment_rail.StripeRail.name:
    handlers["com.stripe"] = [
      {
        "id": payment_rail.StripeRail.name,
        "name": "com.stripe",
        "version": "2026-04-08",
        "spec": "https://docs.stripe.com/api/payment_intents",
        "config": {
          "publishable_key": payment_rail.publishable_key(),
          "mode": payment_rail.StripeRail().mode,
          "credential": "A PaymentMethod id (pm_…) as a token credential.",
        },
      }
    ]

  # Publish the webhook-signing public key so platforms can verify our
  # order-event deliveries (order.md, Webhook Signature Verification /
  # signatures.md, Key Discovery). The discovery profile schema places
  # signing_keys[] at the top level of the served document (a sibling of
  # `ucp`); it is mirrored into ucp.keys[], the RFC 7517 JWK Set this
  # server's own verifier (ucp_signing._extract_keys) resolves, so both
  # discovery conventions find the key.
  jwk = webhook_signer.public_jwk()
  profile_data.setdefault("signing_keys", []).append(jwk)
  ucp.setdefault("keys", []).append(jwk)

  return profile_data
