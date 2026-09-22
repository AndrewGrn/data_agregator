from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParserAccount, Target, TargetAccountLink


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _fixtures(session):
    target = Target(parser_type="telegram", name="c", identifier="@c")
    a1 = ParserAccount(parser_type="telegram", label="a1", credentials={})
    a2 = ParserAccount(parser_type="telegram", label="a2", credentials={})
    session.add_all([target, a1, a2])
    session.commit()
    return target, a1, a2


def test_second_active_link_is_rejected():
    session = _session()
    target, a1, a2 = _fixtures(session)
    session.add(TargetAccountLink(target_id=target.id, account_id=a1.id, is_active=True))
    session.commit()
    session.add(TargetAccountLink(target_id=target.id, account_id=a2.id, is_active=True))

    with pytest.raises(IntegrityError):
        session.commit()


def test_inactive_links_may_coexist_with_one_active():
    session = _session()
    target, a1, a2 = _fixtures(session)
    session.add(TargetAccountLink(target_id=target.id, account_id=a1.id, is_active=False))
    session.add(TargetAccountLink(target_id=target.id, account_id=a2.id, is_active=True))
    session.commit()

    assert session.query(TargetAccountLink).count() == 2


def test_new_account_columns_have_defaults():
    session = _session()
    acc = ParserAccount(parser_type="telegram", label="x", credentials={})
    session.add(acc)
    session.commit()

    assert acc.pool_mode == "shared"
    assert acc.alive is None
    assert acc.join_window_count == 0


def test_new_target_columns_have_defaults():
    session = _session()
    t = Target(parser_type="telegram", name="c", identifier="@c")
    session.add(t)
    session.commit()

    assert t.onboarding_step == "idle"
    assert t.onboarding_error is None
