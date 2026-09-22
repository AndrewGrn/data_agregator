from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import socket
import threading
import time
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from telethon.errors import RPCError

from app.config import get_settings
from app.models import JobStatus, ParseJob, ParserAccount, Target
from app.plugins.base import DeferJob
from app.plugins.registry import plugin_registry
from app.services.execution_queue import _connect, fetch_messages, pull_subscribe_queue
from app.services.job_routing import (
    QUEUE_DARKNET,
    QUEUE_TELEGRAM_BACKFILL,
    QUEUE_TELEGRAM_LIVE,
    QUEUE_WEB,
    QUEUE_WHATSAPP,
    resolve_job_queue,
)
from app.services.darknet_profiles import upsert_darknet_profile_from_event
from app.services.event_sink import persist_events
from app.services.telegram_accounts import account_parallel_limits
from app.services.telegram_offsets import update_offset_from_message
from app.services.telegram_profiles import upsert_telegram_profile_from_event

settings = get_settings()
logger = logging.getLogger(__name__)


def _effective_job_queue(job: ParseJob) -> str:
    raw = str(getattr(job, "queue", "") or "").strip().lower()
    if raw:
        return raw
    return resolve_job_queue(parser_type=job.parser_type, job_key=job.job_key)


def _job_lock_minutes(job: ParseJob) -> int:
    queue = _effective_job_queue(job)
    fetch_timeout_seconds = max(int(settings.telegram_fetch_timeout_seconds), 30)
    soft_minutes = max(5, (fetch_timeout_seconds // 60) + 2)
    backfill_minutes = max(10, soft_minutes * 2)
    if queue == QUEUE_TELEGRAM_LIVE:
        return max(int(settings.worker_telegram_live_lock_minutes), soft_minutes)
    if queue == QUEUE_TELEGRAM_BACKFILL:
        return max(int(settings.worker_telegram_backfill_lock_minutes), backfill_minutes)
    if queue == QUEUE_DARKNET:
        return max(int(settings.worker_darknet_lock_minutes), 5)
    if queue == QUEUE_WEB:
        return max(int(settings.worker_web_lock_minutes), 5)
    return 15


def _job_parallel_limit(job: ParseJob, account: ParserAccount | None) -> int:
    queue = _effective_job_queue(job)
    if account and account.parser_type == "telegram":
        parallel_jobs, backfill_parallel_jobs = account_parallel_limits(
            account=account,
            default_parallel_jobs=settings.telegram_parallel_jobs_per_account,
            default_backfill_parallel_jobs=settings.telegram_backfill_parallel_jobs_per_account,
        )
    else:
        parallel_jobs, backfill_parallel_jobs = max(int(settings.darknet_parallel_jobs_per_account), 1), 1
    if queue == QUEUE_TELEGRAM_LIVE:
        return parallel_jobs
    if queue == QUEUE_TELEGRAM_BACKFILL:
        return backfill_parallel_jobs
    if queue == QUEUE_DARKNET:
        return max(int(settings.darknet_parallel_jobs_per_account), 1)
    if queue == QUEUE_WEB:
        return max(int(settings.web_parallel_jobs_per_account), 1)
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


def _lock_next_job(session: Session, worker_id: str, allowed_queues: set[str] | None = None) -> ParseJob | None:
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
    if allowed_queues:
        stmt = stmt.where(ParseJob.queue.in_(allowed_queues))

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


def _lock_job_by_id(
    session: Session,
    worker_id: str,
    job_id: int,
    allowed_queues: set[str] | None = None,
) -> ParseJob | None:
    now = dt.datetime.now(dt.UTC)
    stmt = select(ParseJob).where(
        ParseJob.id == int(job_id),
        ParseJob.status.in_([JobStatus.pending, JobStatus.retry]),
        ParseJob.run_after <= now,
        or_(ParseJob.lock_expires_at.is_(None), ParseJob.lock_expires_at < now),
    )
    if allowed_queues:
        stmt = stmt.where(ParseJob.queue.in_(allowed_queues))

    dialect = session.get_bind().dialect.name
    if dialect in {"postgresql", "mysql"}:
        stmt = stmt.with_for_update(skip_locked=True)

    candidate = session.execute(stmt).scalar_one_or_none()
    if candidate is None:
        return None

    if candidate.account_id is not None:
        account_stmt = select(ParserAccount).where(ParserAccount.id == candidate.account_id)
        if dialect in {"postgresql", "mysql"}:
            account_stmt = account_stmt.with_for_update(skip_locked=True)
        account_row = session.execute(account_stmt).scalar_one_or_none()
        if account_row is None:
            return None
        running_count = _running_jobs_for_account(
            session=session,
            account_id=int(candidate.account_id),
            now=now,
            excluding_job_id=int(candidate.id),
        )
        if running_count >= _job_parallel_limit(candidate, account_row):
            return None

    candidate.status = JobStatus.running
    candidate.locked_by = worker_id
    candidate.lock_expires_at = now + dt.timedelta(minutes=_job_lock_minutes(candidate))
    candidate.attempt += 1
    return candidate


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
            job.parser_type == "telegram"
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
    job.last_error = None
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


def _is_telegram_side_error(exc: BaseException) -> bool:
    """Only Telegram's own verdicts (RPC errors, dropped connections) say
    anything about the account. Our fetch budget expiring (TimeoutError) or a
    row Postgres refused (SQLAlchemyError) are bugs in this codebase, and
    penalising the account for them put a healthy session on a 30-minute
    cooldown twice in the first live hour."""
    return isinstance(exc, (RPCError, ConnectionError, OSError)) and not isinstance(exc, TimeoutError) \
        and not isinstance(exc, SQLAlchemyError)


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
        queue = _effective_job_queue(job)
        if queue == QUEUE_TELEGRAM_LIVE:
            max_backoff = max(int(settings.worker_telegram_live_backoff_max_seconds), 1)
        elif queue == QUEUE_TELEGRAM_BACKFILL:
            max_backoff = max(int(settings.worker_telegram_backfill_backoff_max_seconds), 1)
        elif queue == QUEUE_DARKNET:
            max_backoff = max(int(settings.worker_darknet_backoff_max_seconds), 1)
        elif queue == QUEUE_WEB:
            max_backoff = max(int(settings.worker_web_backoff_max_seconds), 1)
        else:
            max_backoff = 300
        backoff = min(max_backoff, 2 ** job.attempt)
        job.run_after = now + dt.timedelta(seconds=backoff)
    job.last_error = error[:2000]
    job.locked_by = None
    job.lock_expires_at = None

    # A terminally failed onboarding otherwise leaves the target stuck showing
    # "queued" forever: every non-terminal exception rolled its step back.
    if job.status == JobStatus.failed and str(job.job_key or "").startswith("onboard:"):
        target = session.get(Target, job.target_id)
        if target is not None:
            target.onboarding_step = "failed"
            target.onboarding_error = error[:2000]


def _commit_job(session: Session, job_id: int) -> None:
    """Commit a processed job; a flush error here fails the job, not the worker.

    The job body's own try/except is already over by this point, so an
    IntegrityError raised at flush time would otherwise kill the worker thread
    and leave the job `running` for the next thread to pick up and die on.
    """
    try:
        session.commit()
        return
    except Exception as exc:
        logger.exception("commit failed for job#%s: %s", job_id, exc)
        error = str(exc)
    session.rollback()
    job = session.get(ParseJob, job_id)
    if job:
        _fail_job(session, job, f"commit failed: {error}")
        session.commit()


def _process_job(session: Session, job: ParseJob) -> None:
    job_id = int(job.id)
    account_id = int(job.account_id) if job.account_id is not None else None
    parser_type_value = job.parser_type
    is_telegram_job = parser_type_value == "telegram"
    is_darknet_job = parser_type_value == "darknet"

    target = session.get(Target, job.target_id)
    if not target:
        _fail_job(session, job, "Target not found")
        return

    account = session.get(ParserAccount, job.account_id) if job.account_id else None
    plugin = plugin_registry.get(parser_type_value)
    started_at = dt.datetime.now(dt.UTC)
    logger.info(
        f"start job#{job.id} key={job.job_key or '-'} "
        f"queue={_effective_job_queue(job)} target={job.target_id} "
        f"account={job.account_id or '-'} attempt={job.attempt}"
    )

    try:
        events = plugin.run(session, job, target, account)
        max_telegram_message_id: int | None = None
        max_telegram_observed_at: dt.datetime | None = None
        written = persist_events(
            session,
            target=target,
            parser_type=job.parser_type,
            account_id=job.account_id,
            owner_user_id=job.owner_user_id,
            events=events,
        )
        # Only rows persist_events actually inserted count as new. Discarding on
        # match keeps a duplicate external_id repeated inside one batch from
        # being treated as new twice.
        newly_written_ids = {str(row.external_id) for row in written if row.external_id}

        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            external_id = str(event.external_id) if event.external_id else ""
            if external_id:
                is_new = external_id in newly_written_ids
                newly_written_ids.discard(external_id)
            else:
                is_new = True
            if is_darknet_job:
                upsert_darknet_profile_from_event(
                    session=session,
                    target_id=job.target_id,
                    target_identifier=str(target.identifier or ""),
                    account_id=job.account_id,
                    payload=payload,
                    observed_at=event.observed_at,
                    increment_post_counter=is_new,
                )
            elif is_telegram_job and is_new:
                upsert_telegram_profile_from_event(
                    session=session,
                    target_id=job.target_id,
                    account_id=job.account_id,
                    payload=event.payload,
                    observed_at=event.observed_at,
                )
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
        logger.info(
            f"done job#{job.id} status=succeeded events={len(events)} "
            f"duration_sec={duration:.1f}"
        )
    except DeferJob as defer:
        # No rollback: a defer is control flow, not a DB error. Whatever the
        # plugin wrote before deferring (FloodWait cooldown, channels_full)
        # is precisely the state that must survive, or the flooded account is
        # handed straight to the next queued target.
        now = dt.datetime.now(dt.UTC)
        job.status = JobStatus.retry
        job.run_after = now + dt.timedelta(seconds=defer.seconds)
        # The claim already counted this attempt; a deferral is not a failure.
        job.attempt = max(int(job.attempt or 0) - 1, 0)
        job.last_error = defer.reason[:2000]
        job.locked_by = None
        job.lock_expires_at = None
        logger.info("defer job#%s for %ss: %s", job.id, defer.seconds, defer.reason)
        return
    except Exception as exc:
        # Ensure session is usable after flush/DB errors.
        session.rollback()
        job = session.get(ParseJob, job_id)
        if not job:
            logger.error(f"job#{job_id} disappeared after error: {exc}", exc_info=True)
            return
        if account_id is not None and is_telegram_job and _is_telegram_side_error(exc):
            account = session.get(ParserAccount, account_id)
            if account:
                _register_account_failure(account, str(exc))
        _fail_job(session, job, str(exc))
        finished_at = dt.datetime.now(dt.UTC)
        duration = (finished_at - started_at).total_seconds()
        logger.exception(
            f"done job#{job.id} status={job.status.value} "
            f"duration_sec={duration:.1f} error={str(exc)[:200]}"
        )


def worker_loop(session_factory, worker_name: str, allowed_queues: set[str] | None = None) -> None:
    while True:
        with session_factory() as session:
            _recover_stale_running_jobs(session)
            job = _lock_next_job(session, worker_name, allowed_queues=allowed_queues)
            if not job:
                session.commit()
                time.sleep(settings.worker_poll_interval)
                continue

            # Persist job lock + running status before long processing,
            # so UI and other workers observe the real state immediately.
            session.commit()
            job_id = int(job.id)
            _process_job(session, job)
            _commit_job(session, job_id)


def run_workers(session_factory, concurrency: int, queues: set[str] | None = None) -> None:
    if settings.nats_enabled:
        asyncio.run(run_workers_nats(session_factory, concurrency, queues))
        return

    hostname = socket.gethostname()
    threads: list[threading.Thread] = []
    allowed_queues: set[str] | None = None
    if queues:
        allowed_queues = {str(item).strip().lower() for item in queues if str(item).strip()}
    queue_label = ",".join(sorted(allowed_queues)) if allowed_queues else "all"
    logger.info(f"starting pool concurrency={concurrency} queues={queue_label}")

    for idx in range(concurrency):
        name = f"{hostname}-w{idx}"
        thread = threading.Thread(target=worker_loop, args=(session_factory, name, allowed_queues), daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()


async def _consume_queue_loop(
    session_factory,
    queue_name: str,
    subscription,
    worker_prefix: str,
    allowed_queues: set[str],
    semaphore: asyncio.Semaphore,
) -> None:
    batch_size = max(int(settings.nats_fetch_batch_size), 1)
    timeout_seconds = max(int(settings.nats_fetch_timeout_seconds), 1)
    inflight: set[asyncio.Task] = set()

    async def _handle_message(msg) -> None:
        try:
            payload = json.loads(msg.data.decode("utf-8")) if msg.data else {}
            job_id = int(payload.get("job_id"))
        except Exception:
            await msg.ack()
            return

        try:
            async with semaphore:
                await asyncio.to_thread(
                    _process_delivery_sync,
                    session_factory,
                    worker_prefix,
                    queue_name,
                    job_id,
                    allowed_queues,
                )
        finally:
            await msg.ack()

    while True:
        messages = await fetch_messages(subscription, batch=batch_size, timeout_seconds=timeout_seconds)
        if not messages:
            if inflight:
                done, _ = await asyncio.wait(inflight, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    inflight.discard(task)
            await asyncio.sleep(0.1)
            continue

        for msg in messages:
            task = asyncio.create_task(_handle_message(msg))
            inflight.add(task)
            task.add_done_callback(inflight.discard)

        # Keep memory bounded while still allowing long-running jobs and fresh deliveries in parallel.
        max_inflight = batch_size * 8
        if len(inflight) >= max_inflight:
            done, _ = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                inflight.discard(task)


def _process_delivery_sync(
    session_factory,
    worker_prefix: str,
    queue_name: str,
    job_id: int,
    allowed_queues: set[str],
) -> None:
    worker_id = f"{worker_prefix}-{queue_name}-{uuid.uuid4().hex[:8]}"
    with session_factory() as session:
        _recover_stale_running_jobs(session)
        job = _lock_job_by_id(
            session=session,
            worker_id=worker_id,
            job_id=job_id,
            allowed_queues=allowed_queues,
        )
        if not job:
            session.commit()
            return
        session.commit()
        _process_job(session, job)
        _commit_job(session, int(job_id))


async def run_workers_nats(session_factory, concurrency: int, queues: set[str] | None = None) -> None:
    hostname = socket.gethostname()
    allowed_queues: set[str]
    if queues:
        allowed_queues = {str(item).strip().lower() for item in queues if str(item).strip()}
    else:
        allowed_queues = {QUEUE_TELEGRAM_LIVE, QUEUE_TELEGRAM_BACKFILL, QUEUE_DARKNET, QUEUE_WEB, QUEUE_WHATSAPP}
    queue_label = ",".join(sorted(allowed_queues))
    worker_slots = max(int(concurrency), 1)
    logger.info(f"starting JetStream consumer concurrency={worker_slots} queues={queue_label}")
    semaphore = asyncio.Semaphore(worker_slots)

    nc, js = await _connect()
    try:
        tasks: list[asyncio.Task] = []
        worker_prefix = f"{hostname}"
        for queue_name in sorted(allowed_queues):
            durable = f"worker_{queue_name}"
            subscription = await pull_subscribe_queue(js=js, queue=queue_name, durable=durable)
            tasks.append(
                asyncio.create_task(
                    _consume_queue_loop(
                        session_factory=session_factory,
                        queue_name=queue_name,
                        subscription=subscription,
                        worker_prefix=worker_prefix,
                        allowed_queues=allowed_queues,
                        semaphore=semaphore,
                    )
                )
            )
        await asyncio.gather(*tasks)
    finally:
        await nc.close()
