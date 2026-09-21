from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import EventFile, RawEventFile, Target
from app.plugins.base import FileRef, ParsedEvent
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


def _event(external_id: str, files: list[FileRef]) -> ParsedEvent:
    return ParsedEvent(
        external_id=external_id,
        observed_at=dt.datetime.now(dt.UTC),
        payload={"text": "with media"},
        text="with media",
        files=files,
    )


def _persist(session, target, event):
    persist_events(
        session, target=target, parser_type="telegram",
        account_id=None, owner_user_id=None, events=[event],
    )
    session.commit()


def test_file_row_is_created():
    session = _session()
    target = _target(session)

    _persist(session, target, _event("1", [
        FileRef(source_ref="f1", sha256="aa", mime="image/jpeg", size=100, filename="a.jpg"),
    ]))

    file = session.query(EventFile).one()
    assert file.storage_key == "media/aa"
    assert session.query(RawEventFile).count() == 1


def test_same_file_in_two_events_is_stored_once():
    session = _session()
    target = _target(session)
    shared = FileRef(source_ref="f1", sha256="aa", mime="image/jpeg", size=100)

    _persist(session, target, _event("1", [shared]))
    _persist(session, target, _event("2", [shared]))

    assert session.query(EventFile).count() == 1
    assert session.query(RawEventFile).count() == 2


def test_file_without_sha256_is_skipped():
    session = _session()
    target = _target(session)

    _persist(session, target, _event("1", [FileRef(source_ref="f1", sha256=None)]))

    assert session.query(EventFile).count() == 0


def test_several_files_keep_their_order():
    session = _session()
    target = _target(session)

    _persist(session, target, _event("1", [
        FileRef(source_ref="f1", sha256="aa"),
        FileRef(source_ref="f2", sha256="bb"),
    ]))

    links = session.query(RawEventFile).order_by(RawEventFile.position).all()
    assert [link.position for link in links] == [0, 1]
