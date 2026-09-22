from __future__ import annotations

import datetime as dt

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import JobStatus, ParseJob, ParserAccount, ParserType, Target
from app.plugins.registry import plugin_registry
from app.services.execution_queue import publish_jobs_sync
from app.services.job_routing import default_max_attempts_for_queue, resolve_job_queue

settings = get_settings()


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
            if last_job_at is not None and last_job_at.tzinfo is None:
                # SQLite (used by unit tests) drops tzinfo on round-trip even for
                # DateTime(timezone=True) columns; Postgres preserves it. Normalize
                # to UTC so the subtraction below works the same on both.
                last_job_at = last_job_at.replace(tzinfo=dt.UTC)
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

        queue_value = resolve_job_queue(
            parser_type=target.parser_type,
            job_key=spec.job_key,
            explicit_queue=getattr(spec, "queue", None),
        )
        spec_max_attempts = int(getattr(spec, "max_attempts", 0) or 0)
        max_attempts_value = spec_max_attempts if spec_max_attempts > 0 else default_max_attempts_for_queue(queue_value)

        session.add(
            ParseJob(
                parser_type=target.parser_type,
                target_id=spec.target_id,
                account_id=spec.account_id,
                owner_user_id=target.owner_user_id,
                job_key=spec.job_key,
                payload=spec.payload,
                priority=max(int(getattr(spec, "priority", 100) or 100), 1),
                queue=queue_value,
                max_attempts=max_attempts_value,
                status=JobStatus.pending,
                run_after=getattr(spec, "run_after", None) or now,
            )
        )
        created += 1

    return created, skipped_existing


def schedule_target_once(session: Session, target: Target) -> dict:
    plugin = plugin_registry.get(target.parser_type)
    job_specs = plugin.generate_jobs(session, target)
    created, skipped = _enqueue_job_specs(session, target, job_specs)
    dispatch = dispatch_due_jobs(session=session, target_id=int(target.id))
    return {
        "target_id": target.id,
        "jobs_created": created,
        "jobs_skipped": skipped,
        "dispatched": dispatch["published"],
        "dispatch_errors": dispatch["errors"],
    }


def schedule_once(session: Session, owner_user_id: int | None = None) -> dict:
    # local import: telegram_liveness imports this module
    from app.services.telegram_liveness import check_accounts_liveness

    check_accounts_liveness(session, now=dt.datetime.now(dt.UTC))

    stmt = select(Target).where(Target.is_active.is_(True))
    if owner_user_id is not None:
        stmt = stmt.where(Target.owner_user_id == int(owner_user_id))
    targets = session.execute(stmt).scalars().all()
    created = 0
    skipped_existing = 0

    for target in targets:
        plugin = plugin_registry.get(target.parser_type)
        job_specs = plugin.generate_jobs(session, target)
        target_created, target_skipped = _enqueue_job_specs(session, target, job_specs)
        created += target_created
        skipped_existing += target_skipped

    dispatch = dispatch_due_jobs(session=session, owner_user_id=owner_user_id)
    return {
        "targets": len(targets),
        "jobs_created": created,
        "jobs_skipped": skipped_existing,
        "dispatched": dispatch["published"],
        "dispatch_errors": dispatch["errors"],
    }


def _jobs_for_dispatch(
    session: Session,
    *,
    owner_user_id: int | None = None,
    target_id: int | None = None,
    limit: int | None = None,
) -> list[ParseJob]:
    now = dt.datetime.now(dt.UTC)
    min_interval_seconds = max(int(settings.nats_dispatch_min_interval_seconds), 1)
    min_dispatch_time = now - dt.timedelta(seconds=min_interval_seconds)
    safe_limit = max(int(limit or settings.nats_dispatch_batch_size), 1)

    stmt = (
        select(ParseJob)
        .where(
            ParseJob.status.in_([JobStatus.pending, JobStatus.retry]),
            ParseJob.run_after <= now,
            ParseJob.queue.is_not(None),
            ((ParseJob.lock_expires_at.is_(None)) | (ParseJob.lock_expires_at < now)),
        )
        .where(
            (ParseJob.last_dispatched_at.is_(None)) | (ParseJob.last_dispatched_at <= min_dispatch_time)
        )
        .order_by(ParseJob.priority.asc(), ParseJob.run_after.asc(), ParseJob.id.asc())
        .limit(safe_limit)
    )
    if owner_user_id is not None:
        stmt = stmt.where(ParseJob.owner_user_id == int(owner_user_id))
    if target_id is not None:
        stmt = stmt.where(ParseJob.target_id == int(target_id))

    jobs = session.execute(stmt).scalars().all()
    if not jobs:
        return []

    running_counts_rows = (
        session.execute(
            select(ParseJob.account_id, func.count(ParseJob.id))
            .where(
                ParseJob.status == JobStatus.running,
                ParseJob.account_id.is_not(None),
                or_(ParseJob.lock_expires_at.is_(None), ParseJob.lock_expires_at >= now),
            )
            .group_by(ParseJob.account_id)
        )
        .all()
    )
    running_counts_by_account = {int(account_id): int(count) for account_id, count in running_counts_rows if account_id is not None}
    account_limits_cache: dict[int, int] = {}

    filtered: list[ParseJob] = []
    for job in jobs:
        if job.account_id is None:
            filtered.append(job)
            continue

        account_id = int(job.account_id)
        if account_id not in account_limits_cache:
            account = session.get(ParserAccount, account_id)
            if not account:
                account_limits_cache[account_id] = 1
            elif job.parser_type == ParserType.darknet:
                creds = dict(account.credentials or {})
                try:
                    account_limit = int(creds.get("parallel_jobs", settings.darknet_parallel_jobs_per_account))
                except Exception:
                    account_limit = int(settings.darknet_parallel_jobs_per_account)
                account_limits_cache[account_id] = max(int(account_limit), 1)
            else:
                account_limits_cache[account_id] = 1

        running_count = int(running_counts_by_account.get(account_id, 0))
        account_limit = int(account_limits_cache[account_id])
        if running_count >= account_limit:
            continue
        filtered.append(job)

    return filtered


def dispatch_due_jobs(
    session: Session,
    *,
    owner_user_id: int | None = None,
    target_id: int | None = None,
    limit: int | None = None,
) -> dict:
    if not settings.nats_enabled:
        return {"candidates": 0, "published": 0, "errors": 0}

    jobs = _jobs_for_dispatch(session=session, owner_user_id=owner_user_id, target_id=target_id, limit=limit)
    if not jobs:
        return {"candidates": 0, "published": 0, "errors": 0}

    items = []
    for job in jobs:
        items.append(
            {
                "queue": str(job.queue or "").strip().lower(),
                "payload": {
                    "job_id": int(job.id),
                    "queue": str(job.queue or "").strip().lower(),
                    "parser_type": job.parser_type,
                    "target_id": int(job.target_id),
                    "account_id": int(job.account_id) if job.account_id is not None else None,
                    "attempt": int(job.attempt or 0),
                },
            }
        )

    publish_results = publish_jobs_sync(items)
    now = dt.datetime.now(dt.UTC)
    published = 0
    errors = 0
    for idx, job in enumerate(jobs):
        result = publish_results[idx] if idx < len(publish_results) else {"ok": False, "error": "unknown publish result"}
        ok = bool(result.get("ok"))
        error_text = str(result.get("error") or "").strip() or None
        job.dispatch_attempts = int(job.dispatch_attempts or 0) + 1
        if ok:
            published += 1
            job.last_dispatched_at = now
            job.last_dispatch_error = None
        else:
            errors += 1
            job.last_dispatch_error = (error_text or "publish failed")[:2000]

    return {"candidates": len(jobs), "published": int(published), "errors": int(errors)}


def sync_telegram_memberships(session: Session) -> dict:
    plugin = plugin_registry.get("telegram")
    return plugin.sync_memberships(session)


def sync_whatsapp_memberships(session: Session) -> dict:
    plugin = plugin_registry.get("whatsapp")
    return plugin.sync_memberships(session)


def run_scheduler_forever(session_factory, interval: int) -> None:
    import time

    while True:
        with session_factory() as session:
            schedule_once(session)
            session.commit()
        time.sleep(interval)
