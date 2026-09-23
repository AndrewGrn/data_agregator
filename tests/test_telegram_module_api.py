from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import JobStatus, ParseJob, ParserAccount, Target, TargetAccountLink, User

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


def test_account_disable_deactivates_links_and_flags_targets(client):
    c, session, _ = client
    acc = _account(session)
    t = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=True))
    session.commit()

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/disable")

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    session.refresh(t)
    link = session.execute(select(TargetAccountLink).where(TargetAccountLink.account_id == acc.id)).scalar_one()
    assert link.is_active is False
    assert t.onboarding_status.value == "needs_account"


def test_account_enable_reactivates(client):
    c, session, _ = client
    acc = _account(session)
    acc.is_active = False
    session.commit()

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/enable")

    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_account_delete_refuses_while_links_active(client):
    c, session, _ = client
    acc = _account(session)
    t = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=True))
    session.commit()

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/delete")

    assert resp.status_code == 400
    assert "1" in resp.json()["detail"]
    assert session.get(ParserAccount, acc.id) is not None


def test_account_delete_succeeds_once_detached(client):
    c, session, _ = client
    acc = _account(session)

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/delete")

    assert resp.status_code == 200
    assert session.get(ParserAccount, acc.id) is None


def test_target_delete_soft_deletes_and_deactivates_links(client):
    c, session, _ = client
    acc = _account(session)
    t = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=True))
    session.commit()

    resp = c.post(f"/api/modules/telegram/targets/{t.id}/delete")

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    link = session.execute(select(TargetAccountLink).where(TargetAccountLink.target_id == t.id)).scalar_one()
    assert link.is_active is False
    session.refresh(t)
    assert t.deleted_at is not None
    assert session.get(Target, t.id) is not None, "history-bearing row is kept"

    # gone from the module list and from the account's list ...
    assert all(r["id"] != t.id for r in c.get("/api/modules/telegram").json()["targets"])
    assert all(r["id"] != t.id for r in c.get(f"/api/modules/telegram/accounts/{acc.id}/targets").json())

    # ... and pasting the same link again revives the same row instead of a duplicate
    row = c.post("/api/modules/telegram/onboard", json={"input": "@c"}).json()
    assert row["id"] == t.id
    session.refresh(t)
    assert t.deleted_at is None and t.is_active is True
    assert any(r["id"] == t.id for r in c.get("/api/modules/telegram").json()["targets"])


def test_account_actions_forbidden_for_non_owner(pg_session):
    owner = User(username="owner-tg", password_hash="x", is_admin=False, is_active=True)
    other = User(username="other-tg", password_hash="x", is_admin=False, is_active=True)
    pg_session.add_all([owner, other])
    pg_session.commit()
    acc = ParserAccount(
        parser_type="telegram", label="o", owner_user_id=owner.id,
        credentials={"api_id": 1, "api_hash": "h", "session_string": "s"},
    )
    pg_session.add(acc)
    pg_session.commit()

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        c = TestClient(app)
        assert c.post(f"/api/modules/telegram/accounts/{acc.id}/disable").status_code == 403
        assert c.post(f"/api/modules/telegram/accounts/{acc.id}/enable").status_code == 403
        assert c.post(f"/api/modules/telegram/accounts/{acc.id}/delete").status_code == 403
    finally:
        app.dependency_overrides.clear()


# --- Re-authorise a dead account (revive its session without losing its channel links) ---

from types import SimpleNamespace  # noqa: E402

from app.services import telegram_qr_login as qr  # noqa: E402


def _dead_account_with_links(session, *, identity_username="alice", identity_phone="380671234567"):
    acc = ParserAccount(
        parser_type="telegram",
        label="dead1",
        pool_mode="dedicated",
        hourly_limit=42,
        alive=False,
        is_active=False,
        dead_reason="Session is not authorized",
        credentials={
            "api_id": 1, "api_hash": "h", "phone": identity_phone, "username": identity_username,
            "session_string": "OLD-DEAD-SESSION", "liveness_failures": 2,
        },
    )
    session.add(acc)
    session.commit()
    targets = [Target(parser_type="telegram", name=f"c{i}", identifier=f"@c{i}", config={}) for i in range(2)]
    session.add_all(targets)
    session.commit()
    links = [TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=(i == 0)) for i, t in enumerate(targets)]
    session.add_all(links)
    session.commit()
    return acc, targets, links


class _ReauthQrClient:
    def __init__(self, username="alice", phone="380671234567", user_id=42):
        self._username, self._phone, self._user_id = username, phone, user_id
        self.session = SimpleNamespace(save=lambda: "NEW-LIVE-SESSION")

    async def connect(self): ...
    async def disconnect(self): ...

    async def qr_login(self):
        return SimpleNamespace(
            url="tg://login?token=X",
            expires=__import__("datetime").datetime.now(__import__("datetime").UTC) + __import__("datetime").timedelta(seconds=30),
            wait=self._wait,
            recreate=self._recreate,
        )

    async def _wait(self, timeout=None):
        return SimpleNamespace(id=1)

    async def _recreate(self): ...
    async def sign_in(self, password=None): ...

    async def get_me(self):
        return SimpleNamespace(username=self._username, phone=self._phone, id=self._user_id)


def test_reauth_qr_updates_existing_row_and_preserves_links(client, monkeypatch):
    c, session, _ = client
    acc, targets, links = _dead_account_with_links(session)
    monkeypatch.setattr(qr, "_default_client_factory", lambda a, b: _ReauthQrClient())

    started = c.post(f"/api/modules/telegram/accounts/{acc.id}/reauth/qr-login/start", json={}).json()
    assert started["status"] == "pending"
    token = started["token"]

    view = None
    for _ in range(50):
        view = c.get(f"/api/modules/telegram/accounts/{acc.id}/reauth/qr-login/{token}").json()
        if view["status"] == "done":
            break
    assert view["status"] == "done"
    assert view["account_id"] == acc.id
    assert "NEW-LIVE-SESSION" not in c.get(f"/api/modules/telegram/accounts/{acc.id}/reauth/qr-login/{token}").text

    session.refresh(acc)
    assert acc.alive is True
    assert acc.is_active is True  # revived accounts return to the pool
    assert acc.dead_reason is None
    assert acc.last_alive_at is not None and acc.last_checked_at is not None
    assert acc.credentials["session_string"] == "NEW-LIVE-SESSION"
    assert acc.credentials["liveness_failures"] == 0
    # untouched: identity, label, pool_mode, hourly_limit, link rows
    assert acc.label == "dead1" and acc.pool_mode == "dedicated" and acc.hourly_limit == 42
    session.refresh(links[0]); session.refresh(links[1])
    assert links[0].is_active is True and links[1].is_active is False
    qr._SESSIONS.clear()


def test_reauth_phone_code_updates_existing_row(client, monkeypatch):
    c, session, _ = client
    acc, _targets, links = _dead_account_with_links(session)
    import app.routers.api as api_mod

    async def fake_send_code(*, api_id, api_hash, phone):
        return "TEMP-SESSION", "code-hash"

    async def fake_verify_code(**kwargs):
        return "NEW-LIVE-SESSION", {"username": "alice", "phone": "380671234567", "user_id": 42}

    monkeypatch.setattr(api_mod, "_telegram_send_code", fake_send_code)
    monkeypatch.setattr(api_mod, "_telegram_verify_code", fake_verify_code)

    start = c.post(f"/api/modules/telegram/accounts/{acc.id}/reauth/start-auth", json={}).json()
    assert start["ok"] is True
    payload = start["auth_payload"]

    resp = c.post(
        f"/api/modules/telegram/accounts/{acc.id}/reauth/complete-auth",
        json={**payload, "code": "12345"},
    )
    assert resp.status_code == 200, resp.text

    session.refresh(acc)
    assert acc.alive is True
    assert acc.is_active is True  # revived accounts return to the pool
    assert acc.dead_reason is None
    assert acc.credentials["session_string"] == "NEW-LIVE-SESSION"
    session.refresh(links[0])
    assert links[0].is_active is True  # link untouched by revival


def test_reauth_rejects_mismatched_telegram_identity(client, monkeypatch):
    c, session, _ = client
    acc, *_ = _dead_account_with_links(session, identity_username="alice", identity_phone="380671234567")
    import app.routers.api as api_mod

    async def fake_send_code(*, api_id, api_hash, phone):
        return "TEMP-SESSION", "code-hash"

    async def fake_verify_code(**kwargs):
        # a different Telegram user signed in through this phone/code flow
        return "SOMEONE-ELSES-SESSION", {"username": "bob", "phone": "380679999999", "user_id": 999}

    monkeypatch.setattr(api_mod, "_telegram_send_code", fake_send_code)
    monkeypatch.setattr(api_mod, "_telegram_verify_code", fake_verify_code)

    start = c.post(f"/api/modules/telegram/accounts/{acc.id}/reauth/start-auth", json={}).json()
    resp = c.post(
        f"/api/modules/telegram/accounts/{acc.id}/reauth/complete-auth",
        json={**start["auth_payload"], "code": "12345"},
    )

    assert resp.status_code == 400
    assert "іншому" in resp.json()["detail"] or "не збігається" in resp.json()["detail"] or "@bob" in resp.json()["detail"]
    session.refresh(acc)
    assert acc.alive is False  # rejected: old dead session untouched
    assert acc.credentials["session_string"] == "OLD-DEAD-SESSION"


def test_reauth_forbidden_for_non_owner(pg_session):
    owner = User(username="owner-tg2", password_hash="x", is_admin=False, is_active=True)
    other = User(username="other-tg2", password_hash="x", is_admin=False, is_active=True)
    pg_session.add_all([owner, other])
    pg_session.commit()
    acc = ParserAccount(
        parser_type="telegram", label="o2", owner_user_id=owner.id,
        credentials={"api_id": 1, "api_hash": "h", "session_string": "s"},
    )
    pg_session.add(acc)
    pg_session.commit()

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        c = TestClient(app)
        assert c.post(f"/api/modules/telegram/accounts/{acc.id}/reauth/start-auth", json={}).status_code == 403
        assert c.post(f"/api/modules/telegram/accounts/{acc.id}/reauth/complete-auth", json={}).status_code == 403
        assert c.post(f"/api/modules/telegram/accounts/{acc.id}/reauth/qr-login/start", json={}).status_code == 403
        assert c.get(f"/api/modules/telegram/accounts/{acc.id}/reauth/qr-login/nope").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_onboard_does_not_touch_another_users_target(pg_session):
    """Identifiers are global: onboarding one must not read or re-queue a foreign row."""
    owner = User(username="owner-onb", password_hash="x", is_admin=False, is_active=True)
    other = User(username="other-onb", password_hash="x", is_admin=False, is_active=True)
    pg_session.add_all([owner, other])
    pg_session.commit()
    target = Target(parser_type="telegram", name="Secret chan", identifier="@secretchan",
                    owner_user_id=owner.id, config={}, onboarding_step="joined")
    pg_session.add(target)
    pg_session.commit()

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        c = TestClient(app)
        assert c.post("/api/modules/telegram/onboard", json={"input": "@secretchan"}).status_code == 403
        assert c.post("/api/modules/telegram/targets/smart-add",
                      json={"entries_text": "@secretchan"}).status_code == 403
    finally:
        app.dependency_overrides.clear()

    pg_session.expire_all()
    target = pg_session.get(Target, target.id)
    assert target.onboarding_step == "joined", "foreign target must not be re-queued"
    assert pg_session.query(ParseJob).count() == 0


def test_media_is_off_by_default_and_toggles_per_target(client):
    c, session, _ = client
    row = c.post("/api/modules/telegram/onboard", json={"input": "@durov"}).json()
    assert row["media_enabled"] is False

    target = session.get(Target, row["id"])
    target.config = {
        **target.config,
        "kind": "channel",
        "quiet_until": "2026-01-01T00:00:00+00:00",
        "backfill": {**target.config["backfill"], "full_progress": {"1": {"next_offset_id": 219, "done": False}}},
    }
    session.commit()

    assert c.post(f"/api/modules/telegram/targets/{row['id']}/update", json={"media_enabled": True}).status_code == 200
    session.refresh(target)
    assert target.config["media_enabled"] is True
    assert target.config["kind"] == "channel", "update must not wipe onboarding-written keys"
    assert target.config["quiet_until"] == "2026-01-01T00:00:00+00:00"
    assert target.config["backfill"]["full_progress"]["1"]["next_offset_id"] == 219, "a settings edit must not restart the backfill"
    assert target.config["backfill"]["enabled"] is True

    rows = c.get("/api/modules/telegram").json()["targets"]
    assert next(r for r in rows if r["id"] == row["id"])["media_enabled"] is True


def test_queued_row_exposes_why_and_when_it_will_retry(client):
    """A row parked on "У черзі" must carry the reason and the next attempt time,
    otherwise the user cannot tell waiting from stuck."""
    import datetime as dt

    c, session, _ = client
    row = c.post("/api/modules/telegram/onboard", json={"input": "@durov"}).json()
    target = session.get(Target, row["id"])
    target.onboarding_error = "Черга на вступ: акаунт «test» вступає не частіше ніж раз на 15 хв"
    session.commit()

    job = session.execute(select(ParseJob).where(ParseJob.job_key == f"onboard:{target.id}")).scalar_one()
    job.status = JobStatus.retry
    job.run_after = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=13)
    session.commit()

    rows = c.get("/api/modules/telegram").json()["targets"]
    fresh = next(r for r in rows if r["id"] == target.id)
    assert fresh["onboarding_retry_at"] is not None
    assert "Черга на вступ" in fresh["onboarding_error"]

    # a target with no pending onboard job reports no retry time
    job.status = JobStatus.succeeded
    session.commit()
    rows = c.get("/api/modules/telegram").json()["targets"]
    assert next(r for r in rows if r["id"] == target.id)["onboarding_retry_at"] is None
