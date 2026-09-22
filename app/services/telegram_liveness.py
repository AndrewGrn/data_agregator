from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import OnboardingStatus, ParserAccount, ServiceState, Target, TargetAccountLink
from app.plugins.base import JobSpec
from app.services.scheduler import _enqueue_job_specs
from app.services.telegram_accounts import refresh_account_session_info_sync

logger = logging.getLogger(__name__)
settings = get_settings()

STATE_KEY = "telegram_liveness_last_run"
QUEUE_ONBOARD = "telegram_backfill"


def _last_run(session: Session) -> dt.datetime | None:
    state = session.get(ServiceState, STATE_KEY)
    raw = (state.value or {}).get("at") if state else None
    if not raw:
        return None
    parsed = dt.datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _mark_run(session: Session, now: dt.datetime) -> None:
    state = session.get(ServiceState, STATE_KEY)
    if state is None:
        session.add(ServiceState(key=STATE_KEY, value={"at": now.isoformat()}))
    else:
        state.value = {"at": now.isoformat()}


def failover_account(session: Session, account: ParserAccount, *, now: dt.datetime) -> int:
    """Detach a dead account from its targets. Shared pool → re-onboard elsewhere."""
    links = session.execute(
        select(TargetAccountLink).where(TargetAccountLink.account_id == account.id, TargetAccountLink.is_active.is_(True))
    ).scalars().all()
    moved = 0
    for link in links:
        link.is_active = False
        target = session.get(Target, link.target_id)
        if target is None:
            continue
        target.onboarding_status = OnboardingStatus.needs_account
        if not target.is_active:
            # Switched off by the user: detach, but do not spend a join on it.
            target.onboarding_step = "idle"
            continue
        if account.pool_mode == "shared":
            target.onboarding_step = "queued"
            target.onboarding_error = None
            _enqueue_job_specs(
                session,
                target,
                [
                    JobSpec(
                        parser_type="telegram",
                        target_id=target.id,
                        account_id=None,
                        job_key=f"onboard:{target.id}",
                        payload={"mode": "onboard", "allow_join": True},
                        priority=40,
                        queue=QUEUE_ONBOARD,
                        max_attempts=20,
                    )
                ],
            )
            moved += 1
        else:
            target.onboarding_step = "idle"
    logger.warning("failover account=%s pool_mode=%s detached=%s re-onboarded=%s", account.id, account.pool_mode, len(links), moved)
    return moved


def check_accounts_liveness(
    session: Session,
    *,
    now: dt.datetime,
    checker: Callable[[dict[str, Any]], dict[str, Any]] = refresh_account_session_info_sync,
    force: bool = False,
) -> dict[str, int]:
    """Probe every active Telegram account; two consecutive failures = dead."""
    result = {"checked": 0, "alive": 0, "dead": 0, "failed_over": 0}
    interval = dt.timedelta(seconds=int(settings.telegram_liveness_interval_seconds))
    last = _last_run(session)
    if not force and last is not None and (now - last) < interval:
        return result
    _mark_run(session, now)

    threshold = int(settings.telegram_liveness_failures_to_dead)
    accounts = session.execute(
        select(ParserAccount).where(ParserAccount.parser_type == "telegram", ParserAccount.is_active.is_(True))
    ).scalars().all()

    for account in accounts:
        result["checked"] += 1
        try:
            info = checker(dict(account.credentials or {}))
        except Exception as exc:  # probe itself blew up: treat as a failed check
            info = {"alive": False, "error": str(exc)}

        creds = dict(account.credentials or {})
        was_dead = account.alive is False and int(creds.get("liveness_failures", 0)) >= threshold
        account.last_checked_at = now

        if info.get("alive"):
            account.alive = True
            account.last_alive_at = now
            account.dead_reason = None
            creds["liveness_failures"] = 0
            account.credentials = creds
            result["alive"] += 1
            continue

        failures = int(creds.get("liveness_failures", 0)) + 1
        creds["liveness_failures"] = failures
        account.credentials = creds
        reason = str(info.get("error") or "unknown")[:2000]
        logger.warning("liveness account=%s failed (%s/%s): %s", account.id, failures, threshold, reason)
        # One blip must not black out the pool: alive=False takes every target
        # of every account out of the picker until the next probe.
        if failures >= threshold:
            account.alive = False
            account.dead_reason = reason

        if failures >= threshold and not was_dead:
            result["dead"] += 1
            result["failed_over"] += failover_account(session, account, now=now)

    return result
