from __future__ import annotations

import datetime as dt
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from app.config import get_settings
from app.models import (
    JobStatus,
    OnboardingStatus,
    ParseJob,
    ParserAccount,
    Target,
    TargetAccountLink,
)
from app.plugins.base import DeferJob, JobSpec
from app.services.scheduler import _enqueue_job_specs
from app.services.telegram_accounts import (
    compute_account_load_score,
    find_dialog_match,
    invite_hash,
    is_invite_identifier,
    normalize_telegram_identifier,
)

logger = logging.getLogger(__name__)
settings = get_settings()

QUEUE_ONBOARD = "telegram_backfill"
_TERMINAL_JOIN_ERRORS = (
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
    # Telethon's get_entity raises a bare ValueError for a username it cannot
    # resolve; retrying that 20x never helps. Account-level problems must not
    # raise ValueError inside the join block or they land here by mistake.
    ValueError,
)
# PeerFlood carries no retry-after, so pick a window long enough to matter.
_PEER_FLOOD_COOLDOWN_SECONDS = 24 * 3600


@dataclass(slots=True)
class JoinOutcome:
    identifier: str
    kind: str
    title: str | None
    joined: bool


def default_onboard_config() -> dict[str, Any]:
    """Defaults for a channel the user only gave us a link for."""
    return {
        "limit": int(settings.telegram_fetch_limit),
        "poll_interval_seconds": 300,
        "live_enabled": True,
        "gapfill_limit": int(settings.telegram_fetch_limit),
        "participants_sync_enabled": True,
        "participants_sync_interval_seconds": 3600,
        "participants_limit": 1000,
        "comments_enabled": True,
        "gapfill_comments_enabled": True,
        "backfill": {
            "enabled": True,
            "mode": "full",
            "limit": 200,
            "full_batch_size": 300,
            "comments_enabled": True,
        },
    }


# --------------------------------------------------------------------------
# Entry point from the API: create the target row and queue the work.
# --------------------------------------------------------------------------

def onboard(
    session: Session,
    *,
    raw_input: str,
    owner_user_id: int | None,
    account_id: int | None = None,
    allow_join: bool = True,
    acting_user_id: int | None = None,
    acting_is_admin: bool = False,
) -> Target:
    identifier = normalize_telegram_identifier(raw_input)
    if not identifier:
        raise ValueError("Порожній ідентифікатор каналу")

    target = session.execute(
        select(Target).where(Target.parser_type == "telegram", Target.identifier == identifier)
    ).scalar_one_or_none()
    # Identifiers are global, so an existing row may belong to somebody else:
    # without this check the caller would read and re-queue a foreign target.
    if (
        target is not None
        and acting_user_id is not None
        and not acting_is_admin
        and (target.owner_user_id is None or int(target.owner_user_id) != int(acting_user_id))
    ):
        raise PermissionError("Ця ціль належить іншому користувачу")
    if target is None:
        target = Target(
            parser_type="telegram",
            name=raw_input.strip(),
            identifier=identifier,
            owner_user_id=owner_user_id,
            config=default_onboard_config(),
            is_active=True,
        )
        session.add(target)
        session.flush()

    target.is_active = True
    target.onboarding_step = "queued"
    target.onboarding_error = None
    target.onboarding_status = OnboardingStatus.needs_account

    payload: dict[str, Any] = {"mode": "onboard", "allow_join": bool(allow_join)}
    if account_id is not None:
        payload["account_id"] = int(account_id)

    _enqueue_job_specs(
        session,
        target,
        [
            JobSpec(
                parser_type="telegram",
                target_id=target.id,
                account_id=None,
                job_key=f"onboard:{target.id}",
                payload=payload,
                priority=50,
                queue=QUEUE_ONBOARD,
                max_attempts=20,
            )
        ],
    )
    logger.info("onboard queued target=%s identifier=%s account=%s", target.id, identifier, account_id)
    return target


# --------------------------------------------------------------------------
# Account choice and join pacing.
# --------------------------------------------------------------------------

def _is_member(account: ParserAccount, identifier: str) -> bool:
    dialogs = (account.credentials or {}).get("dialogs_cache") or []
    return find_dialog_match(identifier, dialogs) is not None


def _queued_jobs(session: Session, account_id: int) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(ParseJob).where(
                ParseJob.parser_type == "telegram",
                ParseJob.account_id == account_id,
                ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
            )
        )
        or 0
    )


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite drops tzinfo on DateTime(timezone=True) round-trips; Postgres keeps it."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _available(account: ParserAccount, now: dt.datetime) -> bool:
    if account.alive is False:
        return False
    if (account.credentials or {}).get("channels_full"):
        return False
    cooldown_until = _aware(account.cooldown_until)
    if cooldown_until and cooldown_until > now:
        return False
    if account.health_score < 20:
        return False
    return True


def pick_account(session: Session, target: Target, *, now: dt.datetime) -> ParserAccount | None:
    """Prefer an account that is already a member; otherwise the least loaded."""
    accounts = session.execute(
        select(ParserAccount).where(
            ParserAccount.parser_type == "telegram",
            ParserAccount.is_active.is_(True),
            ParserAccount.pool_mode == "shared",
        )
    ).scalars().all()
    candidates = [a for a in accounts if _available(a, now)]
    if not candidates:
        return None

    def key(a: ParserAccount) -> tuple[int, float]:
        member = 0 if _is_member(a, target.identifier) else 1
        return (member, compute_account_load_score(a, queued_jobs=_queued_jobs(session, a.id), now=now))

    return sorted(candidates, key=key)[0]


def _refresh_join_window(account: ParserAccount, now: dt.datetime) -> None:
    window_start = _aware(account.join_window_start)
    if not window_start or (now - window_start) >= dt.timedelta(days=1):
        account.join_window_start = now
        account.join_window_count = 0


def join_pacing_wait_seconds(account: ParserAccount, now: dt.datetime) -> int:
    """Seconds until this account may join another chat; 0 if it may now."""
    _refresh_join_window(account, now)
    min_gap = int(settings.telegram_join_min_gap_seconds)
    daily = int(settings.telegram_join_daily_limit)

    wait = 0
    last_join_at = _aware(account.last_join_at)
    if last_join_at:
        since = (now - last_join_at).total_seconds()
        if since < min_gap:
            wait = max(wait, int(min_gap - since))
    window_start = _aware(account.join_window_start)
    if account.join_window_count >= daily and window_start:
        until_reset = (window_start + dt.timedelta(days=1) - now).total_seconds()
        wait = max(wait, int(until_reset))
    return max(wait, 0)


def lock_account_for_pacing(session: Session, account: ParserAccount) -> None:
    """Serialize the pacing check across workers by locking the account row.

    Onboard jobs carry account_id=None, so the worker's per-account parallelism
    cap never applies to them: without this two workers read the same
    last_join_at snapshot and both join. The lock is held until the job's
    commit, i.e. across the join call itself — which is exactly the window that
    must not overlap. No-op on SQLite, which has no row locks anyway.
    """
    if session.get_bind().dialect.name in {"postgresql", "mysql"}:
        session.refresh(account, with_for_update=True)


def register_join(account: ParserAccount, now: dt.datetime) -> None:
    _refresh_join_window(account, now)
    account.join_window_count += 1
    account.last_join_at = now


# --------------------------------------------------------------------------
# Telethon side.
# --------------------------------------------------------------------------

def default_client_factory(account: ParserAccount) -> TelegramClient:
    creds = account.credentials or {}
    return TelegramClient(StringSession(str(creds["session_string"])), int(creds["api_id"]), str(creds["api_hash"]))


def _kind_of(entity: Any) -> str:
    if getattr(entity, "megagroup", False):
        return "group"
    if getattr(entity, "broadcast", False):
        return "channel"
    if type(entity).__name__ == "Chat":
        return "group"
    return "private"


def _canonical(entity: Any) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"@{str(username).lower()}"
    return f"-100{int(entity.id)}"


async def join_channel(client: Any, identifier: str) -> JoinOutcome:
    """Join by @username/id or by invite hash. Raises Telethon errors as-is."""
    if is_invite_identifier(identifier):
        updates = await client(ImportChatInviteRequest(invite_hash(identifier)))
        chat = updates.chats[0]
        return JoinOutcome(identifier=_canonical(chat), kind=_kind_of(chat), title=getattr(chat, "title", None), joined=True)

    entity = await client.get_entity(identifier.lstrip("@") if identifier.startswith("@") else identifier)
    await client(JoinChannelRequest(entity))
    return JoinOutcome(identifier=_canonical(entity), kind=_kind_of(entity), title=getattr(entity, "title", None), joined=True)


async def _resolve_only(client: Any, identifier: str) -> JoinOutcome:
    entity = await client.get_entity(identifier.lstrip("@") if identifier.startswith("@") else identifier)
    return JoinOutcome(identifier=_canonical(entity), kind=_kind_of(entity), title=getattr(entity, "title", None), joined=False)


async def _with_client(client: Any, coro_factory):
    await client.connect()
    try:
        if not await client.is_user_authorized():
            # Not a ValueError: that is the terminal "bad identifier" signal.
            raise RuntimeError("account session is not authorized")
        return await coro_factory()
    finally:
        await client.disconnect()


# --------------------------------------------------------------------------
# The job body: called by TelegramPlugin.run for payload.mode == "onboard".
# --------------------------------------------------------------------------

def _fail(target: Target, reason: str, *, status: OnboardingStatus = OnboardingStatus.needs_account) -> None:
    target.onboarding_step = "failed"
    target.onboarding_error = reason[:2000]
    target.onboarding_status = status
    logger.warning("onboard failed target=%s: %s", target.id, reason)


def _link(session: Session, target: Target, account: ParserAccount) -> None:
    for link in session.execute(
        select(TargetAccountLink).where(TargetAccountLink.target_id == target.id, TargetAccountLink.is_active.is_(True))
    ).scalars():
        link.is_active = False
    session.flush()
    # uq_target_account(target_id, account_id) survives deactivation, so a
    # re-link to a previously used account must revive that row, not insert.
    existing = session.execute(
        select(TargetAccountLink).where(
            TargetAccountLink.target_id == target.id,
            TargetAccountLink.account_id == account.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.is_active = True
        existing.auto_detected = True
        existing.owner_user_id = target.owner_user_id
        return
    session.add(
        TargetAccountLink(
            target_id=target.id,
            account_id=account.id,
            owner_user_id=target.owner_user_id,
            is_active=True,
            auto_detected=True,
        )
    )


def run_onboard_job(
    session: Session,
    job: ParseJob,
    target: Target,
    *,
    client_factory: Callable[[ParserAccount], Any] = default_client_factory,
) -> None:
    import asyncio

    now = dt.datetime.now(dt.UTC)
    payload = dict(job.payload or {})
    allow_join = bool(payload.get("allow_join", True))
    pinned_id = payload.get("account_id")

    target.onboarding_step = "resolving"

    if pinned_id is not None:
        account = session.get(ParserAccount, int(pinned_id))
        if account is None or not account.is_active:
            _fail(target, "призначений акаунт не знайдено або вимкнено")
            return
    else:
        account = pick_account(session, target, now=now)
        if account is None:
            _fail(target, "немає доступних акаунтів пулу")
            return

    already_member = _is_member(account, target.identifier)
    needs_join = not already_member

    if needs_join and not allow_join:
        _fail(target, "акаунт не є учасником, а вступ заборонено")
        return

    if needs_join:
        lock_account_for_pacing(session, account)
        wait = join_pacing_wait_seconds(account, now)
        if wait > 0:
            raise DeferJob(seconds=wait, reason=f"join pacing on account #{account.id}")

    target.onboarding_step = "joining" if needs_join else "resolving"
    client = client_factory(account)

    try:
        if needs_join:
            outcome = asyncio.run(_with_client(client, lambda: join_channel(client, target.identifier)))
            register_join(account, now)
        else:
            outcome = asyncio.run(_with_client(client, lambda: _resolve_only(client, target.identifier)))
    except FloodWaitError as exc:
        seconds = int(getattr(exc, "seconds", 60) or 60)
        account.cooldown_until = now + dt.timedelta(seconds=seconds)
        logger.warning("onboard floodwait account=%s seconds=%s", account.id, seconds)
        raise DeferJob(seconds=min(seconds, 3600), reason=f"FloodWait {seconds}s on account #{account.id}")
    except PeerFloodError:
        # Telegram's hardest "you are spamming" signal; it carries no wait hint.
        account.cooldown_until = now + dt.timedelta(seconds=_PEER_FLOOD_COOLDOWN_SECONDS)
        logger.warning("onboard peerflood account=%s cooled for %ss", account.id, _PEER_FLOOD_COOLDOWN_SECONDS)
        raise DeferJob(seconds=3600, reason=f"PeerFlood on account #{account.id}")
    except ChannelsTooMuchError:
        account.credentials = {**(account.credentials or {}), "channels_full": True}
        logger.warning("onboard account=%s is full (ChannelsTooMuch)", account.id)
        raise DeferJob(seconds=30, reason=f"account #{account.id} has too many channels")
    except InviteRequestSentError:
        # The join request went through; only a chat admin can move this on.
        register_join(account, now)
        target.onboarding_step = "pending_approval"
        target.onboarding_error = "Заявку на вступ надіслано, очікує підтвердження адміністратора каналу"
        target.onboarding_status = OnboardingStatus.blocked
        logger.info("onboard join request pending target=%s account=%s", target.id, account.id)
        return
    except UserAlreadyParticipantError:
        outcome = asyncio.run(_with_client(client, lambda: _resolve_only(client, target.identifier)))
    except _TERMINAL_JOIN_ERRORS as exc:
        _fail(target, f"{type(exc).__name__}: {exc}")
        return

    # Canonical identity: invites become -100<id>, usernames stay @lower.
    if is_invite_identifier(target.identifier):
        config = dict(target.config or {})
        config["invite_hash"] = invite_hash(target.identifier)
        target.config = config
    target.identifier = outcome.identifier
    if not target.name or target.name.startswith(("http", "t.me", "@", "invite:")):
        target.name = outcome.title or outcome.identifier

    config = dict(target.config or {})
    config["kind"] = outcome.kind
    quiet_until = now + dt.timedelta(seconds=random.randint(120, 300))
    config["quiet_until"] = quiet_until.isoformat()
    target.config = config

    _link(session, target, account)
    target.onboarding_step = "joined"
    target.onboarding_error = None
    target.onboarding_status = OnboardingStatus.ready
    logger.info("onboard joined target=%s identifier=%s account=%s kind=%s", target.id, target.identifier, account.id, outcome.kind)
