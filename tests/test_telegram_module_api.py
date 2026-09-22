from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import ParseJob, ParserAccount, Target, TargetAccountLink, User

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(pg_session):
    admin = User(username="adm", password_hash="x", role="admin", is_admin=True, is_active=True,
                 totp_enabled=True, totp_confirmed=True)
    pg_session.add(admin)
    pg_session.commit()

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: admin
    yield TestClient(app), pg_session, admin
    app.dependency_overrides.clear()


def _account(session, label="a", pool_mode="shared"):
    acc = ParserAccount(parser_type="telegram", label=label, pool_mode=pool_mode,
                        credentials={"api_id": 1, "api_hash": "h", "session_string": "s"})
    session.add(acc)
    session.commit()
    return acc


def test_onboard_returns_queued_row(client):
    c, session, _ = client
    _account(session)

    resp = c.post("/api/modules/telegram/onboard", json={"input": "https://t.me/durov"})

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["identifier"] == "@durov"
    assert row["onboarding_step"] == "queued"
    assert row["account"] is None
    assert session.query(ParseJob).count() == 1


def test_onboard_rejects_empty_input(client):
    c, *_ = client
    assert c.post("/api/modules/telegram/onboard", json={"input": "   "}).status_code == 400


def test_accounts_overview_reports_liveness_and_load(client):
    c, session, _ = client
    acc = _account(session)
    acc.alive = True
    acc.join_window_count = 3
    t = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=True))
    session.commit()

    rows = c.get("/api/modules/telegram/accounts/overview").json()

    assert rows[0]["alive"] is True
    assert rows[0]["targets_count"] == 1
    assert rows[0]["joins_today"] == 3
    assert rows[0]["join_daily_limit"] == 10


def test_pool_mode_toggle(client):
    c, session, _ = client
    acc = _account(session)

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/pool-mode", json={"pool_mode": "dedicated"})

    assert resp.status_code == 200
    assert resp.json()["pool_mode"] == "dedicated"
    assert c.post(f"/api/modules/telegram/accounts/{acc.id}/pool-mode", json={"pool_mode": "weird"}).status_code == 400


def test_check_alive_updates_account(client, monkeypatch):
    c, session, _ = client
    acc = _account(session)
    import app.routers.api as api_mod
    monkeypatch.setattr(api_mod, "refresh_account_session_info_sync", lambda creds: {"alive": True, "username": "x"})

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/check-alive")

    assert resp.status_code == 200
    assert resp.json()["alive"] is True
    session.refresh(acc)
    assert acc.alive is True and acc.last_checked_at is not None


def test_reassign_detaches_and_requeues(client):
    c, session, _ = client
    a1 = _account(session, "a1")
    a2 = _account(session, "a2", pool_mode="dedicated")
    t = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=a1.id, is_active=True))
    session.commit()

    resp = c.post(f"/api/modules/telegram/targets/{t.id}/reassign", json={"account_id": a2.id})

    assert resp.status_code == 200
    assert resp.json()["onboarding_step"] == "queued"
    active = session.execute(select(TargetAccountLink).where(TargetAccountLink.is_active.is_(True))).scalars().all()
    assert active == []
    job = session.execute(select(ParseJob)).scalar_one()
    assert job.payload["account_id"] == a2.id


def test_retry_onboarding_from_failed(client):
    c, session, _ = client
    _account(session)
    t = Target(parser_type="telegram", name="c", identifier="@c", config={}, onboarding_step="failed", onboarding_error="x")
    session.add(t)
    session.commit()

    resp = c.post(f"/api/modules/telegram/targets/{t.id}/retry-onboarding")

    assert resp.status_code == 200
    assert resp.json()["onboarding_step"] == "queued"
    assert resp.json()["onboarding_error"] is None


def test_module_overview_includes_account_and_step(client):
    c, session, _ = client
    acc = _account(session)
    acc.alive = True
    t = Target(parser_type="telegram", name="c", identifier="@c", config={"kind": "channel"}, onboarding_step="joined")
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=True))
    session.commit()

    data = c.get("/api/modules/telegram").json()
    row = next(x for x in data["targets"] if x["id"] == t.id)

    assert row["account"] == {"id": acc.id, "label": "a", "alive": True}
    assert row["onboarding_step"] == "joined"
    assert row["kind"] == "channel"


def test_account_targets_lists_only_its_active_links(client):
    c, session, _ = client
    a1 = _account(session, "a1")
    a2 = _account(session, "a2")
    t1 = Target(parser_type="telegram", name="c1", identifier="@c1", config={})
    t2 = Target(parser_type="telegram", name="c2", identifier="@c2", config={})
    session.add_all([t1, t2])
    session.commit()
    session.add_all([
        TargetAccountLink(target_id=t1.id, account_id=a1.id, is_active=True),
        TargetAccountLink(target_id=t2.id, account_id=a2.id, is_active=True),
    ])
    session.commit()

    rows = c.get(f"/api/modules/telegram/accounts/{a1.id}/targets").json()
    assert [r["id"] for r in rows] == [t1.id]
