from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParseJob, ParserAccount, ServiceState, Target, TargetAccountLink
from app.services import telegram_liveness as live


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _setup(session, *, pool_mode="shared"):
    acc = ParserAccount(parser_type="telegram", label="a", pool_mode=pool_mode,
                        credentials={"api_id": 1, "api_hash": "h", "session_string": "s"})
    t1 = Target(parser_type="telegram", name="c1", identifier="@c1", config={})
    t2 = Target(parser_type="telegram", name="c2", identifier="@c2", config={})
    session.add_all([acc, t1, t2])
    session.commit()
    session.add_all([
        TargetAccountLink(target_id=t1.id, account_id=acc.id, is_active=True),
        TargetAccountLink(target_id=t2.id, account_id=acc.id, is_active=True),
    ])
    session.commit()
    return acc, t1, t2


def _dead(creds):
    return {"alive": False, "is_authorized": False, "error": "AuthKeyUnregistered"}


def _ok(creds):
    return {"alive": True, "is_authorized": True, "username": "a"}


def test_single_failure_counts_but_keeps_account_usable():
    """One failed probe must not black out the pool until the next check."""
    session = _session()
    acc, t1, t2 = _setup(session)
    now = dt.datetime.now(dt.UTC)

    result = live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    session.commit()

    assert result["dead"] == 0 and result["failed_over"] == 0
    assert acc.alive is not False
    assert acc.credentials["liveness_failures"] == 1
    from app.services.telegram_onboarding import pick_account
    assert pick_account(session, t1, now=now) is not None
    links = session.execute(select(TargetAccountLink)).scalars().all()
    assert all(l.is_active for l in links)


def test_two_failures_failover_shared_account():
    session = _session()
    acc, t1, t2 = _setup(session)
    now = dt.datetime.now(dt.UTC)

    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    result = live.check_accounts_liveness(session, now=now + dt.timedelta(minutes=11), checker=_dead, force=True)
    session.commit()

    assert result["dead"] == 1 and result["failed_over"] == 2
    links = session.execute(select(TargetAccountLink)).scalars().all()
    assert all(not l.is_active for l in links)
    for t in (t1, t2):
        assert t.onboarding_step == "queued"
    jobs = session.execute(select(ParseJob).where(ParseJob.job_key.like("onboard:%"))).scalars().all()
    assert len(jobs) == 2
    assert acc.is_active is True, "dead account stays active so it can come back"


def test_dedicated_account_does_not_failover():
    session = _session()
    acc, t1, t2 = _setup(session, pool_mode="dedicated")
    now = dt.datetime.now(dt.UTC)

    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    result = live.check_accounts_liveness(session, now=now + dt.timedelta(minutes=11), checker=_dead, force=True)
    session.commit()

    assert result["dead"] == 1 and result["failed_over"] == 0
    assert all(not l.is_active for l in session.execute(select(TargetAccountLink)).scalars())
    assert t1.onboarding_status.value == "needs_account"
    assert t1.onboarding_step == "idle"
    assert session.query(ParseJob).count() == 0


def test_alive_resets_failure_counter():
    session = _session()
    acc, *_ = _setup(session)
    now = dt.datetime.now(dt.UTC)

    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    live.check_accounts_liveness(session, now=now, checker=_ok, force=True)
    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    session.commit()

    assert acc.credentials["liveness_failures"] == 1, "recovery must reset the counter"
    assert acc.alive is not False
    assert all(l.is_active for l in session.execute(select(TargetAccountLink)).scalars()), "1 failure after recovery is not death"


def test_interval_gate_skips_when_recent():
    session = _session()
    _setup(session)
    now = dt.datetime.now(dt.UTC)

    first = live.check_accounts_liveness(session, now=now, checker=_ok)
    second = live.check_accounts_liveness(session, now=now + dt.timedelta(seconds=30), checker=_ok)

    assert first["checked"] == 1
    assert second["checked"] == 0
    state = session.get(ServiceState, "telegram_liveness_last_run")
    assert state is not None
