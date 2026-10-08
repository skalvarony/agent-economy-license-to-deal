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

"""RFC 9421 request signing, so a shop can tell this agent is genuine.

Adapted from the UCP sample client (`rest/python/client/flower_shop/
signing.py`). The agent signs every request with its private key. A shop finds
the matching public key in the agent's profile, whose address travels in the
`UCP-Agent` header, and checks the signature against it.
"""

import base64
import hashlib
import pathlib
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
import httpx

_ES256_COORD_BYTES = 32


def _normalize_authority(host: str) -> str:
  """Lowercase an authority and strip the scheme's default port."""
  authority = host.lower()
  for port in (":443", ":80"):
    if authority.endswith(port):
      return authority[: -len(port)]
  return authority


def _b64u(data: bytes) -> str:
  """Encode bytes as base64url text without padding."""
  return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def content_digest(body: bytes) -> str:
  """Return the RFC 9530 sha-256 Content-Digest of the raw body."""
  digest = hashlib.sha256(body).digest()
  return "sha-256=:" + base64.b64encode(digest).decode("ascii") + ":"


def public_jwk(public_key: ec.EllipticCurvePublicKey, kid: str) -> dict:
  """Export an ES256 public key as a JWK."""
  numbers = public_key.public_numbers()
  return {
    "kid": kid,
    "kty": "EC",
    "crv": "P-256",
    "x": _b64u(numbers.x.to_bytes(_ES256_COORD_BYTES, "big")),
    "y": _b64u(numbers.y.to_bytes(_ES256_COORD_BYTES, "big")),
    "use": "sig",
    "alg": "ES256",
  }


class RequestSigner(httpx.Auth):
  """An httpx auth flow that signs every request.

  It signs the method, authority and path (and the query when there is one),
  the content digest and type of a request with a body, and the
  idempotency-key and ucp-agent headers when the request carries them.
  """

  requires_request_body = True

  def __init__(self, private_key: ec.EllipticCurvePrivateKey, kid: str) -> None:
    """Store the signing key and its identifier."""
    self._key = private_key
    self._kid = kid

  def auth_flow(self, request):
    """Add Content-Digest, Signature-Input and Signature to the request."""
    body = request.content or b""
    lowered = {k.lower(): v for k, v in request.headers.items()}
    split = urlsplit(str(request.url))

    if body:
      digest = content_digest(body)
      request.headers["Content-Digest"] = digest
      lowered["content-digest"] = digest
      if "content-type" not in lowered:
        request.headers["Content-Type"] = "application/json"
        lowered["content-type"] = "application/json"

    components = ["@method", "@authority", "@path"]
    if split.query:
      components.append("@query")
    if body:
      components.extend(["content-digest", "content-type"])
    if "idempotency-key" in lowered:
      components.append("idempotency-key")
    if "ucp-agent" in lowered:
      components.append("ucp-agent")

    created = int(time.time())
    raw_params = (
      "(" + " ".join(f'"{c}"' for c in components) + ")"
      f';created={created};keyid="{self._kid}"'
    )

    def resolve(name: str) -> str:
      if name == "@method":
        return request.method.upper()
      if name == "@authority":
        return _normalize_authority(split.netloc)
      if name == "@path":
        return split.path or "/"
      if name == "@query":
        return "?" + split.query
      return lowered[name]

    lines = [f'"{c}": {resolve(c)}' for c in components]
    lines.append(f'"@signature-params": {raw_params}')
    base = "\n".join(lines).encode("utf-8")

    der = self._key.sign(base, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(_ES256_COORD_BYTES, "big") + s.to_bytes(
      _ES256_COORD_BYTES, "big"
    )
    request.headers["Signature-Input"] = f"sig1={raw_params}"
    request.headers["Signature"] = (
      "sig1=:" + base64.b64encode(raw).decode("ascii") + ":"
    )
    yield request


def load_identity(
  key_file: pathlib.Path, kid: str
) -> tuple[RequestSigner, dict]:
  """Return the agent's signer and the public key to publish.

  The private key is made on the first run and kept in `key_file`, so the
  agent is the same agent after a restart. Shops remember an agent's public
  key for a few minutes, and would turn away one that came back with another.
  """
  if key_file.exists():
    key = serialization.load_pem_private_key(key_file.read_bytes(), None)
  else:
    key = ec.generate_private_key(ec.SECP256R1())
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.touch(mode=0o600)
    key_file.write_bytes(
      key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
      )
    )
  return RequestSigner(key, kid), public_jwk(key.public_key(), kid)
