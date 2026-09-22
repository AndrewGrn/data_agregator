from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from telethon.errors import SessionPasswordNeededError

from app.services import telegram_qr_login as qr


class _FakeQr:
    """Scripted QRLogin: each wait() pops the next outcome."""

    def __init__(self, outcomes: list[str]):
        self.outcomes = list(outcomes)
        self.recreates = 0
        self.url = "tg://login?token=T0"
        self.expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)

    async def wait(self, timeout=None):
        outcome = self.outcomes.pop(0)
        if outcome == "timeout":
            raise asyncio.TimeoutError
        if outcome == "password":
            raise SessionPasswordNeededError(request=None)
        return SimpleNamespace(id=1)

    async def recreate(self):
        self.recreates += 1
        self.url = f"tg://login?token=T{self.recreates}"
        self.expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)


class _FakeClient:
    def __init__(self, fake_qr: _FakeQr):
        self._qr = fake_qr
        self.signed_in_with: str | None = None
        self.disconnected = False
        self.session = SimpleNamespace(save=lambda: "SESSION-STRING")

    async def connect(self): ...
    async def disconnect(self): self.disconnected = True
    async def qr_login(self): return self._qr
    async def sign_in(self, password=None): self.signed_in_with = password
    async def get_me(self): return SimpleNamespace(username="Alice", phone="380671234567", id=42)


def test_start_exposes_url_and_svg():
    async def go():
        fake = _FakeQr(["ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        assert s.status == "pending"
        assert s.url == "tg://login?token=T0"
        assert s.qr_svg.startswith("data:image/svg+xml")
        assert qr.get_qr_session(s.token) is s
        await s._task
        return s, client

    s, client = asyncio.run(go())
    assert s.status == "done"
    assert s.session_string == "SESSION-STRING"
    assert s.me["username"] == "alice"
    assert client.disconnected is True


def test_token_timeout_recreates_and_updates_url():
    async def go():
        fake = _FakeQr(["timeout", "timeout", "ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s, fake

    s, fake = asyncio.run(go())
    assert fake.recreates == 2
    assert s.url == "tg://login?token=T2"
    assert s.status == "done"


def test_password_needed_then_submit_signs_in():
    async def go():
        fake = _FakeQr(["password"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        for _ in range(50):
            if s.status == "password_needed":
                break
            await asyncio.sleep(0.01)
        assert s.status == "password_needed"
        assert qr.submit_password(s.token, "cloud-pw") is True
        await s._task
        return s, client

    s, client = asyncio.run(go())
    assert client.signed_in_with == "cloud-pw"
    assert s.status == "done"


def test_submit_password_when_not_needed_is_rejected():
    async def go():
        fake = _FakeQr(["ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s

    s = asyncio.run(go())
    assert qr.submit_password(s.token, "x") is False
    assert qr.submit_password("no-such-token", "x") is False


def test_ttl_expiry_marks_expired(monkeypatch):
    monkeypatch.setattr(qr.settings, "telegram_qr_login_ttl_seconds", 0)

    async def go():
        fake = _FakeQr(["timeout"] * 5)
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s, client

    s, client = asyncio.run(go())
    assert s.status == "expired"
    assert client.disconnected is True


def test_cancel_removes_session():
    async def go():
        fake = _FakeQr(["timeout"] * 100)
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await qr.cancel_qr_login(s.token)
        return s, client

    s, client = asyncio.run(go())
    assert qr.get_qr_session(s.token) is None
    assert client.disconnected is True


def test_public_view_never_leaks_session_string():
    async def go():
        fake = _FakeQr(["ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s

    s = asyncio.run(go())
    view = qr.public_view(s)
    assert set(view) == {"token", "status", "url", "qr_svg", "error", "account_id", "expires_at"}
    assert "SESSION-STRING" not in str(view)
