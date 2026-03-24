from __future__ import annotations

import datetime as dt

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import JobStatus, ParseJob, Target
from app.plugins.registry import plugin_registry


def _enqueue_job_specs(session: Session, target: Target, job_specs) -> tuple[int, int]:
    created = 0
    skipped_existing = 0
    now = dt.datetime.now(dt.UTC)

    for spec in job_specs:
        interval_seconds = max(int((spec.payload or {}).get("min_interval_seconds", 0)), 0)
        if spec.job_key and interval_seconds > 0:
            last_job_at = session.execute(
                select(ParseJob.created_at).where(ParseJob.job_key == spec.job_key).order_by(desc(ParseJob.created_at)).limit(1)
            ).scalar_one_or_none()
            if last_job_at and (now - last_job_at) < dt.timedelta(seconds=interval_seconds):
                skipped_existing += 1
                continue

        if spec.job_key and (spec.job_key.startswith("backfill:") or spec.job_key.startswith("backfill-full:")):
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
                owner_user_id=target.owner_user_id,
                job_key=spec.job_key,
                payload=spec.payload,
                priority=max(int(getattr(spec, "priority", 100) or 100), 1),
                status=JobStatus.pending,
                run_after=now,
            )
        )
        created += 1

    return created, skipped_existing


def schedule_target_once(session: Session, target: Target) -> dict:
    plugin = plugin_registry.get(target.parser_type.value)
    job_specs = plugin.generate_jobs(session, target)
    created, skipped = _enqueue_job_specs(session, target, job_specs)
    return {"target_id": target.id, "jobs_created": created, "jobs_skipped": skipped}


def schedule_once(session: Session, owner_user_id: int | None = None) -> dict:
    stmt = select(Target).where(Target.is_active.is_(True))
    if owner_user_id is not None:
        stmt = stmt.where(Target.owner_user_id == int(owner_user_id))
    targets = session.execute(stmt).scalars().all()
    created = 0
    skipped_existing = 0

    for target in targets:
        plugin = plugin_registry.get(target.parser_type.value)
        job_specs = plugin.generate_jobs(session, target)
        target_created, target_skipped = _enqueue_job_specs(session, target, job_specs)
        created += target_created
        skipped_existing += target_skipped

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
