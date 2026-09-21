from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DarknetUser, DarknetUserMembership


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
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except Exception:
        return None
    return _coerce_utc(parsed)


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


def _clean_username(value: Any) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    cleaned = raw.removeprefix("@").strip()
    if not cleaned:
        return None
    return cleaned, cleaned.lower()


def _extract_forum_host(thread_url: str, target_identifier: str) -> str:
    thread_parsed = urlparse(thread_url or "")
    if thread_parsed.netloc:
        return str(thread_parsed.netloc).strip().lower()
    target_parsed = urlparse(target_identifier or "")
    if target_parsed.netloc:
        return str(target_parsed.netloc).strip().lower()
    return "unknown"


def _extract_event_username(payload: dict) -> tuple[str, str] | None:
    event_type = str(payload.get("event_type") or "").strip()
    if event_type == "forum_user":
        user = payload.get("user")
        if isinstance(user, dict):
            return _clean_username(user.get("username"))
        return None
    if event_type == "forum_post":
        return _clean_username(payload.get("author"))
    return None


def _extract_event_time(payload: dict, observed_at: dt.datetime | None) -> dt.datetime:
    return (
        _coerce_utc(observed_at)
        or _parse_iso(str(payload.get("posted_at") or ""))
        or _parse_iso(str(payload.get("date") or ""))
        or dt.datetime.now(dt.UTC)
    )


def _upsert_darknet_user(
    session: Session,
    forum_host: str,
    username: str,
    username_normalized: str,
    observed_at: dt.datetime,
    payload: dict,
) -> DarknetUser | None:
    row = session.execute(
        select(DarknetUser).where(
            DarknetUser.forum_host == forum_host,
            DarknetUser.username_normalized == username_normalized,
        )
    ).scalar_one_or_none()

    is_new = row is None
    if row is None:
        row = DarknetUser(
            forum_host=forum_host,
            username=username,
            username_normalized=username_normalized,
            display_name=username,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        session.add(row)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            row = session.execute(
                select(DarknetUser).where(
                    DarknetUser.forum_host == forum_host,
                    DarknetUser.username_normalized == username_normalized,
                )
            ).scalar_one_or_none()
            is_new = False

    if row is None:
        return None

    row.username = username
    current_display_name = str(row.display_name or "").strip()
    if not current_display_name or current_display_name == f"@{username}":
        row.display_name = username
    row_last_seen = _coerce_utc(row.last_seen_at)
    if not row_last_seen or observed_at > row_last_seen:
        row.last_seen_at = observed_at
    if isinstance(payload, dict):
        user_payload = payload.get("user")
        row.raw = _json_safe(user_payload if isinstance(user_payload, dict) else {"username": username})

    if is_new:
        session.flush()
    return row


def upsert_darknet_profile_from_event(
    session: Session,
    target_id: int,
    target_identifier: str,
    account_id: int | None,
    payload: dict,
    observed_at: dt.datetime | None = None,
    increment_post_counter: bool = True,
) -> bool:
    if not isinstance(payload, dict):
        return False

    event_type = str(payload.get("event_type") or "").strip()
    if event_type not in {"forum_user", "forum_post"}:
        return False

    username_info = _extract_event_username(payload)
    if not username_info:
        return False
    username, username_normalized = username_info

    thread_url = str(payload.get("thread_url") or "").strip() or str(target_identifier or "").strip()
    if not thread_url:
        return False
    thread_title = str(payload.get("thread_title") or "").strip() or None
    seen_at = _extract_event_time(payload, observed_at)
    forum_host = _extract_forum_host(thread_url=thread_url, target_identifier=target_identifier)

    user = _upsert_darknet_user(
        session=session,
        forum_host=forum_host,
        username=username,
        username_normalized=username_normalized,
        observed_at=seen_at,
        payload=payload,
    )
    if user is None:
        return False

    membership = session.execute(
        select(DarknetUserMembership).where(
            DarknetUserMembership.target_id == int(target_id),
            DarknetUserMembership.darknet_user_ref_id == int(user.id),
            DarknetUserMembership.thread_url == thread_url,
        )
    ).scalar_one_or_none()

    is_new = membership is None
    if membership is None:
        membership = DarknetUserMembership(
            target_id=int(target_id),
            account_id=int(account_id) if account_id is not None else None,
            darknet_user_ref_id=int(user.id),
            thread_url=thread_url,
            thread_title=thread_title,
            is_active=True,
            posts_count=1 if event_type == "forum_post" else 0,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            last_source=event_type,
            last_raw=_json_safe(payload.get("raw")) if isinstance(payload.get("raw"), dict) else None,
        )
        session.add(membership)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            membership = session.execute(
                select(DarknetUserMembership).where(
                    DarknetUserMembership.target_id == int(target_id),
                    DarknetUserMembership.darknet_user_ref_id == int(user.id),
                    DarknetUserMembership.thread_url == thread_url,
                )
            ).scalar_one_or_none()
            is_new = False

    if membership is None:
        return False

    if account_id is not None:
        membership.account_id = int(account_id)
    membership.is_active = True
    if thread_title:
        membership.thread_title = thread_title
    membership_last_seen = _coerce_utc(membership.last_seen_at)
    if not membership_last_seen or seen_at > membership_last_seen:
        membership.last_seen_at = seen_at
    if event_type == "forum_post" and increment_post_counter:
        membership.posts_count = int(membership.posts_count or 0) + 1
    membership.last_source = event_type
    if isinstance(payload.get("raw"), dict):
        membership.last_raw = _json_safe(payload.get("raw"))

    if is_new:
        session.flush()
    return True
