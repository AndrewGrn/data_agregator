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
    InviteRequestSentError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
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


# ---- integration: _process_job + session.commit() ------------------------
# The unit tests above read the identity map, so a write that only explodes at
# flush time (or that a rollback silently throws away) looks fine to them.


def _process(session, job, client, monkeypatch):
    """Run the job the way the worker does: through the plugin, then commit."""
    from app.plugins import telegram as tg
    from app.services import worker as worker_mod

    monkeypatch.setattr(
        tg, "run_onboard_job",
        lambda s, j, t: onb.run_onboard_job(s, j, t, client_factory=lambda acc: client),
    )
    job.status = JobStatus.running
    session.commit()
    worker_mod._process_job(session, job)
    worker_mod._commit_job(session, int(job.id))


def test_relink_to_previously_used_account_commits(monkeypatch):
    """uq_target_account(target_id, account_id) outlives is_active=False."""
    session = _session()
    target, job = _queued(session)
    acc = session.execute(select(ParserAccount)).scalar_one()
    session.add(TargetAccountLink(target_id=target.id, account_id=acc.id, is_active=False, auto_detected=True))
    session.commit()

    _process(session, job, _FakeClient(), monkeypatch)

    links = session.execute(select(TargetAccountLink)).scalars().all()
    assert len(links) == 1 and links[0].is_active is True
    assert session.get(ParseJob, job.id).status == JobStatus.succeeded
    assert session.get(Target, target.id).onboarding_step == "joined"


def test_floodwait_cooldown_survives_the_defer_commit(monkeypatch):
    session = _session()
    target, job = _queued(session)

    _process(session, job, _FakeClient(raise_on_join=FloodWaitError(request=None, capture=120)), monkeypatch)

    acc = session.get(ParserAccount, session.execute(select(ParserAccount.id)).scalar_one())
    assert acc.cooldown_until is not None, "the flooded account must stay cooled after the defer"
    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)) is None
    assert session.get(ParseJob, job.id).status == JobStatus.retry


def test_peer_flood_cooldown_survives_the_defer_commit(monkeypatch):
    """Same shape as the FloodWait case: run through _process_job + commit, not
    run_onboard_job directly, since that's what actually persists the cooldown."""
    session = _session()
    target, job = _queued(session)

    _process(session, job, _FakeClient(raise_on_join=PeerFloodError(request=None)), monkeypatch)

    acc = session.get(ParserAccount, session.execute(select(ParserAccount.id)).scalar_one())
    assert acc.cooldown_until is not None, "the peer-flooded account must stay cooled after the defer"
    assert (onb._aware(acc.cooldown_until) - dt.datetime.now(dt.UTC)) > dt.timedelta(hours=1), \
        "PeerFlood must cool down far longer than a plain FloodWait"
    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)) is None
    assert session.get(ParseJob, job.id).status == JobStatus.retry


def test_channels_full_survives_the_defer_commit(monkeypatch):
    session = _session()
    target, job = _queued(session)

    _process(session, job, _FakeClient(raise_on_join=ChannelsTooMuchError(request=None)), monkeypatch)

    acc = session.get(ParserAccount, session.execute(select(ParserAccount.id)).scalar_one())
    assert acc.credentials.get("channels_full") is True


def test_terminal_failure_surfaces_on_the_target(monkeypatch):
    """A job that exhausts its attempts must not leave the row reading 'queued'."""
    session = _session()
    target, job = _queued(session)
    job.attempt = job.max_attempts
    session.commit()

    _process(session, job, _FakeClient(raise_on_join=RuntimeError("boom")), monkeypatch)

    target = session.get(Target, target.id)
    assert session.get(ParseJob, job.id).status == JobStatus.failed
    assert target.onboarding_step == "failed"
    assert "boom" in (target.onboarding_error or "")


def test_peer_flood_cools_the_account():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=PeerFloodError(request=None))

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.cooldown_until is not None
    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)) is None


def test_invite_request_sent_is_not_retried():
    session = _session()
    target, job = _queued(session, identifier="t.me/+Wait")
    client = _FakeClient(raise_on_join=InviteRequestSentError(request=None))

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert target.onboarding_step == "pending_approval"
    assert target.onboarding_status.value == "blocked"
    assert target.onboarding_error


def test_unresolvable_username_is_terminal():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=UsernameInvalidError(request=None))

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert target.onboarding_step == "failed"
    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.cooldown_until is None


def test_unauthorized_session_is_not_a_terminal_target_error():
    """RuntimeError, not ValueError: the account is broken, the target is fine."""
    session = _session()
    target, job = _queued(session)
    client = _FakeClient()
    client.is_user_authorized = lambda: _false()

    with pytest.raises(RuntimeError):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert target.onboarding_step != "failed"


async def _false():
    return False


def test_client_is_constructed_inside_the_event_loop():
    """Regression: the worker runs jobs on a thread that has no event loop.

    Telethon's TelegramClient binds to the running loop in its constructor, so
    building it in sync code killed every real onboard job with "There is no
    current event loop in thread 'asyncio_0'" before a request was ever sent.
    asyncio.get_running_loop() raises in plain sync code and succeeds inside
    asyncio.run(), so this pins the factory to the latter.
    """
    session = _session()
    target, job = _queued(session)
    built_with_loop: list[bool] = []

    def factory(account):
        try:
            asyncio.get_running_loop()
            built_with_loop.append(True)
        except RuntimeError:
            built_with_loop.append(False)
        return _FakeClient()

    onb.run_onboard_job(session, job, target, client_factory=factory)
    session.commit()

    assert built_with_loop == [True]
    assert target.onboarding_step == "joined"


def test_run_defers_while_pool_is_only_cooling_down():
    """A cooldown is a pause, not an absence: the job must come back, not fail."""
    session = _session()
    target, job = _queued(session)
    acc = session.execute(select(ParserAccount)).scalar_one()
    acc.cooldown_until = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
    session.commit()
    client = _FakeClient()

    with pytest.raises(DeferJob) as info:
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert 500 < info.value.seconds <= 601
    assert client.calls == []
    assert target.onboarding_step == "queued"
    assert target.onboarding_status.value == "needs_account"


def test_private_link_nobody_is_in_fails_with_a_human_message():
    """t.me/c/<id>/<msg> is a private chat: there is nothing to join by, so an
    unresolvable id must say 'pick an account that is already a member' instead
    of leaking Telethon's 'Cannot find any entity corresponding to ...'."""
    session = _session()
    target, job = _queued(session, identifier="https://t.me/c/2707984934/1393921")
    assert target.identifier == "-1002707984934"

    class _Outsider(_FakeClient):
        async def iter_dialogs(self):
            return
            yield  # noqa: unreachable - makes this an async generator with no dialogs

        async def get_entity(self, ident):
            raise ValueError(f'Cannot find any entity corresponding to "{ident}"')

    client = _Outsider()
    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    session.commit()

    assert client.calls == [], "nobody is inside: do not spend a request finding out"
    assert target.onboarding_step == "failed"
    assert target.onboarding_status.value == "needs_account"
    assert target.onboarding_error.startswith("Приватний чат")
    assert "Cannot find any entity" not in target.onboarding_error


def test_private_id_resolves_after_walking_dialogs_for_a_member_account():
    """A dedicated account that IS inside the private chat must succeed even
    when its session has not met the chat yet (Telethon raises ValueError on a
    bare id until the dialogs were listed once)."""
    session = _session()
    target, job = _queued(session, identifier="https://t.me/c/2707984934/1393921")
    # the pool account is inside the chat; only its Telethon session has not met it yet
    acc = session.execute(select(ParserAccount)).scalar_one()
    acc.credentials = {**acc.credentials,
                       "dialogs_cache": [{"identifier": "-1002707984934", "dialog_id": "-1002707984934", "title": "Private"}]}
    session.commit()

    class _Member(_FakeClient):
        def __init__(self):
            super().__init__(entity=_FakeEntity(id=2707984934, username=None, megagroup=True, broadcast=False, title="Private"))
            self.dialogs_walked = False

        async def iter_dialogs(self):
            self.dialogs_walked = True
            yield object()

        async def get_entity(self, ident):
            self.calls.append(f"get_entity:{ident}")
            if not self.dialogs_walked:
                raise ValueError("Could not find the input entity")
            return self.entity

    client = _Member()
    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    session.commit()

    assert client.dialogs_walked
    assert client.calls.count("get_entity:-1002707984934") == 2
    assert "JoinChannelRequest" not in client.calls, "already a member: resolve, never join"
    assert target.onboarding_step == "joined"
    assert target.identifier == "-1002707984934"
    assert target.name == "Private"


def test_low_health_account_is_still_usable_once_its_cooldown_expires():
    """Regression: health_score only grows in _register_account_success(), which needs a
    job to run, which needed the account to pass this very check. A run of network blips
    (-8 each) drove a live account to 4 and every target sat on "У черзі" forever."""
    session = _session()
    acc = _account(session, "battered", health=4.0)
    acc.cooldown_until = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)  # served
    session.commit()
    target = Target(parser_type="telegram", name="d", identifier="@durov")
    session.add(target)
    session.commit()

    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)).id == acc.id


def test_cooldown_still_blocks_and_is_reported_as_a_wait():
    session = _session()
    acc = _account(session, "cooling", health=100.0)
    acc.cooldown_until = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    session.commit()
    target = Target(parser_type="telegram", name="d", identifier="@durov")
    session.add(target)
    session.commit()
    now = dt.datetime.now(dt.UTC)

    assert onb.pick_account(session, target, now=now) is None
    assert 250 < onb.pool_retry_wait_seconds(session, now=now) <= 301


def test_dead_account_is_still_excluded_regardless_of_health():
    """alive=False remains the kill switch."""
    session = _session()
    _account(session, "dead", alive=False, health=100.0)
    target = Target(parser_type="telegram", name="d", identifier="@durov")
    session.add(target)
    session.commit()

    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)) is None


def test_private_chat_fails_immediately_without_burning_a_join_slot():
    """Regression: six private channels whose only member account had died sat on
    'У черзі' for a day. Each retry waited 15 min for a join slot, then tried a join
    that CANNOT work (a bare -100 id has no username and no invite), so the pool's
    10 daily joins were spent on guaranteed failures."""
    session = _session()
    target, job = _queued(session, identifier="-1001338286165")
    acc = session.execute(select(ParserAccount)).scalar_one()
    acc.last_join_at = dt.datetime.now(dt.UTC)  # a join slot would otherwise be due in 15 min
    session.commit()
    client = _FakeClient()

    onb.run_onboard_job(session, job, target, client_factory=lambda a: client)
    session.commit()

    assert client.calls == [], "must not touch Telegram at all"
    assert target.onboarding_step == "failed"
    assert target.onboarding_error.startswith("Приватний чат")
    session.refresh(acc)
    assert acc.join_window_count == 0, "no join slot consumed"


def test_private_chat_picks_the_member_account_even_if_busier():
    session = _session()
    _account(session, "outsider", health=100.0)
    member = _account(session, "insider", health=10.0,
                      dialogs=[{"identifier": "-1001338286165", "dialog_id": "-1001338286165", "title": "ЖК"}])
    target = Target(parser_type="telegram", name="ЖК", identifier="-1001338286165")
    session.add(target)
    session.commit()

    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)).id == member.id


def test_public_channel_is_unaffected_by_the_private_rule():
    session = _session()
    target, job = _queued(session, identifier="@durov")
    client = _FakeClient()

    onb.run_onboard_job(session, job, target, client_factory=lambda a: client)
    session.commit()

    assert "JoinChannelRequest" in client.calls
    assert target.onboarding_step == "joined"


def test_join_pacing_wait_is_explained_in_the_row():
    """A deferral must leave a human reason where the UI shows it."""
    session = _session()
    target, job = _queued(session)
    acc = session.execute(select(ParserAccount)).scalar_one()
    acc.last_join_at = dt.datetime.now(dt.UTC)
    session.commit()

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda a: _FakeClient())

    assert "Черга на вступ" in target.onboarding_error
    assert "хв" in target.onboarding_error
