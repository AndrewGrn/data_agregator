from __future__ import annotations

import datetime as dt
import socket
import threading
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import JobStatus, ParseJob, ParserAccount, RawEvent, Target
from app.plugins.registry import plugin_registry
from app.services.object_store import object_store

settings = get_settings()


def _lock_next_job(session: Session, worker_id: str) -> ParseJob | None:
    now = dt.datetime.now(dt.UTC)

    stmt = select(ParseJob).where(
        ParseJob.status.in_([JobStatus.pending, JobStatus.retry]),
        ParseJob.run_after <= now,
        or_(ParseJob.lock_expires_at.is_(None), ParseJob.lock_expires_at < now),
    ).order_by(ParseJob.created_at.asc()).limit(1)

    dialect = session.get_bind().dialect.name
    if dialect in {"postgresql", "mysql"}:
        stmt = stmt.with_for_update(skip_locked=True)

    job = session.execute(stmt).scalar_one_or_none()
    if not job:
        return None

    job.status = JobStatus.running
    job.locked_by = worker_id
    job.lock_expires_at = now + dt.timedelta(minutes=10)
    job.attempt += 1
    return job


def _complete_job(session: Session, job: ParseJob) -> None:
    job.status = JobStatus.succeeded
    job.finished_at = dt.datetime.now(dt.UTC)
    job.locked_by = None
    job.lock_expires_at = None


def _refresh_rate_window(account: ParserAccount, now: dt.datetime) -> None:
    if not account.hour_window_start or (now - account.hour_window_start) >= dt.timedelta(hours=1):
        account.hour_window_start = now
        account.hour_window_count = 0


def _register_account_success(account: ParserAccount) -> None:
    now = dt.datetime.now(dt.UTC)
    _refresh_rate_window(account, now)
    account.hour_window_count += 1
    account.success_count += 1
    account.health_score = min(100.0, account.health_score + 1.0)
    account.cooldown_until = None
    account.last_error = None
    account.last_success_at = now


def _register_account_failure(account: ParserAccount, error: str) -> None:
    now = dt.datetime.now(dt.UTC)
    _refresh_rate_window(account, now)
    account.fail_count += 1
    account.health_score = max(0.0, account.health_score - 8.0)
    cooldown_seconds = min(1800, 30 * (2 ** min(account.fail_count, 6)))
    account.cooldown_until = now + dt.timedelta(seconds=cooldown_seconds)
    account.last_error = error[:2000]


def _fail_job(session: Session, job: ParseJob, error: str) -> None:
    now = dt.datetime.now(dt.UTC)
    if job.attempt >= job.max_attempts:
        job.status = JobStatus.failed
        job.finished_at = now
    else:
        job.status = JobStatus.retry
        backoff = min(300, 2 ** job.attempt)
        job.run_after = now + dt.timedelta(seconds=backoff)
    job.last_error = error[:2000]
    job.locked_by = None
    job.lock_expires_at = None


def _process_job(session: Session, job: ParseJob) -> None:
    target = session.get(Target, job.target_id)
    if not target:
        _fail_job(session, job, "Target not found")
        return

    account = session.get(ParserAccount, job.account_id) if job.account_id else None
    plugin = plugin_registry.get(job.parser_type.value)

    try:
        events = plugin.run(session, job, target, account)
        for event in events:
            stored = object_store.put_json(
                parser_type=job.parser_type.value,
                target_id=job.target_id,
                payload=event.payload,
                external_id=event.external_id,
            )
            session.add(
                RawEvent(
                    parser_type=job.parser_type,
                    target_id=job.target_id,
                    account_id=job.account_id,
                    external_id=event.external_id,
                    observed_at=event.observed_at,
                    storage_type=stored.storage_type,
                    payload_ref=stored.payload_ref,
                    payload_sha256=stored.payload_sha256,
                    payload_size=stored.payload_size,
                    payload_preview=stored.payload_preview,
                    payload=stored.payload_inline,
                )
            )
        if account and job.parser_type.value == "telegram":
            _register_account_success(account)
        _complete_job(session, job)
    except Exception as exc:
        if account and job.parser_type.value == "telegram":
            _register_account_failure(account, str(exc))
        _fail_job(session, job, str(exc))


def worker_loop(session_factory, worker_name: str) -> None:
    while True:
        with session_factory() as session:
            job = _lock_next_job(session, worker_name)
            if not job:
                session.commit()
                time.sleep(settings.worker_poll_interval)
                continue

            _process_job(session, job)
            session.commit()


def run_workers(session_factory, concurrency: int) -> None:
    hostname = socket.gethostname()
    threads: list[threading.Thread] = []

    for idx in range(concurrency):
        name = f"{hostname}-w{idx}"
        thread = threading.Thread(target=worker_loop, args=(session_factory, name), daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()
