from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParserType, Target


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_enum_member_is_stored_as_plain_value():
    session = _session()
    session.add(Target(parser_type=ParserType.telegram, name="t", identifier="@x"))
    session.commit()

    stored = session.execute(
        Target.__table__.select().with_only_columns(Target.__table__.c.parser_type)
    ).scalar_one()
    assert stored == "telegram"


def test_plain_string_is_accepted():
    session = _session()
    session.add(Target(parser_type="whatsapp", name="t", identifier="1@c.us"))
    session.commit()

    stored = session.execute(
        Target.__table__.select().with_only_columns(Target.__table__.c.parser_type)
    ).scalar_one()
    assert stored == "whatsapp"
