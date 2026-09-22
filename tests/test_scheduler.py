import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import JobStatus, ParseJob, ParserType, Target
from app.services.scheduler import schedule_once


def make_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_scheduler_creates_darknet_job_once(monkeypatch):
    # Job creation is a pure DB-side decision; dispatching created jobs onto
    # NATS is a separate concern that needs a live broker (nats://nats:4222
    # only resolves inside the docker network). Stub the publish call so
    # this stays a unit test of scheduling, not an integration test of NATS.
    monkeypatch.setattr(
        "app.services.scheduler.publish_jobs_sync",
        lambda items: [{"ok": True, "error": None} for _ in items],
    )

    session = make_session()
    try:
        target = Target(parser_type=ParserType.darknet, name="forum", identifier="http://example.onion", config={})
        session.add(target)
        session.commit()

        first = schedule_once(session)
        session.commit()

        second = schedule_once(session)
        session.commit()

        jobs = session.execute(select(ParseJob)).scalars().all()

        assert first["jobs_created"] == 1
        assert second["jobs_created"] == 0
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.pending
        assert jobs[0].target_id == target.id
    finally:
        session.close()


def test_full_backfill_restarts_after_progress_reset_but_never_doubles_open_job():
    """Regression: @ukrdropcard's history was wiped, full_progress was gone, yet
    the scheduler never re-queued backfill-full:1:1:0 because a job with that
    key had succeeded in March. Only an OPEN job with the same key may block."""
    from app.plugins.base import JobSpec
    from app.services.scheduler import _enqueue_job_specs

    session = make_session()
    target = Target(parser_type=ParserType.telegram, name="c", identifier="@c", config={})
    session.add(target)
    session.commit()
    spec = JobSpec(parser_type="telegram", target_id=target.id, account_id=1,
                   job_key=f"backfill-full:{target.id}:1:0", payload={"backfill": True, "offset_id": 0})

    created, _ = _enqueue_job_specs(session, target, [spec]); session.commit()
    assert created == 1
    created, skipped = _enqueue_job_specs(session, target, [spec]); session.commit()
    assert (created, skipped) == (0, 1), "open job with the same key blocks a duplicate"

    job = session.execute(select(ParseJob)).scalar_one()
    job.status = JobStatus.succeeded
    session.commit()
    created, _ = _enqueue_job_specs(session, target, [spec]); session.commit()
    assert created == 1, "a long-finished job must not block a restart"

    # date-range backfills keep the run-once-ever rule
    rng = JobSpec(parser_type="telegram", target_id=target.id, account_id=1,
                  job_key=f"backfill:{target.id}:1:2024-01-01", payload={"backfill": True})
    _enqueue_job_specs(session, target, [rng]); session.commit()
    session.execute(select(ParseJob).where(ParseJob.job_key == rng.job_key)).scalar_one().status = JobStatus.succeeded
    session.commit()
    created, skipped = _enqueue_job_specs(session, target, [rng]); session.commit()
    assert (created, skipped) == (0, 1)
