from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobStatus, ParseJob, Target
from app.plugins.registry import plugin_registry


def schedule_once(session: Session) -> dict:
    targets = session.execute(select(Target).where(Target.is_active.is_(True))).scalars().all()
    created = 0
    skipped_existing = 0

    for target in targets:
        plugin = plugin_registry.get(target.parser_type.value)
        job_specs = plugin.generate_jobs(session, target)

        for spec in job_specs:
            if spec.job_key and spec.job_key.startswith("backfill:"):
                existing = session.execute(select(ParseJob).where(ParseJob.job_key == spec.job_key)).scalar_one_or_none()
            elif spec.job_key:
                existing = session.execute(
                    select(ParseJob).where(
                        ParseJob.job_key == spec.job_key,
                        ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
                    )
                ).scalar_one_or_none()
            else:
                existing = session.execute(
                    select(ParseJob).where(
                        ParseJob.target_id == spec.target_id,
                        ParseJob.account_id == spec.account_id,
                        ParseJob.parser_type == target.parser_type,
                        ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
                    )
                ).scalar_one_or_none()
            if existing:
                skipped_existing += 1
                continue

            session.add(
                ParseJob(
                    parser_type=target.parser_type,
                    target_id=spec.target_id,
                    account_id=spec.account_id,
                    job_key=spec.job_key,
                    payload=spec.payload,
                    status=JobStatus.pending,
                    run_after=dt.datetime.now(dt.UTC),
                )
            )
            created += 1

    return {"targets": len(targets), "jobs_created": created, "jobs_skipped": skipped_existing}


def sync_telegram_memberships(session: Session) -> dict:
    plugin = plugin_registry.get("telegram")
    return plugin.sync_memberships(session)


def run_scheduler_forever(session_factory, interval: int) -> None:
    import time

    while True:
        with session_factory() as session:
            schedule_once(session)
            session.commit()
        time.sleep(interval)
