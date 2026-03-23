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


def test_scheduler_creates_darknet_job_once():
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
