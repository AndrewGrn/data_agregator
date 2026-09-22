from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import JobStatus, ParseJob, ParserAccount, Target
from app.plugins.base import DeferJob, JobSpec
from app.services import worker as worker_mod
from app.services.scheduler import _enqueue_job_specs


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_jobspec_run_after_is_honoured():
    session = _session()
    target = Target(parser_type="telegram", name="c", identifier="@c")
    session.add(target)
    session.commit()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)

    _enqueue_job_specs(
        session,
        target,
        [JobSpec(parser_type="telegram", target_id=target.id, account_id=None, job_key="k", payload={}, run_after=later)],
    )
    session.commit()

    job = session.query(ParseJob).one()
    assert abs((job.run_after.replace(tzinfo=dt.UTC) - later).total_seconds()) < 1


def test_defer_job_reschedules_without_penalty(monkeypatch):
    session = _session()
    target = Target(parser_type="telegram", name="c", identifier="@c")
    account = ParserAccount(parser_type="telegram", label="a", credentials={}, health_score=90.0, fail_count=0)
    session.add_all([target, account])
    session.commit()
    job = ParseJob(
        parser_type="telegram", target_id=target.id, account_id=account.id,
        job_key="onboard:1", payload={"mode": "onboard"}, queue="telegram_backfill",
        status=JobStatus.running, attempt=1, max_attempts=5,
    )
    session.add(job)
    session.commit()

    class _Plugin:
        def run(self, session, job, target, account):
            raise DeferJob(seconds=600, reason="join pacing")

    monkeypatch.setattr(worker_mod.plugin_registry, "get", lambda name: _Plugin())

    worker_mod._process_job(session, job)
    session.commit()

    job = session.get(ParseJob, job.id)
    account = session.get(ParserAccount, account.id)
    assert job.status == JobStatus.retry
    assert job.attempt == 0, "deferral must not consume an attempt"
    assert (job.run_after.replace(tzinfo=dt.UTC) - dt.datetime.now(dt.UTC)).total_seconds() > 500
    assert job.last_error == "join pacing"
    assert account.fail_count == 0
    assert account.health_score == 90.0
    assert account.cooldown_until is None


def _running_job(session):
    target = Target(parser_type="telegram", name="c", identifier="@c")
    account = ParserAccount(parser_type="telegram", label="a", credentials={}, health_score=90.0, fail_count=0)
    session.add_all([target, account])
    session.commit()
    job = ParseJob(
        parser_type="telegram", target_id=target.id, account_id=account.id,
        job_key="backfill-full:1:1:0", payload={"backfill": True}, queue="telegram_backfill",
        status=JobStatus.running, attempt=1, max_attempts=5,
    )
    session.add(job)
    session.commit()
    return job, account


def _run_raising(session, job, exc, monkeypatch):
    class _Plugin:
        def run(self, session, job, target, account):
            raise exc

    monkeypatch.setattr(worker_mod.plugin_registry, "get", lambda name: _Plugin())
    worker_mod._process_job(session, job)
    session.commit()
    return session.get(ParseJob, job.id), session.get(ParserAccount, job.account_id)


def test_our_own_errors_fail_the_job_but_spare_the_account(monkeypatch):
    """Regression: a fetch timeout and a jsonb insert error each cost the only
    live account a 30-minute cooldown, so the UI showed 'accounts unavailable'
    for a bug in our code."""
    from sqlalchemy.exc import DataError

    for exc in (TimeoutError("Telegram messages fetch timeout after 900s"),
                DataError("INSERT", {}, Exception("\\u0000 cannot be converted to text"))):
        session = _session()
        job, account = _running_job(session)
        job, account = _run_raising(session, job, exc, monkeypatch)
        assert job.status == JobStatus.retry
        assert account.fail_count == 0
        assert account.health_score == 90.0
        assert account.cooldown_until is None


def test_telegram_rpc_errors_still_penalise_the_account(monkeypatch):
    from telethon.errors import AuthKeyUnregisteredError

    session = _session()
    job, account = _running_job(session)
    job, account = _run_raising(session, job, AuthKeyUnregisteredError(request=None), monkeypatch)
    assert job.status == JobStatus.retry
    assert account.fail_count == 1
    assert account.health_score == 82.0
    assert account.cooldown_until is not None
