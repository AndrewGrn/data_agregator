from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TelegramMembership, TelegramMembershipHistory, TelegramUser


def _coerce_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _parse_iso(value: str | None) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    return _coerce_utc(parsed)


def _clean_username(value: Any) -> str | None:
    username = str(value or "").strip().lower()
    if not username:
        return None
    return username.removeprefix("@")


def _json_safe(value: Any):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sender_tg_id(sender: dict) -> int | None:
    raw = sender.get("id")
    try:
        if raw is None:
            return None
        return int(raw)
    except Exception:
        return None


def _upsert_user(
    session: Session,
    sender: dict,
    observed_at: dt.datetime,
    raw_payload: dict | None = None,
) -> TelegramUser | None:
    tg_user_id = _sender_tg_id(sender)
    if tg_user_id is None:
        return None

    user = session.execute(select(TelegramUser).where(TelegramUser.telegram_user_id == tg_user_id)).scalar_one_or_none()
    is_new = user is None
    if user is None:
        user = TelegramUser(
            telegram_user_id=tg_user_id,
            username=_clean_username(sender.get("username")),
            first_name=(str(sender.get("first_name")) if sender.get("first_name") is not None else None),
            last_name=(str(sender.get("last_name")) if sender.get("last_name") is not None else None),
            phone=(str(sender.get("phone")) if sender.get("phone") is not None else None),
            is_bot=bool(sender.get("is_bot", False)),
            is_verified=bool(sender.get("is_verified", False)),
            is_scam=bool(sender.get("is_scam", False)),
            is_fake=bool(sender.get("is_fake", False)),
            is_deleted=bool(sender.get("is_deleted", False)),
            last_seen_at=observed_at,
            raw=_json_safe(raw_payload) if isinstance(raw_payload, dict) else None,
        )
        session.add(user)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            user = session.execute(select(TelegramUser).where(TelegramUser.telegram_user_id == tg_user_id)).scalar_one_or_none()
            is_new = False

    if user is None:
        return None

    username = _clean_username(sender.get("username"))
    if username:
        user.username = username
    if sender.get("first_name") is not None:
        user.first_name = str(sender.get("first_name"))
    if sender.get("last_name") is not None:
        user.last_name = str(sender.get("last_name"))
    if sender.get("phone") is not None:
        user.phone = str(sender.get("phone"))
    if sender.get("is_bot") is not None:
        user.is_bot = bool(sender.get("is_bot"))
    if sender.get("is_verified") is not None:
        user.is_verified = bool(sender.get("is_verified"))
    if sender.get("is_scam") is not None:
        user.is_scam = bool(sender.get("is_scam"))
    if sender.get("is_fake") is not None:
        user.is_fake = bool(sender.get("is_fake"))
    if sender.get("is_deleted") is not None:
        user.is_deleted = bool(sender.get("is_deleted"))
    user_last_seen = _coerce_utc(user.last_seen_at)
    if not user_last_seen or observed_at > user_last_seen:
        user.last_seen_at = observed_at
    if raw_payload:
        user.raw = _json_safe(raw_payload)

    if is_new:
        # Ensures membership inserts can use user.id in this tx.
        session.flush()
    return user


def _resolve_membership_state(payload: dict, observed_at: dt.datetime) -> tuple[str, bool, str, dt.datetime | None]:
    event_type = str(payload.get("event_type") or "")
    if event_type == "telegram_participant":
        status = str(payload.get("membership_status") or "participant_unknown")
        is_active = bool(payload.get("membership_is_active", True))
        source = "participants_sync"
        joined_at = _parse_iso(payload.get("joined_at"))
        return status, is_active, source, joined_at

    source_hint = str(payload.get("source") or "")
    source = "listener" if source_hint == "telegram_listener" else "polling"
    return "message_observed", True, source, None


def upsert_telegram_profile_from_event(
    session: Session,
    target_id: int,
    account_id: int | None,
    payload: dict,
    observed_at: dt.datetime | None = None,
) -> None:
    if account_id is None:
        return
    if not isinstance(payload, dict):
        return

    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return

    event_observed_at = _coerce_utc(observed_at) or _parse_iso(payload.get("date")) or dt.datetime.now(dt.UTC)
    user = _upsert_user(
        session,
        sender,
        event_observed_at,
        raw_payload=payload.get("raw") if isinstance(payload.get("raw"), dict) else None,
    )
    if not user:
        return

    status, is_active, source, joined_at = _resolve_membership_state(payload, event_observed_at)
    membership = session.execute(
        select(TelegramMembership).where(
            TelegramMembership.target_id == int(target_id),
            TelegramMembership.account_id == int(account_id),
            TelegramMembership.telegram_user_ref_id == user.id,
        )
    ).scalar_one_or_none()

    is_new = membership is None
    if membership is None:
        membership = TelegramMembership(
            target_id=int(target_id),
            account_id=int(account_id),
            telegram_user_ref_id=user.id,
            membership_status=status,
            is_active=is_active,
            first_seen_at=event_observed_at,
            last_seen_at=event_observed_at,
            joined_at=joined_at,
            last_source=source,
            last_raw=_json_safe(payload.get("raw")) if isinstance(payload.get("raw"), dict) else None,
        )
        session.add(membership)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            membership = session.execute(
                select(TelegramMembership).where(
                    TelegramMembership.target_id == int(target_id),
                    TelegramMembership.account_id == int(account_id),
                    TelegramMembership.telegram_user_ref_id == user.id,
                )
            ).scalar_one_or_none()
            is_new = False

    if membership is None:
        return

    status_changed = False
    active_changed = False
    joined_changed = False

    membership_last_seen = _coerce_utc(membership.last_seen_at)
    if not membership_last_seen or event_observed_at > membership_last_seen:
        membership.last_seen_at = event_observed_at

    if str(payload.get("event_type") or "") == "telegram_participant":
        if membership.membership_status != status:
            membership.membership_status = status
            status_changed = True
        if bool(membership.is_active) != bool(is_active):
            membership.is_active = bool(is_active)
            active_changed = True
        if joined_at and membership.joined_at != joined_at:
            membership.joined_at = joined_at
            joined_changed = True
    elif not membership.membership_status:
        membership.membership_status = status
        status_changed = True

    membership.last_source = source
    if isinstance(payload.get("raw"), dict):
        membership.last_raw = _json_safe(payload.get("raw"))

    if is_new or status_changed or active_changed or joined_changed:
        session.add(
            TelegramMembershipHistory(
                membership_id=membership.id,
                observed_at=event_observed_at,
                membership_status=membership.membership_status,
                is_active=bool(membership.is_active),
                source=source,
                snapshot={
                    "target_id": int(target_id),
                    "account_id": int(account_id),
                    "telegram_user_id": int(user.telegram_user_id),
                    "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
                },
            )
        )
