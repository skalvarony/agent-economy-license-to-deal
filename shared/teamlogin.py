"""A sign-in page and a session cookie for the team's pages.

The console, the venue simulator and each person's agent are for the team
only. Instead of the browser's own password prompt, each app gets a sign-in
page, a signed session cookie that lasts a week, and a sign-out button.

  GET  /login            the page (with `?next=` to come back to)
  POST /login            username + password, as a form
  POST /logout           drops the session
  GET  /api/whoami       {"user": "..."} for the page's corner

Who may sign in comes from the environment:

  LOGIN_USERS           "user:bcrypt-hash,user:bcrypt-hash"
  AGENT_USER + AGENT_PASSWORD_HASH   one user, the older pair (fallback)

The hash is bcrypt, as `caddy hash-password` makes it. With no users set
there is no sign-in at all: that is local development and the tests. The
cookie is signed with LOGIN_SECRET, or with a secret kept in the app's run
directory, made on the first start.

A request that carries the merchant secret (`Simulation-Secret`, the value
of SIMULATION_SECRET) is let through as the team: that is how the console
asks an agent for its evidence, server to server.

This file is one and the same in console/, venue/ and agent/ (a link to
shared/teamlogin.py); the images copy it in.
"""

import asyncio
import collections
import hashlib
import hmac
import html
import os
import pathlib
import secrets
import time
from typing import Any
from urllib.parse import quote, urlparse

import bcrypt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "team_session"
SESSION_SECONDS = 7 * 24 * 3600
# After this many wrong passwords from one address, a wait before the next.
MAX_FAILURES = 8
LOCKOUT_SECONDS = 600


def _users() -> dict[str, str]:
  """Return {user: bcrypt hash} from the environment."""
  users: dict[str, str] = {}
  for pair in os.environ.get("LOGIN_USERS", "").split(","):
    user, _, digest = pair.strip().partition(":")
    if user and digest:
      users[user] = digest
  if (
    not users
    and os.environ.get("AGENT_USER")
    and os.environ.get("AGENT_PASSWORD_HASH")
  ):
    users[os.environ["AGENT_USER"]] = os.environ["AGENT_PASSWORD_HASH"]
  return users


def _secret(run_dir: pathlib.Path) -> bytes:
  """Return the key the cookie is signed with, making one on the first run."""
  if os.environ.get("LOGIN_SECRET"):
    return os.environ["LOGIN_SECRET"].encode()
  path = run_dir / "session_secret"
  if path.is_file():
    return path.read_text().strip().encode()
  key = secrets.token_urlsafe(32)
  run_dir.mkdir(parents=True, exist_ok=True)
  path.write_text(key)
  path.chmod(0o600)
  return key.encode()


class Sessions:
  """Signs and reads the session cookie."""

  def __init__(self, key: bytes) -> None:
    """Keep the signing key."""
    self.key = key

  def make(self, user: str) -> str:
    """Return a cookie value for `user`, good for a week."""
    body = f"{user}|{int(time.time()) + SESSION_SECONDS}"
    return f"{body}|{self._sign(body)}"

  def read(self, value: str | None) -> str | None:
    """Return the user a cookie names, or None if it is bad or old."""
    if not value or value.count("|") != 2:
      return None
    user, expiry, signature = value.split("|")
    body = f"{user}|{expiry}"
    if not hmac.compare_digest(self._sign(body), signature):
      return None
    if not expiry.isdigit() or int(expiry) < time.time():
      return None
    return user

  def _sign(self, body: str) -> str:
    return hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()


def _is_https(request: Request) -> bool:
  return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _safe_next(value: str | None) -> str:
  """Only paths on this site: no sending people elsewhere after sign-in."""
  if not value or not value.startswith("/") or value.startswith("//"):
    return "/"
  return value if not urlparse(value).netloc else "/"


def install(
  app: FastAPI,
  name: str,
  run_dir: pathlib.Path,
  open_paths: tuple[str, ...] = (),
) -> None:
  """Put the sign-in in front of `app`; `open_paths` are let through."""
  users = _users()
  if not users:
    # Nobody to sign in as: the app runs open (local development, tests).
    @app.get("/api/whoami")
    async def whoami_open() -> dict[str, Any]:
      return {"user": None, "open": True}

    return

  sessions = Sessions(_secret(run_dir))
  merchant_secret = os.environ.get("SIMULATION_SECRET", "")
  failures: dict[str, collections.deque] = collections.defaultdict(
    collections.deque
  )
  always_open = ("/login", "/logout", "/static/", "/api/whoami", *open_paths)

  def user_of(request: Request) -> str | None:
    if merchant_secret and hmac.compare_digest(
      request.headers.get("simulation-secret", ""), merchant_secret
    ):
      return "console"
    return sessions.read(request.cookies.get(COOKIE))

  @app.middleware("http")
  async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(always_open) or user_of(request):
      return await call_next(request)
    if "text/html" in request.headers.get("accept", ""):
      target = path + (f"?{request.url.query}" if request.url.query else "")
      return RedirectResponse(f"/login?next={quote(target, safe='')}", 303)
    return JSONResponse({"detail": "Sign in first"}, status_code=401)

  def locked(address: str) -> int:
    """Seconds this address still has to wait, or 0."""
    recent = failures[address]
    while recent and recent[0] < time.time() - LOCKOUT_SECONDS:
      recent.popleft()
    if len(recent) >= MAX_FAILURES:
      return int(recent[0] + LOCKOUT_SECONDS - time.time()) + 1
    return 0

  def address_of(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (
      request.client.host if request.client else "?"
    )

  @app.get("/login", include_in_schema=False)
  async def login_page(request: Request) -> HTMLResponse:
    if user_of(request):
      return RedirectResponse(_safe_next(request.query_params.get("next")), 303)
    return HTMLResponse(page(name, request.query_params.get("next") or "/"))

  @app.post("/login", include_in_schema=False)
  async def login(request: Request):
    form = await request.form()
    user = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    target = _safe_next(str(form.get("next") or "/"))
    address = address_of(request)
    wait = locked(address)
    if wait:
      return HTMLResponse(
        page(
          name,
          target,
          f"Too many tries. Wait {wait // 60 + 1} minutes.",
          user,
        ),
        status_code=429,
      )
    digest = users.get(user)
    ok = bool(digest) and bcrypt.checkpw(password.encode(), digest.encode())
    if not ok:
      failures[address].append(time.time())
      await asyncio.sleep(0.5)  # A wrong guess costs a moment.
      return HTMLResponse(
        page(name, target, "That user and password don't match.", user),
        status_code=401,
      )
    failures.pop(address, None)
    response = RedirectResponse(target, 303)
    response.set_cookie(
      COOKIE,
      sessions.make(user),
      max_age=SESSION_SECONDS,
      httponly=True,
      samesite="lax",
      secure=_is_https(request),
      path="/",
    )
    return response

  @app.post("/logout", include_in_schema=False)
  async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", 303)
    response.delete_cookie(COOKIE, path="/")
    return response

  @app.get("/api/whoami")
  async def whoami(request: Request) -> dict[str, Any]:
    return {"user": user_of(request), "open": False}


TEMPLATE = pathlib.Path(__file__).with_name("teamlogin.html")


def page(name: str, target: str, error: str = "", user: str = "") -> str:
  """Render the sign-in page from teamlogin.html, next to this file."""
  notice = (
    f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
  )
  return (
    TEMPLATE.read_text(encoding="utf-8")
    .replace("{{NAME}}", html.escape(name))
    .replace("{{NEXT}}", html.escape(target))
    .replace("{{USER}}", html.escape(user))
    .replace("{{NOTICE}}", notice)
  )
