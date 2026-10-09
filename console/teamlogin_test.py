"""Tests of the team's sign-in (shared/teamlogin.py, linked here)."""

import bcrypt
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import teamlogin

PASSWORD = "correct horse battery"


def make_app(monkeypatch, tmp_path, users=True):
  digest = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
  monkeypatch.setenv(
    "LOGIN_USERS", f"ana:{digest},bo:{digest}" if users else ""
  )
  monkeypatch.delenv("AGENT_USER", raising=False)
  monkeypatch.setenv("SIMULATION_SECRET", "s3cret")
  app = FastAPI()

  @app.get("/", response_class=teamlogin.HTMLResponse)
  async def home():
    return "<h1>home</h1>"

  @app.get("/api/thing")
  async def thing():
    return {"ok": True}

  @app.get("/profile.json")
  async def profile():
    return {"public": True}

  teamlogin.install(app, "Test app", tmp_path, open_paths=("/profile.json",))
  return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(monkeypatch, tmp_path):
  return make_app(monkeypatch, tmp_path)


def test_pages_redirect_and_apis_refuse_without_a_session(client):
  page = client.get("/?x=1", headers={"Accept": "text/html"})
  assert page.status_code == 303
  assert page.headers["location"] == "/login?next=%2F%3Fx%3D1"
  assert client.get("/api/thing").status_code == 401
  assert client.get("/api/whoami").json() == {"user": None, "open": False}


def test_open_paths_and_the_merchant_secret_pass(client):
  assert client.get("/profile.json").json() == {"public": True}
  assert client.get(
    "/api/thing", headers={"Simulation-Secret": "s3cret"}
  ).json() == {"ok": True}
  assert (
    client.get("/api/thing", headers={"Simulation-Secret": "no"}).status_code
    == 401
  )


def test_sign_in_sign_out(client):
  wrong = client.post(
    "/login", data={"username": "ana", "password": "nope", "next": "/"}
  )
  assert wrong.status_code == 401
  assert "password don&#x27;t match" in wrong.text
  assert teamlogin.COOKIE not in client.cookies

  ok = client.post(
    "/login",
    data={"username": "ana", "password": PASSWORD, "next": "/api/thing"},
  )
  assert ok.status_code == 303
  assert ok.headers["location"] == "/api/thing"
  assert teamlogin.COOKIE in client.cookies
  assert client.get("/api/thing").json() == {"ok": True}
  assert client.get("/api/whoami").json()["user"] == "ana"
  # Signed in, the sign-in page sends you on.
  assert client.get("/login?next=/x").headers["location"] == "/x"

  out = client.post("/logout")
  assert out.status_code == 303
  assert client.get("/api/thing").status_code == 401


def test_next_stays_on_this_site(client):
  gone = client.post(
    "/login",
    data={
      "username": "bo",
      "password": PASSWORD,
      "next": "https://evil.example/",
    },
  )
  assert gone.headers["location"] == "/"
  client.post("/logout")
  gone = client.post(
    "/login", data={"username": "bo", "password": PASSWORD, "next": "//evil"}
  )
  assert gone.headers["location"] == "/"


def test_a_forged_or_expired_cookie_is_no_session(client):
  sessions = teamlogin.Sessions(b"key")
  assert sessions.read(sessions.make("ana")) == "ana"
  cookie = sessions.make("ana")
  # Flip the last character of the signature: one in sixteen times it is
  # already a 0, so "0" would not have forged anything.
  forged = cookie[:-1] + ("1" if cookie[-1] == "0" else "0")
  assert sessions.read(forged) is None
  assert sessions.read("ana|1|deadbeef") is None
  assert teamlogin.Sessions(b"other").read(sessions.make("ana")) is None


def test_too_many_wrong_passwords_lock_the_address(client):
  for _ in range(teamlogin.MAX_FAILURES):
    client.post(
      "/login", data={"username": "ana", "password": "x", "next": "/"}
    )
  locked = client.post(
    "/login", data={"username": "ana", "password": PASSWORD, "next": "/"}
  )
  assert locked.status_code == 429
  assert "Too many tries" in locked.text


def test_without_users_the_app_runs_open(monkeypatch, tmp_path):
  client = make_app(monkeypatch, tmp_path, users=False)
  assert client.get("/api/thing").json() == {"ok": True}
  assert client.get("/api/whoami").json() == {"user": None, "open": True}
