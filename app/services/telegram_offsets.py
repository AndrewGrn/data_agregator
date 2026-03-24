from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TelegramOffset


def _coerce_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def get_or_create_offset(session: Session, target_id: int, account_id: int) -> TelegramOffset:
    row = session.execute(
        select(TelegramOffset).where(
            TelegramOffset.target_id == int(target_id),
            TelegramOffset.account_id == int(account_id),
        )
    ).scalar_one_or_none()
    if row:
        return row

    row = TelegramOffset(
        target_id=int(target_id),
        account_id=int(account_id),
        max_message_id=0,
        is_caught_up=False,
    )
    session.add(row)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        row = session.execute(
            select(TelegramOffset).where(
                TelegramOffset.target_id == int(target_id),
                TelegramOffset.account_id == int(account_id),
            )
        ).scalar_one()
    return row


def update_offset_from_message(
    session: Session,
    target_id: int,
    account_id: int,
    message_id: int,
    observed_at: dt.datetime | None,
    source: str,
) -> TelegramOffset:
    row = get_or_create_offset(session, target_id=target_id, account_id=account_id)
    message_id_int = int(message_id)
    if message_id_int > int(row.max_message_id or 0):
        row.max_message_id = message_id_int
    observed_utc = _coerce_utc(observed_at)
    last_event_at = _coerce_utc(row.last_event_at)
    if observed_utc and (not last_event_at or observed_utc > last_event_at):
        row.last_event_at = observed_utc
    row.last_source = str(source or "")[:64] or None
    row.updated_at = dt.datetime.now(dt.UTC)
    return row


def update_gapfill_state(
    session: Session,
    target_id: int,
    account_id: int,
    max_message_id: int | None,
    max_observed_at: dt.datetime | None,
    batch_count: int,
    batch_limit: int,
    source: str = "gap_fill",
) -> TelegramOffset:
    row = get_or_create_offset(session, target_id=target_id, account_id=account_id)
    if max_message_id is not None and int(max_message_id) > int(row.max_message_id or 0):
        row.max_message_id = int(max_message_id)

    observed_utc = _coerce_utc(max_observed_at)
    last_event_at = _coerce_utc(row.last_event_at)
    if observed_utc and (not last_event_at or observed_utc > last_event_at):
        row.last_event_at = observed_utc

    row.last_gapfill_batch_count = max(int(batch_count), 0)
    row.last_gapfill_limit = max(int(batch_limit), 1)
    row.is_caught_up = int(batch_count) < max(int(batch_limit), 1)
    row.last_source = str(source or "gap_fill")[:64]
    row.updated_at = dt.datetime.now(dt.UTC)
    return row

