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
