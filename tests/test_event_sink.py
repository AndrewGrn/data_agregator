from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import RawEvent, Target
from app.plugins.base import ParsedEvent
from app.services.event_sink import persist_events


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _target(session):
    target = Target(parser_type="telegram", name="chan", identifier="@chan")
    session.add(target)
    session.commit()
    return target


def _event(external_id: str, text: str = "hello") -> ParsedEvent:
    return ParsedEvent(
        external_id=external_id,
        observed_at=dt.datetime.now(dt.UTC),
        payload={"event_type": "telegram_message", "text": text},
        text=text,
        author_id="777",
        author_label="@alice",
        event_kind="message",
        thread_id="@chan",
    )


def test_persists_normalized_fields():
    session = _session()
    target = _target(session)

    written = persist_events(
        session,
        target=target,
        parser_type="telegram",
        account_id=None,
        owner_user_id=None,
        events=[_event("1")],
    )
    session.commit()

    assert len(written) == 1
    stored = session.query(RawEvent).one()
    assert written[0] is stored
    assert stored.text == "hello"
    assert stored.author_label == "@alice"
    assert stored.payload["event_type"] == "telegram_message"


def test_duplicate_external_id_is_skipped():
    session = _session()
    target = _target(session)

    persist_events(session, target=target, parser_type="telegram", account_id=None, owner_user_id=None, events=[_event("1")])
    session.commit()
    written = persist_events(session, target=target, parser_type="telegram", account_id=None, owner_user_id=None, events=[_event("1", text="changed")])
    session.commit()

    assert written == []
    assert session.query(RawEvent).count() == 1


def test_event_without_external_id_is_always_written():
    session = _session()
    target = _target(session)

    persist_events(session, target=target, parser_type="darknet", account_id=None, owner_user_id=None, events=[
        ParsedEvent(external_id=None, observed_at=None, payload={"a": 1}),
    ])
    persist_events(session, target=target, parser_type="darknet", account_id=None, owner_user_id=None, events=[
        ParsedEvent(external_id=None, observed_at=None, payload={"a": 2}),
    ])
    session.commit()

    assert session.query(RawEvent).count() == 2


def test_owner_falls_back_to_target_owner():
    session = _session()
    target = _target(session)
    target.owner_user_id = 5
    session.commit()

    persist_events(session, target=target, parser_type="telegram", account_id=None, owner_user_id=None, events=[_event("1")])
    session.commit()

    assert session.query(RawEvent).one().owner_user_id == 5


class _EmptyResult:
    """Stand-in for a SQLAlchemy Result whose .all() found nothing."""

    def all(self):
        return []


class _BlindPreCheckSession:
    """Session whose first execute() returns no rows, simulating a duplicate
    committed by another worker after our pre-check ran."""

    def __init__(self, inner):
        self._inner = inner
        self._pre_check_done = False

    def execute(self, *args, **kwargs):
        if not self._pre_check_done:
            self._pre_check_done = True
            return _EmptyResult()
        return self._inner.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_savepoint_absorbs_race_and_batch_survives():
    session = _session()
    target = _target(session)

    # Seed a row as if another worker already committed it.
    persist_events(session, target=target, parser_type="telegram", account_id=None, owner_user_id=None, events=[_event("1")])
    session.commit()

    wrapped = _BlindPreCheckSession(session)
    written = persist_events(
        wrapped,
        target=target,
        parser_type="telegram",
        account_id=None,
        owner_user_id=None,
        # "1" races past the blinded pre-check and must hit the DB unique
        # constraint inside the savepoint; "2" is a genuinely new event that
        # must still be written despite "1" raising IntegrityError first.
        events=[_event("1", text="racing duplicate"), _event("2", text="new")],
    )
    session.commit()

    # Only the genuinely new event comes back; the race loser must not appear.
    assert [row.external_id for row in written] == ["2"]
    duplicate_rows = session.query(RawEvent).filter_by(external_id="1").all()
    assert len(duplicate_rows) == 1
    assert duplicate_rows[0].text == "hello"  # original row untouched by the race loser
    assert session.query(RawEvent).filter_by(external_id="2").count() == 1
