from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import EventFile, RawEvent, RawEventFile, Target
from app.plugins.base import FileRef, ParsedEvent

logger = logging.getLogger(__name__)

# Postgres error code for a unique-key violation (our dedup path). Anything
# else caught here — most importantly a foreign-key violation on account_id,
# which arrives over the wire from external bridges (e.g. WhatsApp) and can't
# be trusted the way our own database-generated ids can — must be logged
# instead of silently disappearing.
_UNIQUE_VIOLATION_PGCODE = "23505"


def _is_duplicate_violation(exc: IntegrityError) -> bool:
    """Only true for a duplicate-key violation, never a foreign-key one."""
    pgcode = getattr(exc.orig, "pgcode", None)
    if pgcode is not None:
        return pgcode == _UNIQUE_VIOLATION_PGCODE
    # sqlite (unit tests) has no pgcode; fall back to the driver's message.
    return "unique" in str(exc.orig).lower()


def _link_files(session: Session, raw_event_id: int, files: list[FileRef]) -> None:
    """Attach already-uploaded files to an event, deduplicating by sha256."""
    for position, ref in enumerate(files):
        if not ref.sha256:
            continue

        file = session.execute(
            select(EventFile).where(EventFile.sha256 == ref.sha256)
        ).scalar_one_or_none()
        if file is None:
            file = EventFile(
                sha256=ref.sha256,
                storage_key=f"media/{ref.sha256}",
                mime=ref.mime,
                size=ref.size,
                filename=ref.filename,
            )
            session.add(file)
            session.flush()

        exists = session.execute(
            select(RawEventFile).where(
                RawEventFile.raw_event_id == raw_event_id,
                RawEventFile.file_id == file.id,
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                RawEventFile(
                    raw_event_id=raw_event_id,
                    file_id=file.id,
                    source_ref=ref.source_ref,
                    position=position,
                )
            )


def persist_events(
    session: Session,
    *,
    target: Target,
    parser_type: str,
    account_id: int | None,
    owner_user_id: int | None,
    events: list[ParsedEvent],
) -> list[RawEvent]:
    """Write parsed events, skipping ones already stored.

    Deduplication is by (parser_type, target_id, external_id); events without
    an external_id are always written. Returns the rows actually inserted, in
    input order, so callers can tell which events were new without re-querying.
    """
    if not events:
        return []

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
    written: list[RawEvent] = []

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
        except IntegrityError as exc:
            if not _is_duplicate_violation(exc):
                logger.warning(
                    "persist_events: insert rejected for parser_type=%s target_id=%s "
                    "external_id=%s account_id=%s: %s",
                    parser_type,
                    target.id,
                    event.external_id,
                    account_id,
                    exc.orig,
                )
            continue

        if event.files:
            _link_files(session, raw_event.id, event.files)

        if event.external_id:
            known.add(str(event.external_id))
        written.append(raw_event)

    return written
