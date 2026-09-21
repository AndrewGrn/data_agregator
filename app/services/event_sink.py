from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import RawEvent, Target
from app.plugins.base import ParsedEvent


def persist_events(
    session: Session,
    *,
    target: Target,
    parser_type: str,
    account_id: int | None,
    owner_user_id: int | None,
    events: list[ParsedEvent],
) -> int:
    """Write parsed events, skipping ones already stored.

    Deduplication is by (parser_type, target_id, external_id); events without
    an external_id are always written. Returns the number of new rows.
    """
    if not events:
        return 0

    external_ids = [str(event.external_id) for event in events if event.external_id]
    known: set[str] = set()
    if external_ids:
        rows = session.execute(
            select(RawEvent.external_id).where(
                RawEvent.parser_type == parser_type,
                RawEvent.target_id == target.id,
                RawEvent.external_id.in_(external_ids),
            )
        ).all()
        known = {str(row[0]) for row in rows if row[0]}

    resolved_owner = owner_user_id if owner_user_id is not None else target.owner_user_id
    written = 0

    for event in events:
        if event.external_id and str(event.external_id) in known:
            continue

        raw_event = RawEvent(
            parser_type=parser_type,
            target_id=target.id,
            account_id=account_id,
            owner_user_id=resolved_owner,
            external_id=event.external_id,
            observed_at=event.observed_at,
            payload=event.payload if isinstance(event.payload, dict) else {},
            text=event.text,
            author_id=event.author_id,
            author_label=event.author_label,
            event_kind=event.event_kind,
            thread_id=event.thread_id,
            reply_to=event.reply_to,
        )
        try:
            # A savepoint keeps a concurrent duplicate from failing the batch.
            with session.begin_nested():
                session.add(raw_event)
                session.flush()
        except IntegrityError:
            continue

        if event.external_id:
            known.add(str(event.external_id))
        written += 1

    return written
