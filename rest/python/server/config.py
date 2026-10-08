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

"""Shared configuration and startup logic for UCP servers."""

import contextlib
import json
from pathlib import Path
import uuid
from absl import flags
import db
from fastapi import FastAPI

FLAGS = flags.FLAGS

_PROFILE_CACHE = None
_SHOP_CACHE = None

DEFAULT_SHOP = {
  "name": "Flower Shop",
  "tagline": "",
  "city": "",
  "accent": "#444444",
  "hero": "",
  "hero_sub": "",
  "promo_code": "",
  "font": "",
  # Share of what a buyer pays by card that comes back as coins.
  "coins_back_percent": 0,
}

# checkout.json annotates `currency` with `ucp_request: omit` and describes
# it as "reflecting the merchant's market determination ... buyers provide
# signals, merchants determine currency". A conformant platform therefore does
# not send it, and the generated CheckoutCreateRequest has no such field. This
# sample serves a single market, so the determination is a constant.
DEFAULT_CURRENCY = "USD"


def get_default_currency() -> str:
  """Return the currency this business trades in."""
  return DEFAULT_CURRENCY


def _get_profile() -> dict:
  global _PROFILE_CACHE
  if _PROFILE_CACHE:
    return _PROFILE_CACHE

  current_dir = Path(__file__).resolve().parent
  profile_path = current_dir / "routes" / "discovery_profile.json"

  with profile_path.open(encoding="utf-8") as f:
    _PROFILE_CACHE = json.load(f)
  return _PROFILE_CACHE


def get_shop() -> dict:
  """Read and cache this shop's identity from --shop_dir/shop.json."""
  global _SHOP_CACHE
  if _SHOP_CACHE is None:
    _SHOP_CACHE = dict(DEFAULT_SHOP)
    if FLAGS.is_parsed() and FLAGS.shop_dir:
      with (Path(FLAGS.shop_dir) / "shop.json").open(encoding="utf-8") as f:
        _SHOP_CACHE.update(json.load(f))
  return _SHOP_CACHE


def get_server_version() -> str:
  """Read and cache the server version from the discovery profile."""
  profile = _get_profile()
  return profile["ucp"]["version"]


def get_payment_handlers() -> dict:
  """Read and cache the payment handlers from the discovery profile."""
  profile = _get_profile()
  return profile["ucp"].get("payment_handlers", {})


# Define flags only if they haven't been defined yet (to avoid duplicates
# during tests or re-imports)
try:
  flags.DEFINE_string("products_db_path", None, "Path to products DB")
  flags.DEFINE_string("transactions_db_path", None, "Path to transactions DB")
  flags.DEFINE_string(
    "simulation_secret",
    str(uuid.uuid4()),
    "Secret key for simulation endpoints",
  )
  flags.DEFINE_integer("port", None, "Port to run the server on")
  flags.DEFINE_boolean(
    "require_signatures",
    False,
    "Reject requests whose RFC 9421 signature is missing or invalid. When "
    "false (the default), signatures are still verified when present, but "
    "unsigned or invalid requests are allowed and only logged.",
  )
  flags.DEFINE_boolean(
    "require_signature_nonce",
    False,
    "Refuse a signed request whose signature carries no nonce (401 "
    "signature_invalid). A nonce is what lets the shop notice a copied "
    "request being replayed. False here so older clients keep working; the "
    "deployed shops turn it on (REQUIRE_SIGNATURE_NONCE=1, the default in "
    "docker-entrypoint.sh).",
  )
  flags.DEFINE_integer(
    "signature_max_age_seconds",
    300,
    "How long after `created` a signature without `expires` is still "
    "accepted. Signatures with `expires` are held to it instead.",
  )
  flags.DEFINE_boolean(
    "allow_insecure_profile_urls",
    False,
    "Permit http and loopback/private UCP-Agent profile URLs when resolving "
    "signer keys. For localhost demos and CI only; never enable in "
    "production, as it disables SSRF protections.",
  )
  flags.DEFINE_boolean(
    "secure_cookies",
    False,
    "Mark the storefront's cookies Secure. Set it when the shop is served "
    "over HTTPS (behind a proxy that terminates TLS).",
  )
  flags.DEFINE_string(
    "webhook_signing_key",
    None,
    "Path to a PEM private key (EC P-256 or Ed25519) used to sign outbound "
    "order-event webhooks as this business (order.md, Webhook Signature "
    "Verification). When unset, an ephemeral demo key is generated at "
    "startup; either way the public JWK is published in the served "
    "profile's signing_keys[].",
  )
  flags.DEFINE_integer(
    "webhook_delivery_attempts",
    3,
    "Total delivery attempts per order-event webhook (the initial attempt "
    "plus retries). order.md requires failed deliveries to be retried; the "
    "bound keeps the retry finite.",
    lower_bound=1,
  )
  flags.DEFINE_float(
    "webhook_retry_backoff_seconds",
    0.5,
    "Delay before the first webhook retry, doubling on each subsequent "
    "retry (exponential backoff).",
    lower_bound=0.0,
  )
  flags.DEFINE_string(
    "shop_dir",
    None,
    "Directory with this shop's shop.json (name, tagline, accent colour). "
    "When unset, the server runs as the unnamed sample shop.",
  )
  flags.DEFINE_string(
    "payment_rail",
    "mock",
    "Rail that takes and refunds payments: 'mock' (a labelled simulation) "
    "or 'stripe' (needs STRIPE_SECRET_KEY; a test key moves no money).",
  )
  flags.DEFINE_string(
    "ledger_path",
    None,
    "Append-only JSONL file this shop writes its ledger events to. The demo "
    "shops share one file. When unset, no events are written.",
  )
except flags.DuplicateFlagError:
  pass


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
  """Shared lifespan manager for initializing databases."""
  del app  # Unused.
  # Load (and thereby validate) the webhook-signing identity up front: a
  # misconfigured --webhook_signing_key must abort the boot loudly, not
  # surface as a swallowed per-delivery error that silently degrades every
  # webhook. Imported lazily; webhook_signer imports this module.
  import webhook_signer

  webhook_signer.signing_key()
  # In tests or if flags aren't set, these might be None, handled by caller
  if FLAGS.products_db_path and FLAGS.transactions_db_path:
    await db.manager.init_dbs(
      FLAGS.products_db_path, FLAGS.transactions_db_path
    )
  yield
  await db.manager.close()
