from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from telethon.errors import (
    ChannelsTooMuchError,
    FloodWaitError,
    InviteHashExpiredError,
    UserAlreadyParticipantError,
)

from app.db import Base
from app.models import JobStatus, ParseJob, ParserAccount, Target, TargetAccountLink
from app.plugins.base import DeferJob
from app.services import telegram_onboarding as onb


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _account(session, label, *, pool_mode="shared", alive=None, dialogs=None, health=100.0):
    acc = ParserAccount(
        parser_type="telegram", label=label, pool_mode=pool_mode, alive=alive,
        health_score=health, credentials={"api_id": 1, "api_hash": "h", "session_string": "s",
                                          "dialogs_cache": dialogs or []},
    )
    session.add(acc)
    session.commit()
    return acc


class _FakeEntity:
    def __init__(self, id=123, username="chan", megagroup=False, broadcast=True, title="Chan"):
        self.id = id
        self.username = username
        self.megagroup = megagroup
        self.broadcast = broadcast
        self.title = title


class _FakeClient:
    """Stands in for TelegramClient: records calls, raises on demand."""

    def __init__(self, *, entity=None, raise_on_join=None, invite_chat_id=555):
        self.entity = entity or _FakeEntity()
        self.raise_on_join = raise_on_join
        self.invite_chat_id = invite_chat_id
        self.calls: list[str] = []

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def is_user_authorized(self):
        return True

    async def get_entity(self, ident):
        self.calls.append(f"get_entity:{ident}")
        return self.entity

    async def __call__(self, request):
        name = type(request).__name__
        self.calls.append(name)
        if self.raise_on_join:
            raise self.raise_on_join
        if name == "ImportChatInviteRequest":
            class _Updates:
                chats = [_FakeEntity(id=self.invite_chat_id, username=None, megagroup=True, broadcast=False, title="Priv")]
            return _Updates()
        return None


def _run(coro):
    return asyncio.run(coro)


# ---- onboard(): creates target + job ------------------------------------

def test_onboard_creates_queued_target_and_job():
    session = _session()
    _account(session, "a1")
    target = onb.onboard(session, raw_input="https://t.me/durov", owner_user_id=7)
    session.commit()

    assert target.identifier == "@durov"
    assert target.onboarding_step == "queued"
    assert target.config["live_enabled"] is True
    assert target.config["backfill"]["mode"] == "full"
    job = session.execute(select(ParseJob)).scalar_one()
    assert job.job_key == f"onboard:{target.id}"
    assert job.payload["mode"] == "onboard"
    assert job.account_id is None


def test_onboard_with_explicit_account_pins_it_in_payload():
    session = _session()
    acc = _account(session, "priv", pool_mode="dedicated")
    target = onb.onboard(session, raw_input="@secret", owner_user_id=7, account_id=acc.id, allow_join=False)
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()
    assert job.payload["account_id"] == acc.id
    assert job.payload["allow_join"] is False


def test_onboard_is_idempotent_for_same_identifier():
    session = _session()
    _account(session, "a1")
    t1 = onb.onboard(session, raw_input="@durov", owner_user_id=7)
    session.commit()
    t2 = onb.onboard(session, raw_input="t.me/DUROV", owner_user_id=7)
    session.commit()
    assert t1.id == t2.id
    assert session.query(ParseJob).count() == 1


# ---- pick_account() ------------------------------------------------------

def test_pick_prefers_account_already_in_dialogs():
    session = _session()
    _account(session, "idle", health=100.0)
    member = _account(session, "member", health=60.0,
                      dialogs=[{"identifier": "@durov", "title": "Durov", "username": "durov"}])
    target = Target(parser_type="telegram", name="d", identifier="@durov")
    session.add(target)
    session.commit()

    chosen = onb.pick_account(session, target, now=dt.datetime.now(dt.UTC))
    assert chosen.id == member.id


def test_pick_skips_dedicated_dead_and_full_accounts():
    session = _session()
    _account(session, "dedicated", pool_mode="dedicated")
    _account(session, "dead", alive=False)
    full = _account(session, "full")
    full.credentials = {**full.credentials, "channels_full": True}
    session.commit()
    target = Target(parser_type="telegram", name="d", identifier="@durov")
    session.add(target)
    session.commit()

    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)) is None


# ---- pacing --------------------------------------------------------------

def test_join_pacing_enforces_min_gap_and_daily_limit():
    session = _session()
    acc = _account(session, "a")
    now = dt.datetime.now(dt.UTC)
    assert onb.join_pacing_wait_seconds(acc, now) == 0

    onb.register_join(acc, now)
    assert onb.join_pacing_wait_seconds(acc, now + dt.timedelta(seconds=10)) > 800

    acc.join_window_count = 10
    acc.last_join_at = now - dt.timedelta(hours=2)
    assert onb.join_pacing_wait_seconds(acc, now) > 3600


# ---- run_onboard_job(): the state machine --------------------------------

def _queued(session, identifier="@durov", **onboard_kwargs):
    _account(session, "a1")
    target = onb.onboard(session, raw_input=identifier, owner_user_id=7, **onboard_kwargs)
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()
    job.status = JobStatus.running
    session.commit()
    return target, job


def test_run_joins_public_channel_links_and_sets_quiet_period():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient()

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    session.commit()

    assert "JoinChannelRequest" in client.calls
    assert target.onboarding_step == "joined"
    assert target.onboarding_status.value == "ready"
    assert target.config["kind"] == "channel"
    link = session.execute(select(TargetAccountLink)).scalar_one()
    assert link.is_active is True and link.auto_detected is True
    quiet_until = dt.datetime.fromisoformat(target.config["quiet_until"])
    delta = (quiet_until - dt.datetime.now(dt.UTC)).total_seconds()
    assert 100 < delta <= 300


def test_run_resolves_invite_to_canonical_id():
    session = _session()
    target, job = _queued(session, identifier="https://t.me/+AbC")
    client = _FakeClient(invite_chat_id=555)

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    session.commit()

    assert "ImportChatInviteRequest" in client.calls
    assert target.identifier == "-100555"
    assert target.config["invite_hash"] == "AbC"
    assert target.config["kind"] == "group"


def test_run_skips_join_when_account_already_member():
    session = _session()
    _account(session, "member", dialogs=[{"identifier": "@durov", "title": "D", "username": "durov"}])
    target = onb.onboard(session, raw_input="@durov", owner_user_id=7)
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()
    client = _FakeClient()

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert "JoinChannelRequest" not in client.calls
    assert target.onboarding_step == "joined"


def test_run_floodwait_cools_account_and_defers():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=FloodWaitError(request=None, capture=120))

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.cooldown_until is not None
    assert target.onboarding_step == "joining"


def test_run_channels_too_much_marks_full_and_defers():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=ChannelsTooMuchError(request=None))

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.credentials.get("channels_full") is True


def test_run_already_participant_is_success():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=UserAlreadyParticipantError(request=None))

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    assert target.onboarding_step == "joined"


def test_run_expired_invite_fails_target_without_punishing_account():
    session = _session()
    target, job = _queued(session, identifier="t.me/+Old")
    client = _FakeClient(raise_on_join=InviteHashExpiredError(request=None))

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert target.onboarding_step == "failed"
    assert "expired" in (target.onboarding_error or "").lower() or "InviteHashExpired" in (target.onboarding_error or "")
    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.cooldown_until is None


def test_run_no_candidates_fails_with_needs_account():
    session = _session()
    target = onb.onboard(session, raw_input="@durov", owner_user_id=7)  # no accounts at all
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: _FakeClient())

    assert target.onboarding_step == "failed"
    assert target.onboarding_status.value == "needs_account"


def test_run_pacing_defers_before_touching_telegram():
    session = _session()
    target, job = _queued(session)
    acc = session.execute(select(ParserAccount)).scalar_one()
    onb.register_join(acc, dt.datetime.now(dt.UTC))
    session.commit()
    client = _FakeClient()

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert client.calls == []
