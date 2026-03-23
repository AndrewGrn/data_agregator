from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import get_settings
from app.models import OnboardingStatus, ParseJob, ParserAccount, ParserType, Target, TargetAccountLink
from app.plugins.base import JobSpec, ParsedEvent, ParserPlugin

settings = get_settings()


def _coerce_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return _coerce_utc(parsed)


class TelegramPlugin(ParserPlugin):
    parser_type = ParserType.telegram.value

    def _is_account_available(self, account: ParserAccount, now: dt.datetime) -> bool:
        if account.cooldown_until and account.cooldown_until > now:
            return False
        if account.health_score < 20:
            return False

        if account.hour_window_start and (now - account.hour_window_start) < dt.timedelta(hours=1):
            return account.hour_window_count < max(account.hourly_limit, 1)

        return True

    def _build_backfill_ranges(self, target: Target) -> list[tuple[dt.datetime, dt.datetime]]:
        config = target.config or {}
        backfill = config.get("backfill", {})
        if not backfill.get("enabled"):
            return []

        start = _parse_iso_datetime(backfill.get("from"))
        end = _parse_iso_datetime(backfill.get("to"))
        if not start or not end or end <= start:
            return []

        chunk_days = max(int(backfill.get("chunk_days", 7)), 1)
        step = dt.timedelta(days=chunk_days)

        ranges: list[tuple[dt.datetime, dt.datetime]] = []
        cursor = start
        while cursor < end:
            upper = min(cursor + step, end)
            ranges.append((cursor, upper))
            cursor = upper
        return ranges

    def generate_jobs(self, session: Session, target: Target) -> list[JobSpec]:
        rows = session.execute(
            select(TargetAccountLink, ParserAccount)
            .join(ParserAccount, TargetAccountLink.account_id == ParserAccount.id)
            .where(
                TargetAccountLink.target_id == target.id,
                TargetAccountLink.is_active.is_(True),
                ParserAccount.is_active.is_(True),
                ParserAccount.parser_type == ParserType.telegram,
            )
        ).all()

        if not rows:
            target.onboarding_status = OnboardingStatus.needs_account
            return []

        now = dt.datetime.now(dt.UTC)
        accounts = [acc for _, acc in rows if self._is_account_available(acc, now)]
        accounts.sort(key=lambda a: (-a.health_score, a.hour_window_count, a.id))

        if not accounts:
            target.onboarding_status = OnboardingStatus.blocked
            return []

        target.onboarding_status = OnboardingStatus.ready
        jobs: list[JobSpec] = []

        for account in accounts:
            jobs.append(
                JobSpec(
                    parser_type=self.parser_type,
                    target_id=target.id,
                    account_id=account.id,
                    job_key=f"poll:{target.id}:{account.id}",
                    payload={"identifier": target.identifier, "limit": target.config.get("limit", settings.telegram_fetch_limit)},
                )
            )

            for from_date, to_date in self._build_backfill_ranges(target):
                jobs.append(
                    JobSpec(
                        parser_type=self.parser_type,
                        target_id=target.id,
                        account_id=account.id,
                        job_key=f"backfill:{target.id}:{account.id}:{from_date.isoformat()}:{to_date.isoformat()}",
                        payload={
                            "identifier": target.identifier,
                            "limit": int(target.config.get("backfill_limit", 5000)),
                            "backfill": True,
                            "from_date": from_date.isoformat(),
                            "to_date": to_date.isoformat(),
                        },
                    )
                )

        return jobs

    async def _fetch_messages(
        self,
        account: ParserAccount,
        identifier: str,
        limit: int,
        from_date: dt.datetime | None = None,
        to_date: dt.datetime | None = None,
    ) -> list[dict[str, Any]]:
        creds = account.credentials or {}
        api_id = creds.get("api_id")
        api_hash = creds.get("api_hash")
        session_string = creds.get("session_string")

        if not (api_id and api_hash and session_string):
            raise ValueError(
                f"Telegram account '{account.label}' is missing api_id/api_hash/session_string. "
                "Use UI to update credentials."
            )

        client = TelegramClient(StringSession(session_string), int(api_id), str(api_hash))
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ValueError(f"Telegram account '{account.label}' is not authorized.")

        messages: list[dict[str, Any]] = []
        iter_kwargs: dict[str, Any] = {"limit": limit}
        if to_date:
            iter_kwargs["offset_date"] = to_date + dt.timedelta(seconds=1)

        async for msg in client.iter_messages(identifier, **iter_kwargs):
            msg_date = _coerce_utc(msg.date) if msg.date else None
            if from_date and msg_date and msg_date < from_date:
                break
            if to_date and msg_date and msg_date > to_date:
                continue

            sender = await msg.get_sender()
            messages.append(
                {
                    "message_id": msg.id,
                    "date": msg.date.isoformat() if msg.date else None,
                    "text": msg.message,
                    "chat_id": getattr(msg.peer_id, "channel_id", None)
                    or getattr(msg.peer_id, "chat_id", None)
                    or getattr(msg.peer_id, "user_id", None),
                    "sender": {
                        "id": getattr(sender, "id", None),
                        "username": getattr(sender, "username", None),
                        "first_name": getattr(sender, "first_name", None),
                        "last_name": getattr(sender, "last_name", None),
                    },
                    "raw": msg.to_dict(),
                }
            )

        await client.disconnect()
        return messages

    def run(self, session: Session, job: ParseJob, target: Target, account: ParserAccount | None) -> list[ParsedEvent]:
        if not account:
            raise ValueError("Telegram job requires account_id")

        identifier = job.payload.get("identifier", target.identifier)
        limit = int(job.payload.get("limit", settings.telegram_fetch_limit))
        from_date = _parse_iso_datetime(job.payload.get("from_date"))
        to_date = _parse_iso_datetime(job.payload.get("to_date"))

        messages = asyncio.run(self._fetch_messages(account, identifier, limit, from_date=from_date, to_date=to_date))

        events: list[ParsedEvent] = []
        for item in messages:
            observed_at = dt.datetime.fromisoformat(item["date"]) if item.get("date") else None
            events.append(
                ParsedEvent(
                    external_id=str(item.get("message_id")) if item.get("message_id") else None,
                    observed_at=observed_at,
                    payload=item,
                )
            )
        return events

    async def _check_target_access(self, account: ParserAccount, identifier: str) -> bool:
        creds = account.credentials or {}
        api_id = creds.get("api_id")
        api_hash = creds.get("api_hash")
        session_string = creds.get("session_string")
        if not (api_id and api_hash and session_string):
            return False

        client = TelegramClient(StringSession(session_string), int(api_id), str(api_hash))
        await client.connect()
        ok = False
        try:
            if not await client.is_user_authorized():
                return False
            await client.get_entity(identifier)
            ok = True
        except Exception:
            ok = False
        finally:
            await client.disconnect()
        return ok

    def sync_memberships(self, session: Session) -> dict:
        accounts = (
            session.execute(select(ParserAccount).where(ParserAccount.parser_type == ParserType.telegram, ParserAccount.is_active.is_(True)))
            .scalars()
            .all()
        )
        targets = (
            session.execute(select(Target).where(Target.parser_type == ParserType.telegram, Target.is_active.is_(True))).scalars().all()
        )

        checked = 0
        linked = 0
        now = dt.datetime.now(dt.UTC)

        for account in accounts:
            for target in targets:
                checked += 1
                can_access = asyncio.run(self._check_target_access(account, target.identifier))
                link = session.execute(
                    select(TargetAccountLink).where(
                        and_(TargetAccountLink.target_id == target.id, TargetAccountLink.account_id == account.id)
                    )
                ).scalar_one_or_none()

                if can_access:
                    if not link:
                        link = TargetAccountLink(
                            target_id=target.id,
                            account_id=account.id,
                            is_active=True,
                            auto_detected=True,
                            last_checked_at=now,
                        )
                        session.add(link)
                        linked += 1
                    else:
                        if not link.is_active:
                            linked += 1
                        link.is_active = True
                        link.auto_detected = True
                        link.last_checked_at = now
                elif link:
                    link.is_active = False
                    link.last_checked_at = now

        return {"checked": checked, "linked": linked}
