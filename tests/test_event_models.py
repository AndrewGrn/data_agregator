from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import EventFile, RawEvent, RawEventFile, Target


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _target(session):
    target = Target(parser_type="telegram", name="chan", identifier="@chan")
    session.add(target)
    session.commit()
    return target


def test_raw_event_stores_normalized_fields():
    session = _session()
    target = _target(session)
    session.add(
        RawEvent(
            parser_type="telegram",
            target_id=target.id,
            external_id="42",
            observed_at=dt.datetime.now(dt.UTC),
            payload={"event_type": "telegram_message", "text": "hello"},
            text="hello",
            author_id="777",
            author_label="@alice",
            event_kind="message",
            thread_id="@chan",
        )
    )
    session.commit()

    stored = session.query(RawEvent).one()
    assert stored.payload["text"] == "hello"
    assert stored.author_id == "777"
    assert stored.event_kind == "message"


def test_payload_is_required():
    session = _session()
    target = _target(session)
    session.add(RawEvent(parser_type="telegram", target_id=target.id, external_id="1"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_one_file_links_to_many_events():
    session = _session()
    target = _target(session)
    first = RawEvent(parser_type="telegram", target_id=target.id, external_id="1", payload={})
    second = RawEvent(parser_type="telegram", target_id=target.id, external_id="2", payload={})
    file = EventFile(sha256="deadbeef", storage_key="media/deadbeef", mime="image/jpeg", size=10)
    session.add_all([first, second, file])
    session.flush()
    session.add_all(
        [
            RawEventFile(raw_event_id=first.id, file_id=file.id, source_ref="a", position=0),
            RawEventFile(raw_event_id=second.id, file_id=file.id, source_ref="b", position=0),
        ]
    )
    session.commit()

    assert session.query(EventFile).count() == 1
    assert session.query(RawEventFile).count() == 2
