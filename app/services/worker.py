from __future__ import annotations

import datetime as dt
import socket
import threading
import time

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import JobStatus, ParseJob, ParserAccount, RawEvent, Target
from app.plugins.registry import plugin_registry
from app.services.object_store import object_store
from app.services.telegram_accounts import account_parallel_limits
from app.services.telegram_offsets import update_offset_from_message
from app.services.telegram_profiles import upsert_telegram_profile_from_event

settings = get_settings()


def _job_lock_minutes(job: ParseJob) -> int:
    key = str(job.job_key or "")
    fetch_timeout_seconds = max(int(settings.telegram_fetch_timeout_seconds), 30)
    soft_minutes = max(5, (fetch_timeout_seconds // 60) + 2)
    backfill_minutes = max(10, soft_minutes * 2)
    if key.startswith("gapfill:"):
        return soft_minutes
    if key.startswith("backfill-full:"):
        return backfill_minutes
    if key.startswith("backfill:"):
        return backfill_minutes
    if key.startswith("poll:"):
        return soft_minutes
    if key.startswith("participants:"):
        return soft_minutes
    return 15


def _job_parallel_limit(job: ParseJob, account: ParserAccount | None) -> int:
    key = str(job.job_key or "")
    if account and account.parser_type.value == "telegram":
        parallel_jobs, backfill_parallel_jobs = account_parallel_limits(
            account=account,
            default_parallel_jobs=settings.telegram_parallel_jobs_per_account,
            default_backfill_parallel_jobs=settings.telegram_backfill_parallel_jobs_per_account,
        )
    else:
        parallel_jobs, backfill_parallel_jobs = 1, 1
    if key.startswith("gapfill:"):
        return parallel_jobs
    if key.startswith("poll:"):
        return parallel_jobs
    if key.startswith("backfill-full:") or key.startswith("backfill:"):
        return backfill_parallel_jobs
    if key.startswith("participants:"):
        return 1
    return 1


def _running_jobs_for_account(session: Session, account_id: int, now: dt.datetime, excluding_job_id: int | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(ParseJob)
        .where(
            ParseJob.status == JobStatus.running,
            ParseJob.account_id == int(account_id),
            or_(ParseJob.lock_expires_at.is_(None), ParseJob.lock_expires_at >= now),
        )
    )
    if excluding_job_id is not None:
        stmt = stmt.where(ParseJob.id != int(excluding_job_id))
    return int(session.scalar(stmt) or 0)


def _lock_next_job(session: Session, worker_id: str) -> ParseJob | None:
    now = dt.datetime.now(dt.UTC)
    stmt = (
        select(ParseJob)
        .where(
        ParseJob.status.in_([JobStatus.pending, JobStatus.retry]),
        ParseJob.run_after <= now,
        or_(ParseJob.lock_expires_at.is_(None), ParseJob.lock_expires_at < now),
        )
        .order_by(ParseJob.priority.asc(), ParseJob.run_after.asc(), ParseJob.created_at.asc())
        .limit(50)
    )

    dialect = session.get_bind().dialect.name
    if dialect in {"postgresql", "mysql"}:
        stmt = stmt.with_for_update(skip_locked=True)

    candidates = session.execute(stmt).scalars().all()
    if not candidates:
        return None

    for candidate in candidates:
        if candidate.account_id is not None:
            account_stmt = select(ParserAccount).where(ParserAccount.id == candidate.account_id)
            if dialect in {"postgresql", "mysql"}:
                account_stmt = account_stmt.with_for_update(skip_locked=True)
            account_row = session.execute(account_stmt).scalar_one_or_none()
            if account_row is None:
                continue

            running_count = _running_jobs_for_account(
                session=session,
                account_id=int(candidate.account_id),
                now=now,
                excluding_job_id=int(candidate.id),
            )
            if running_count >= _job_parallel_limit(candidate, account_row):
                continue

        candidate.status = JobStatus.running
        candidate.locked_by = worker_id
        candidate.lock_expires_at = now + dt.timedelta(minutes=_job_lock_minutes(candidate))
        candidate.attempt += 1
        return candidate

    return None


def _recover_stale_running_jobs(session: Session) -> int:
    now = dt.datetime.now(dt.UTC)
    telegram_stale_before = now - dt.timedelta(seconds=max(int(settings.telegram_fetch_timeout_seconds) * 2, 600))
    stmt = (
        select(ParseJob)
        .where(ParseJob.status == JobStatus.running)
        .order_by(ParseJob.updated_at.asc())
        .limit(100)
    )

    dialect = session.get_bind().dialect.name
    if dialect in {"postgresql", "mysql"}:
        stmt = stmt.with_for_update(skip_locked=True)

    running_jobs = session.execute(stmt).scalars().all()
    recovered = 0
    for job in running_jobs:
        lock_expired = bool(job.lock_expires_at and job.lock_expires_at < now)
        telegram_soft_stale = bool(
            job.parser_type.value == "telegram"
            and job.updated_at
            and job.updated_at < telegram_stale_before
        )
        if not (lock_expired or telegram_soft_stale):
            continue
        job.status = JobStatus.retry
        job.run_after = now
        job.locked_by = None
        job.lock_expires_at = None
        if telegram_soft_stale:
            job.last_error = "Автовідновлення: задача зависла в running"
        elif not job.last_error:
            job.last_error = "Відновлено після перезапуску воркера"
        recovered += 1
    return recovered


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
    job_id = int(job.id)
    account_id = int(job.account_id) if job.account_id is not None else None
    parser_type_value = job.parser_type.value
    is_telegram_job = parser_type_value == "telegram"

    target = session.get(Target, job.target_id)
    if not target:
        _fail_job(session, job, "Target not found")
        return

    account = session.get(ParserAccount, job.account_id) if job.account_id else None
    plugin = plugin_registry.get(parser_type_value)
    started_at = dt.datetime.now(dt.UTC)
    print(
        f"[worker] start job#{job.id} key={job.job_key or '-'} "
        f"target={job.target_id} account={job.account_id or '-'} attempt={job.attempt}",
        flush=True,
    )

    try:
        events = plugin.run(session, job, target, account)
        max_telegram_message_id: int | None = None
        max_telegram_observed_at: dt.datetime | None = None
        external_ids = [event.external_id for event in events if event.external_id]
        existing_external_ids: set[str] = set()
        if external_ids:
            rows = (
                session.execute(
                    select(RawEvent.external_id).where(
                        RawEvent.parser_type == job.parser_type,
                        RawEvent.target_id == job.target_id,
                        RawEvent.external_id.in_(external_ids),
                    )
                )
                .scalars()
                .all()
            )
            existing_external_ids = {str(item) for item in rows if item}

        for event in events:
            if event.external_id and event.external_id in existing_external_ids:
                continue
            stored = object_store.put_json(
                parser_type=job.parser_type.value,
                target_id=job.target_id,
                payload=event.payload,
                external_id=event.external_id,
            )
            try:
                # Savepoint prevents a duplicate insert race from failing the whole job.
                with session.begin_nested():
                    session.add(
                        RawEvent(
                            parser_type=job.parser_type,
                            target_id=job.target_id,
                            account_id=job.account_id,
                            owner_user_id=job.owner_user_id if job.owner_user_id is not None else target.owner_user_id,
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
                    session.flush()
            except IntegrityError:
                continue
            if is_telegram_job:
                upsert_telegram_profile_from_event(
                    session=session,
                    target_id=job.target_id,
                    account_id=job.account_id,
                    payload=event.payload,
                    observed_at=event.observed_at,
                )
                payload = event.payload if isinstance(event.payload, dict) else {}
                if str(payload.get("event_type") or "") == "telegram_message":
                    raw_message_id = payload.get("message_id")
                    if raw_message_id is not None:
                        current_message_id = int(raw_message_id)
                        max_telegram_message_id = (
                            current_message_id
                            if max_telegram_message_id is None
                            else max(max_telegram_message_id, current_message_id)
                        )
                        if event.observed_at and (
                            max_telegram_observed_at is None or event.observed_at > max_telegram_observed_at
                        ):
                            max_telegram_observed_at = event.observed_at
            if event.external_id:
                existing_external_ids.add(event.external_id)
        if is_telegram_job and account_id is not None and max_telegram_message_id is not None:
            update_offset_from_message(
                session=session,
                target_id=job.target_id,
                account_id=account_id,
                message_id=max_telegram_message_id,
                observed_at=max_telegram_observed_at,
                source=str(job.payload.get("mode") or job.job_key or "worker"),
            )
        if account and is_telegram_job:
            _register_account_success(account)
        _complete_job(session, job)
        finished_at = dt.datetime.now(dt.UTC)
        duration = (finished_at - started_at).total_seconds()
        print(
            f"[worker] done job#{job.id} status=succeeded events={len(events)} "
            f"duration_sec={duration:.1f}",
            flush=True,
        )
    except Exception as exc:
        # Ensure session is usable after flush/DB errors.
        session.rollback()
        job = session.get(ParseJob, job_id)
        if not job:
            print(f"[worker] job#{job_id} disappeared after error: {exc}", flush=True)
            return
        if account_id is not None and is_telegram_job:
            account = session.get(ParserAccount, account_id)
            if account:
                _register_account_failure(account, str(exc))
        _fail_job(session, job, str(exc))
        finished_at = dt.datetime.now(dt.UTC)
        duration = (finished_at - started_at).total_seconds()
        print(
            f"[worker] done job#{job.id} status={job.status.value} "
            f"duration_sec={duration:.1f} error={str(exc)[:200]}",
            flush=True,
        )


def worker_loop(session_factory, worker_name: str) -> None:
    while True:
        with session_factory() as session:
            _recover_stale_running_jobs(session)
            job = _lock_next_job(session, worker_name)
            if not job:
                session.commit()
                time.sleep(settings.worker_poll_interval)
                continue

            # Persist job lock + running status before long processing,
            # so UI and other workers observe the real state immediately.
            session.commit()
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
