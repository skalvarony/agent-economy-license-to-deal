#!/usr/bin/env python3
"""Show that a copied signed request cannot be replayed.

Signs one catalog search the way the agent does (its own signing code, a
throwaway key), sends it to a local shop, then sends the identical bytes again.
The shop must answer the first normally and the second 401 signature_replayed;
the exit code says whether it did, so a deploy can run it as a check.

Start the shop with signatures on first:  REQUIRE_SIGNATURES=1 scripts/shops.sh start
Run it from the agent folder (it needs httpx and cryptography):

  cd agent && uv run python ../scripts/replay-demo.py [http://localhost:8181]
"""
import http.server
import json
import pathlib
import sys
import threading
import uuid

import httpx
from cryptography.hazmat.primitives.asymmetric import ec

# The agent's own signing code, so the demo signs exactly as the agent does.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "agent"))
from signing import RequestSigner, public_jwk  # noqa: E402

shop = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8181").rstrip("/")
key = ec.generate_private_key(ec.SECP256R1())
profile = json.dumps({"ucp": {"keys": [public_jwk(key.public_key(), "demo")]}})


class Profile(http.server.BaseHTTPRequestHandler):
  """Serve the demo agent's public key, as the shop expects of an agent."""

  def do_GET(self):  # noqa: N802
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    self.wfile.write(profile.encode())

  def log_message(self, *args):
    pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Profile)
threading.Thread(target=server.serve_forever, daemon=True).start()

request = httpx.Request(
  "POST",
  f"{shop}/catalog/search",
  json={"query": "spa"},
  headers={
    "UCP-Agent": f'profile="http://127.0.0.1:{server.server_port}/p.json"',
    "Idempotency-Key": str(uuid.uuid4()),
    "Request-Id": str(uuid.uuid4()),
  },
)
next(RequestSigner(key, "demo").auth_flow(request))  # adds the signature
print("Signature-Input:", request.headers["Signature-Input"])

with httpx.Client() as client:
  first = client.send(request)
  print(f"first send    : {first.status_code} {first.text[:110]}")
  copy = client.send(request)
  print(f"identical copy: {copy.status_code} {copy.text[:110]}")

# Also a check for the deploy: exit 1 when the copy was not refused.
ok = first.status_code == 200 and copy.status_code == 401
ok = ok and "signature_replayed" in copy.text
print("replay refused" if ok else "REPLAY NOT REFUSED")
sys.exit(0 if ok else 1)
