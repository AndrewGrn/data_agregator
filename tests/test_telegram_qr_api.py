from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import ParserAccount, User
from app.services import telegram_qr_login as qr

pytestmark = pytest.mark.postgres


class _Qr:
    def __init__(self):
        self.url = "tg://login?token=X"
        self.expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)

    async def wait(self, timeout=None): return SimpleNamespace(id=1)
    async def recreate(self): ...


class _Client:
    def __init__(self):
        self.session = SimpleNamespace(save=lambda: "SESSION-STRING")

    async def connect(self): ...
    async def disconnect(self): ...
    async def qr_login(self): return _Qr()
    async def sign_in(self, password=None): ...
    async def get_me(self): return SimpleNamespace(username="alice", phone="380671234567", id=42)


@pytest.fixture
def client(pg_session, monkeypatch):
    admin = User(username="adm", password_hash="x", role="admin", is_admin=True, is_active=True,
                 totp_enabled=True, totp_confirmed=True)
    pg_session.add(admin)
    pg_session.commit()
    monkeypatch.setattr(qr, "_default_client_factory", lambda a, b: _Client())
    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: admin
    yield TestClient(app), pg_session
    app.dependency_overrides.clear()
    qr._SESSIONS.clear()


def test_start_then_status_creates_account_once(client):
    c, session = client
    started = c.post("/api/modules/telegram/accounts/qr-login/start",
                     json={"api_id": 1, "api_hash": "h", "label": "qr-acc"}).json()
    assert started["status"] == "pending"
    assert started["qr_svg"].startswith("data:image/svg+xml")
    token = started["token"]

    view = None
    for _ in range(50):
        view = c.get(f"/api/modules/telegram/accounts/qr-login/{token}").json()
        if view["status"] == "done":
            break
    assert view["status"] == "done"
    assert view["account_id"] is not None

    again = c.get(f"/api/modules/telegram/accounts/qr-login/{token}").json()
    assert again["account_id"] == view["account_id"]
    accounts = session.execute(select(ParserAccount).where(ParserAccount.label == "qr-acc")).scalars().all()
    assert len(accounts) == 1
    creds = accounts[0].credentials
    assert creds["session_string"] == "SESSION-STRING"
    assert creds["api_id"] == "1" and creds["api_hash"] == "h"
    assert creds["username"] == "alice" and creds["phone"] == "380671234567"
    assert creds["session_status"]["alive"] is True
    assert "SESSION-STRING" not in c.get(f"/api/modules/telegram/accounts/qr-login/{token}").text


def test_duplicate_label_rejected(client):
    c, session = client
    session.add(ParserAccount(parser_type="telegram", label="taken", credentials={}))
    session.commit()
    resp = c.post("/api/modules/telegram/accounts/qr-login/start", json={"api_id": 1, "api_hash": "h", "label": "taken"})
    assert resp.status_code == 400


def test_password_endpoint_rejects_when_not_needed(client):
    c, _ = client
    token = c.post("/api/modules/telegram/accounts/qr-login/start",
                   json={"api_id": 1, "api_hash": "h", "label": "a2"}).json()["token"]
    assert c.post(f"/api/modules/telegram/accounts/qr-login/{token}/password", json={"password": "x"}).status_code == 400


def test_unknown_token_is_404(client):
    c, _ = client
    assert c.get("/api/modules/telegram/accounts/qr-login/nope").status_code == 404


def test_cancel(client):
    c, _ = client
    token = c.post("/api/modules/telegram/accounts/qr-login/start",
                   json={"api_id": 1, "api_hash": "h", "label": "a3"}).json()["token"]
    assert c.post(f"/api/modules/telegram/accounts/qr-login/{token}/cancel").status_code == 200
    assert c.get(f"/api/modules/telegram/accounts/qr-login/{token}").status_code == 404
