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

    assert written == 1
    stored = session.query(RawEvent).one()
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

    assert written == 0
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
