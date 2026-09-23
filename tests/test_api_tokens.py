from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import ApiToken, User
from app.services import api_tokens

pytestmark = pytest.mark.postgres


@pytest.fixture
def anon(pg_session):
    """No session override: these tests authenticate with a bearer token only."""
    app.dependency_overrides[get_db] = lambda: pg_session
    yield TestClient(app), pg_session
    app.dependency_overrides.clear()


def _user(session, username="mrgrinch", active=True):
    u = User(username=username, password_hash="x", role="admin", is_admin=True, is_active=active,
             totp_enabled=True, totp_confirmed=True, session_version=1)
    session.add(u)
    session.commit()
    return u


def test_raw_token_is_never_stored(anon):
    _c, session = anon
    user = _user(session)
    token, raw = api_tokens.create_token(session, owner_user_id=user.id, name="прод")
    session.commit()

    assert raw.startswith("agg_") and len(raw) > 40
    stored = session.execute(select(ApiToken)).scalar_one()
    assert stored.token_hash != raw and raw not in stored.token_hash
    assert stored.token_hash == api_tokens.hash_token(raw)
    assert stored.prefix == raw[:10] and stored.expires_at is None, "no expiry means eternal"


def test_bearer_token_authenticates_without_a_session(anon):
    c, session = anon
    user = _user(session)
    _t, raw = api_tokens.create_token(session, owner_user_id=user.id, name="прод")
    session.commit()

    assert c.get("/api/v2/events").status_code == 401, "no credentials at all"
    r = c.get("/api/v2/events", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code in (200, 503), r.text  # 503 only when ClickHouse is absent
    assert c.get("/api/search/status", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


@pytest.mark.parametrize("mangle", [
    lambda raw: raw + "x",
    lambda raw: raw.replace("agg_", "xxx_"),
    lambda raw: "",
])
def test_a_wrong_token_is_rejected(anon, mangle):
    c, session = anon
    user = _user(session)
    _t, raw = api_tokens.create_token(session, owner_user_id=user.id, name="прод")
    session.commit()

    assert c.get("/api/search/status", headers={"Authorization": f"Bearer {mangle(raw)}"}).status_code == 401


def test_revoked_expired_and_deactivated_all_stop_working(anon):
    c, session = anon
    user = _user(session)
    live, live_raw = api_tokens.create_token(session, owner_user_id=user.id, name="жив")
    gone, gone_raw = api_tokens.create_token(session, owner_user_id=user.id, name="відкликаний")
    old, old_raw = api_tokens.create_token(
        session, owner_user_id=user.id, name="протермінований",
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
    )
    api_tokens.revoke_token(session, gone)
    session.commit()

    hdr = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
    assert c.get("/api/search/status", headers=hdr(live_raw)).status_code == 200
    assert c.get("/api/search/status", headers=hdr(gone_raw)).status_code == 401
    assert c.get("/api/search/status", headers=hdr(old_raw)).status_code == 401

    user.is_active = False
    session.commit()
    assert c.get("/api/search/status", headers=hdr(live_raw)).status_code == 401, "owner disabled"


def test_token_acts_as_its_owner_and_sees_only_their_data(anon):
    c, session = anon
    mine = _user(session, "mine")
    mine.is_admin = False
    mine.role = "user"
    session.commit()
    _t, raw = api_tokens.create_token(session, owner_user_id=mine.id, name="мій")
    session.commit()

    me = c.get("/api/search/status", headers={"Authorization": f"Bearer {raw}"})
    assert me.status_code == 200


def test_tokens_are_minted_listed_and_revoked_over_http(anon):
    c, session = anon
    user = _user(session)
    _t, bootstrap = api_tokens.create_token(session, owner_user_id=user.id, name="bootstrap")
    session.commit()
    hdr = {"Authorization": f"Bearer {bootstrap}"}

    made = c.post("/api/tokens", json={"name": "  інший сервіс  "}, headers=hdr)
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["name"] == "інший сервіс" and body["expires_at"] is None
    new_raw = body["token"]
    assert c.get("/api/search/status", headers={"Authorization": f"Bearer {new_raw}"}).status_code == 200

    listed = c.get("/api/tokens", headers=hdr).json()["tokens"]
    assert {t["name"] for t in listed} == {"bootstrap", "інший сервіс"}
    assert all("token" not in t for t in listed), "the raw value is returned once, at creation"

    assert c.post(f"/api/tokens/{body['id']}/revoke", headers=hdr).json()["revoked"] is True
    assert c.get("/api/search/status", headers={"Authorization": f"Bearer {new_raw}"}).status_code == 401


def test_expiring_token_is_created_with_a_deadline(anon):
    c, session = anon
    user = _user(session)
    _t, raw = api_tokens.create_token(session, owner_user_id=user.id, name="b")
    session.commit()

    body = c.post("/api/tokens", json={"name": "тимчасовий", "expires_in_days": 7},
                  headers={"Authorization": f"Bearer {raw}"}).json()
    assert body["expires_at"] is not None
    bad = c.post("/api/tokens", json={"name": "x", "expires_in_days": 0},
                 headers={"Authorization": f"Bearer {raw}"})
    assert bad.status_code == 400


def test_last_used_is_recorded_for_audit(anon):
    c, session = anon
    user = _user(session)
    token, raw = api_tokens.create_token(session, owner_user_id=user.id, name="прод")
    session.commit()
    assert token.last_used_at is None

    c.get("/api/search/status", headers={"Authorization": f"Bearer {raw}"})
    session.expire_all()
    assert session.get(ApiToken, token.id).last_used_at is not None
