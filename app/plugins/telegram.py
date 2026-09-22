from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetForumTopicsRequest

from app.config import get_settings
from app.models import OnboardingStatus, ParseJob, ParserAccount, ParserType, Target, TargetAccountLink, TelegramOffset
from app.plugins.base import FileRef, JobSpec, ParsedEvent, ParserPlugin
from app.services.object_store import object_store
from app.services.telegram_offsets import update_gapfill_state

logger = logging.getLogger(__name__)

settings = get_settings()


def _coerce_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = dt.datetime.fromisoformat(normalized)
    return _coerce_utc(parsed)


def _entity_ref(identifier: str | int) -> str | int:
    raw = str(identifier or "").strip()
    if raw and raw.lstrip("-").isdigit():
        try:
            return int(raw)
        except Exception:
            return raw
    return raw


_EVENT_KINDS = {"telegram_message": "message", "telegram_comment": "comment"}


def _message_topic_id(msg: Any) -> int | None:
    """Return the forum topic root id a message belongs to, or None.

    Telethon marks a topic message via ``msg.reply_to.forum_topic``. The
    topic's identity is ``reply_to.reply_to_top_id`` — except for the
    topic's own root message, which has no ``reply_to_top_id`` (it isn't a
    reply to anything) and is its own identity.
    """
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is not None and getattr(reply_to, "forum_topic", False):
        return int(getattr(reply_to, "reply_to_top_id", None) or msg.id)
    return None


def _use_aggressive_participants(limit: int, threshold: int, enabled: bool) -> bool:
    return bool(enabled) and int(limit) > int(threshold)


def run_onboard_job(session: Session, job: ParseJob, target: Target) -> None:
    """Proxy to app.services.telegram_onboarding.run_onboard_job.

    Imported lazily inside the call (not at module scope) to avoid a circular
    import: scheduler -> plugin_registry -> telegram -> telegram_onboarding ->
    scheduler._enqueue_job_specs. Exposed as a module-level name (rather than a
    local import inside TelegramPlugin.run) so it stays monkeypatchable in tests.
    """
    from app.services.telegram_onboarding import run_onboard_job as _impl

    return _impl(session, job, target)


def normalize_telegram_payload(item: dict) -> dict:
    """Map a Telegram message dict onto the shared ParsedEvent fields."""
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}

    sender_id = sender.get("id")
    author_id = str(sender_id) if sender_id is not None else None

    username = str(sender.get("username") or "").strip().removeprefix("@")
    full_name = " ".join(
        part for part in [
            str(sender.get("first_name") or "").strip(),
            str(sender.get("last_name") or "").strip(),
        ] if part
    ).strip()
    if username:
        author_label = f"@{username}"
    elif full_name:
        author_label = full_name
    elif author_id:
        author_label = f"ID {author_id}"
    else:
        author_label = None

    event_type = str(item.get("event_type") or "")
    is_comment = event_type == "telegram_comment"
    root_post_id = item.get("root_post_id")
    parent_message_id = item.get("parent_message_id")
    chat_id = item.get("chat_id")
    topic_id = item.get("topic_id")

    if is_comment and root_post_id is not None:
        thread_id = str(root_post_id)
    elif topic_id is not None:
        thread_id = str(topic_id)
    elif chat_id is not None:
        thread_id = str(chat_id)
    else:
        thread_id = None

    return {
        "text": str(item.get("text") or "") or None,
        "author_id": author_id,
        "author_label": author_label,
        # Unknown types pass through unchanged (as darknet does), so
        # telegram_participant is not silently filed as a message.
        "event_kind": _EVENT_KINDS.get(event_type, event_type or "message"),
        "thread_id": thread_id,
        "reply_to": str(parent_message_id) if parent_message_id is not None else None,
    }


async def _download_media(msg: Any, max_bytes: int) -> FileRef | None:
    """Download a message attachment into S3, returning its reference.

    Returns None when there is no media, it exceeds the limit, or the object
    store is unavailable — the event is still saved, just without the file.
    """
    if max_bytes <= 0 or not getattr(msg, "media", None):
        return None

    file_obj = getattr(msg, "file", None)
    size = getattr(file_obj, "size", None)
    if size is not None and int(size) > max_bytes:
        return None

    try:
        data = await msg.download_media(file=bytes)
    except Exception:
        logger.exception("telegram download_media failed for message %s", getattr(msg, "id", None))
        return None
    if not data or len(data) > max_bytes:
        return None

    stored = object_store.put_bytes(
        data,
        mime=getattr(file_obj, "mime_type", None),
        filename=getattr(file_obj, "name", None),
    )
    if stored is None:
        return None

    return FileRef(
        source_ref=str(msg.id),
        filename=stored.filename,
        mime=stored.mime,
        size=stored.size,
        sha256=stored.sha256,
    )


def _file_entries(file_ref: FileRef | None) -> list[dict[str, Any]]:
    if file_ref is None:
        return []
    return [
        {
            "source_ref": file_ref.source_ref,
            "filename": file_ref.filename,
            "mime": file_ref.mime,
            "size": file_ref.size,
            "sha256": file_ref.sha256,
        }
    ]


class TelegramPlugin(ParserPlugin):
    parser_type = ParserType.telegram.value

    def _backfill_mode(self, target: Target) -> str:
        backfill = dict((target.config or {}).get("backfill") or {})
        mode = str(backfill.get("mode") or "range").strip().lower()
        if mode not in {"range", "full"}:
            return "range"
        return mode

    def _is_account_available(self, account: ParserAccount, now: dt.datetime) -> bool:
        if account.alive is False:
            return False
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
        if self._backfill_mode(target) != "range":
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

    def _full_backfill_state(self, target: Target, account_id: int) -> dict:
        config = dict(target.config or {})
        backfill = dict(config.get("backfill") or {})
        progress = dict(backfill.get("full_progress") or {})
        state = dict(progress.get(str(account_id)) or {})
        return state

    def _update_full_backfill_state(
        self,
        target: Target,
        account_id: int,
        requested_limit: int,
        fetched_root_count: int,
        oldest_root_message_id: int | None,
    ) -> None:
        config = dict(target.config or {})
        backfill = dict(config.get("backfill") or {})
        progress = dict(backfill.get("full_progress") or {})
        state = dict(progress.get(str(account_id)) or {})

        done = fetched_root_count <= 0 or oldest_root_message_id is None or fetched_root_count < max(int(requested_limit), 1)
        state["done"] = bool(done)
        state["last_batch_count"] = int(max(fetched_root_count, 0))
        state["last_oldest_message_id"] = int(oldest_root_message_id) if oldest_root_message_id else None
        state["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
        if done:
            state["next_offset_id"] = None
        else:
            state["next_offset_id"] = int(oldest_root_message_id)

        progress[str(account_id)] = state
        backfill["full_progress"] = progress
        config["backfill"] = backfill
        target.config = config

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

        quiet_raw = (target.config or {}).get("quiet_until")
        quiet_until = _parse_iso_datetime(quiet_raw) if quiet_raw else None
        if quiet_until and quiet_until > dt.datetime.now(dt.UTC):
            return []

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
        config = dict(target.config or {})
        poll_interval_seconds = max(int(config.get("poll_interval_seconds", 300)), 30)
        participants_unavailable = bool(config.get("participants_unavailable"))
        participants_sync_enabled = bool(config.get("participants_sync_enabled", True)) and not participants_unavailable
        participants_sync_interval_seconds = max(int(config.get("participants_sync_interval_seconds", 3600)), 300)
        participants_limit = max(int(config.get("participants_limit", 1000)), 1)
        participants_aggressive_enabled = bool(config.get("participants_aggressive_enabled", True))
        participants_aggressive_threshold = max(int(config.get("participants_aggressive_threshold", 2000)), 1)
        live_enabled = bool(config.get("live_enabled", True))
        gapfill_limit = max(int(config.get("gapfill_limit", config.get("limit", settings.telegram_fetch_limit))), 1)
        backfill_mode = self._backfill_mode(target)
        backfill_enabled = bool((config.get("backfill") or {}).get("enabled"))
        account_ids = [account.id for account in accounts]
        offsets_by_account: dict[int, TelegramOffset] = {}
        if account_ids:
            offsets = (
                session.execute(
                    select(TelegramOffset).where(
                        TelegramOffset.target_id == target.id,
                        TelegramOffset.account_id.in_(account_ids),
                    )
                )
                .scalars()
                .all()
            )
            offsets_by_account = {int(offset.account_id): offset for offset in offsets}

        for account in accounts:
            full_backfill_active = False
            full_backfill_state: dict[str, Any] = {}
            if backfill_enabled and backfill_mode == "full":
                full_backfill_state = self._full_backfill_state(target, account.id)
                full_backfill_active = not bool(full_backfill_state.get("done"))

            if full_backfill_active:
                next_offset_id = int(full_backfill_state.get("next_offset_id") or 0)
                full_batch_size = max(int(target.config.get("backfill_full_batch_size", 300)), 1)
                configured_limit = max(int(target.config.get("backfill_limit", 5000)), 1)
                effective_limit = min(configured_limit, full_batch_size)
                jobs.append(
                    JobSpec(
                        parser_type=self.parser_type,
                        target_id=target.id,
                        account_id=account.id,
                        job_key=f"backfill-full:{target.id}:{account.id}:{next_offset_id}",
                        payload={
                            "identifier": target.identifier,
                            "limit": effective_limit,
                            "backfill": True,
                            "backfill_mode": "full",
                            "offset_id": next_offset_id,
                        },
                        priority=240,
                    )
                )
                # During full backfill we intentionally do not run poll/participants for the same account.
                continue

            if live_enabled:
                offset_row = offsets_by_account.get(account.id)
                min_id = int(offset_row.max_message_id) if offset_row else 0
                jobs.append(
                    JobSpec(
                        parser_type=self.parser_type,
                        target_id=target.id,
                        account_id=account.id,
                        job_key=f"gapfill:{target.id}:{account.id}",
                        payload={
                            "mode": "gap_fill",
                            "identifier": target.identifier,
                            "limit": gapfill_limit,
                            "min_id": min_id,
                            "min_interval_seconds": poll_interval_seconds,
                        },
                        priority=80,
                    )
                )
            else:
                jobs.append(
                    JobSpec(
                        parser_type=self.parser_type,
                        target_id=target.id,
                        account_id=account.id,
                        job_key=f"poll:{target.id}:{account.id}",
                        payload={
                            "identifier": target.identifier,
                            "limit": config.get("limit", settings.telegram_fetch_limit),
                            "min_interval_seconds": poll_interval_seconds,
                        },
                        priority=140,
                    )
                )

            if participants_sync_enabled:
                jobs.append(
                    JobSpec(
                        parser_type=self.parser_type,
                        target_id=target.id,
                        account_id=account.id,
                        job_key=f"participants:{target.id}:{account.id}",
                        payload={
                            "mode": "participants_sync",
                            "identifier": target.identifier,
                            "participants_limit": participants_limit,
                            "participants_aggressive_enabled": participants_aggressive_enabled,
                            "participants_aggressive_threshold": participants_aggressive_threshold,
                            "min_interval_seconds": participants_sync_interval_seconds,
                        },
                        priority=220,
                    )
                )

            if not (backfill_enabled and backfill_mode == "full"):
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
                                "backfill_mode": "range",
                                "from_date": from_date.isoformat(),
                                "to_date": to_date.isoformat(),
                            },
                            priority=260,
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
        min_id: int = 0,
        offset_id: int = 0,
        comments_enabled: bool = False,
        comments_limit: int = 50,
        comments_depth: int = 2,
        comments_recheck_posts: int = 100,
        media_enabled: bool = False,
    ) -> tuple[list[dict[str, Any]], int, int | None]:
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
        try:
            if not await client.is_user_authorized():
                raise ValueError(f"Telegram account '{account.label}' is not authorized.")
            entity = _entity_ref(identifier)

            topic_titles: dict[int, str] = {}
            try:
                full_entity = await client.get_entity(entity)
            except Exception:
                full_entity = None
            if full_entity is not None and getattr(full_entity, "forum", False):
                try:
                    # ponytail: single page (Telegram's own per-call cap), good
                    # enough for the vast majority of forums; paginate with
                    # offset_topic/offset_id if a target ever has >100 topics.
                    topics_result = await client(
                        GetForumTopicsRequest(
                            channel=full_entity,
                            offset_date=None,
                            offset_id=0,
                            offset_topic=0,
                            limit=100,
                        )
                    )
                    topic_titles = {int(t.id): str(t.title) for t in getattr(topics_result, "topics", [])}
                except Exception:
                    logger.exception("telegram forum topics fetch failed for %s", identifier)

            # Media is opt-in per channel (config.media_enabled): downloading every
            # photo and video inline made a 300-post batch take 15+ minutes.
            max_bytes = int(get_settings().media_max_bytes) if media_enabled else 0
            messages: list[dict[str, Any]] = []
            root_post_ids: list[int] = []
            root_count = 0
            oldest_root_message_id: int | None = None
            root_comments_sample_count = max(int(comments_recheck_posts), 1)
            iter_kwargs: dict[str, Any] = {"limit": limit}
            if to_date:
                iter_kwargs["offset_date"] = to_date + dt.timedelta(seconds=1)
            if min_id > 0:
                iter_kwargs["min_id"] = int(min_id)
            if offset_id > 0:
                iter_kwargs["offset_id"] = int(offset_id)

            async for msg in client.iter_messages(entity, **iter_kwargs):
                msg_date = _coerce_utc(msg.date) if msg.date else None
                if from_date and msg_date and msg_date < from_date:
                    break
                if to_date and msg_date and msg_date > to_date:
                    continue

                sender = await msg.get_sender()
                chat_id = (
                    getattr(msg.peer_id, "channel_id", None)
                    or getattr(msg.peer_id, "chat_id", None)
                    or getattr(msg.peer_id, "user_id", None)
                )
                root_count += 1
                msg_id = int(msg.id)
                if oldest_root_message_id is None or msg_id < oldest_root_message_id:
                    oldest_root_message_id = msg_id
                file_ref = await _download_media(msg, max_bytes)
                topic_id = _message_topic_id(msg)
                messages.append(
                    {
                        "event_type": "telegram_message",
                        "message_id": msg_id,
                        "date": msg.date.isoformat() if msg.date else None,
                        "text": msg.message,
                        "chat_id": chat_id,
                        "topic_id": topic_id,
                        "topic_title": topic_titles.get(topic_id) if topic_id is not None else None,
                        "sender": {
                            "id": getattr(sender, "id", None),
                            "username": getattr(sender, "username", None),
                            "first_name": getattr(sender, "first_name", None),
                            "last_name": getattr(sender, "last_name", None),
                        },
                        "files": _file_entries(file_ref),
                        "raw": msg.to_dict(),
                    }
                )
                if len(root_post_ids) < root_comments_sample_count:
                    root_post_ids.append(msg_id)

            if comments_enabled:
                recheck_limit = max(int(comments_recheck_posts), 1)
                known_root_ids = set(root_post_ids)
                if recheck_limit > len(root_post_ids):
                    async for root_msg in client.iter_messages(entity, limit=recheck_limit):
                        known_root_ids.add(int(root_msg.id))

                queue: list[tuple[int, int, int | None]] = [(root_id, 0, None) for root_id in known_root_ids]
                visited: set[tuple[int, int]] = set()
                while queue:
                    parent_id, depth, root_id = queue.pop(0)
                    root_post_id = root_id if root_id is not None else parent_id
                    if (parent_id, depth) in visited:
                        continue
                    visited.add((parent_id, depth))

                    try:
                        comments = []
                        async for cmt in client.iter_messages(entity, reply_to=parent_id, limit=max(int(comments_limit), 1)):
                            comments.append(cmt)
                    except Exception:
                        continue

                    for cmt in comments:
                        sender = await cmt.get_sender()
                        cmt_chat_id = (
                            getattr(cmt.peer_id, "channel_id", None)
                            or getattr(cmt.peer_id, "chat_id", None)
                            or getattr(cmt.peer_id, "user_id", None)
                        )
                        cmt_file_ref = await _download_media(cmt, max_bytes)
                        cmt_topic_id = _message_topic_id(cmt)
                        messages.append(
                            {
                                "event_type": "telegram_comment",
                                "message_id": cmt.id,
                                "root_post_id": root_post_id,
                                "parent_message_id": parent_id,
                                "depth": depth + 1,
                                "date": cmt.date.isoformat() if cmt.date else None,
                                "text": cmt.message,
                                "chat_id": cmt_chat_id,
                                "topic_id": cmt_topic_id,
                                "topic_title": topic_titles.get(cmt_topic_id) if cmt_topic_id is not None else None,
                                "sender": {
                                    "id": getattr(sender, "id", None),
                                    "username": getattr(sender, "username", None),
                                    "first_name": getattr(sender, "first_name", None),
                                    "last_name": getattr(sender, "last_name", None),
                                },
                                "files": _file_entries(cmt_file_ref),
                                "raw": cmt.to_dict(),
                            }
                        )

                        if (depth + 1) < max(int(comments_depth), 1):
                            queue.append((int(cmt.id), depth + 1, root_post_id))

            return messages, root_count, oldest_root_message_id
        finally:
            await client.disconnect()

    def _participant_status(self, participant_obj) -> tuple[str, bool, dt.datetime | None]:
        if participant_obj is None:
            return "participant_member", True, None

        class_name = participant_obj.__class__.__name__.lower()
        joined_at_raw = getattr(participant_obj, "date", None)
        joined_at = _coerce_utc(joined_at_raw) if joined_at_raw else None

        if "creator" in class_name:
            return "participant_creator", True, joined_at
        if "admin" in class_name:
            return "participant_admin", True, joined_at
        if "banned" in class_name:
            return "participant_banned", False, joined_at
        if "left" in class_name:
            return "participant_left", False, joined_at
        if "restricted" in class_name:
            left_flag = bool(getattr(participant_obj, "left", False))
            return "participant_restricted", not left_flag, joined_at
        return "participant_member", True, joined_at

    async def _fetch_participants(
        self,
        account: ParserAccount,
        identifier: str,
        limit: int,
        aggressive_enabled: bool = True,
        aggressive_threshold: int = 2000,
    ) -> list[dict[str, Any]]:
        creds = account.credentials or {}
        api_id = creds.get("api_id")
        api_hash = creds.get("api_hash")
        session_string = creds.get("session_string")

        if not (api_id and api_hash and session_string):
            return []

        client = TelegramClient(StringSession(session_string), int(api_id), str(api_hash))
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return []

        payloads: list[dict[str, Any]] = []
        observed_at = dt.datetime.now(dt.UTC)

        try:
            entity = await client.get_entity(_entity_ref(identifier))
            entity_id = getattr(entity, "id", None)
            chat_id = int(entity_id) if entity_id is not None else None
            if chat_id and chat_id > 0 and (getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)):
                chat_id = -1000000000000 + chat_id

            use_aggressive = _use_aggressive_participants(limit, aggressive_threshold, aggressive_enabled)
            async for participant in client.iter_participants(
                entity, limit=max(int(limit), 1), aggressive=use_aggressive
            ):
                membership_obj = getattr(participant, "participant", None)
                membership_status, membership_is_active, joined_at = self._participant_status(membership_obj)
                payloads.append(
                    {
                        "event_type": "telegram_participant",
                        "date": observed_at.isoformat(),
                        "chat_id": chat_id,
                        "identifier": identifier,
                        "membership_status": membership_status,
                        "membership_is_active": membership_is_active,
                        "joined_at": joined_at.isoformat() if joined_at else None,
                        "sender": {
                            "id": getattr(participant, "id", None),
                            "username": getattr(participant, "username", None),
                            "first_name": getattr(participant, "first_name", None),
                            "last_name": getattr(participant, "last_name", None),
                            "phone": getattr(participant, "phone", None),
                            "is_bot": bool(getattr(participant, "bot", False)),
                            "is_verified": bool(getattr(participant, "verified", False)),
                            "is_scam": bool(getattr(participant, "scam", False)),
                            "is_fake": bool(getattr(participant, "fake", False)),
                            "is_deleted": bool(getattr(participant, "deleted", False)),
                        },
                        "raw": participant.to_dict(),
                    }
                )
        except ChatAdminRequiredError:
            raise
        except Exception:
            payloads = []
        finally:
            await client.disconnect()

        return payloads

    def run(self, session: Session, job: ParseJob, target: Target, account: ParserAccount | None) -> list[ParsedEvent]:
        mode = str(job.payload.get("mode") or "poll")
        if mode == "onboard":
            run_onboard_job(session, job, target)
            return []

        if not account:
            raise ValueError("Telegram job requires account_id")

        identifier = job.payload.get("identifier", target.identifier)
        is_backfill = bool(job.payload.get("backfill"))
        backfill_mode = str(job.payload.get("backfill_mode") or "").strip().lower()
        limit = int(job.payload.get("limit", settings.telegram_fetch_limit))
        from_date = _parse_iso_datetime(job.payload.get("from_date"))
        to_date = _parse_iso_datetime(job.payload.get("to_date"))
        min_id = int(job.payload.get("min_id", 0) or 0)
        offset_id = int(job.payload.get("offset_id", 0) or 0)
        config = target.config or {}
        comments_enabled = bool(config.get("comments_enabled", True))
        if mode == "gap_fill" and not bool(config.get("gapfill_comments_enabled", True)):
            comments_enabled = False
        comments_limit = max(int(config.get("comments_limit", 20)), 1)
        comments_depth = max(int(config.get("comments_depth", 2)), 1)
        comments_recheck_posts = max(int(config.get("comments_recheck_posts", 30)), 1)
        if is_backfill and not bool(config.get("backfill_comments_enabled", True)):
            comments_enabled = False
        media_enabled = bool(config.get("media_enabled", False))

        if mode == "participants_sync":
            participants_limit = max(int(job.payload.get("participants_limit", config.get("participants_limit", 1000))), 1)
            aggressive_enabled = bool(
                job.payload.get("participants_aggressive_enabled", config.get("participants_aggressive_enabled", True))
            )
            aggressive_threshold = max(
                int(job.payload.get("participants_aggressive_threshold", config.get("participants_aggressive_threshold", 2000))),
                1,
            )
            try:
                messages = asyncio.run(
                    asyncio.wait_for(
                        self._fetch_participants(
                            account, identifier, participants_limit, aggressive_enabled, aggressive_threshold
                        ),
                        timeout=max(int(settings.telegram_fetch_timeout_seconds), 30),
                    )
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Telegram participants fetch timeout after {max(int(settings.telegram_fetch_timeout_seconds), 30)}s"
                ) from exc
            except ChatAdminRequiredError:
                new_config = dict(target.config or {})
                new_config["participants_unavailable"] = {
                    "reason": "chat_admin_required",
                    "detected_at": dt.datetime.now(dt.UTC).isoformat(),
                }
                target.config = new_config
                return []
        else:
            try:
                messages, root_count, oldest_root_message_id = asyncio.run(
                    asyncio.wait_for(
                        self._fetch_messages(
                            account,
                            identifier,
                            limit,
                            from_date=from_date,
                            to_date=to_date,
                            min_id=min_id,
                            offset_id=offset_id,
                            comments_enabled=comments_enabled,
                            comments_limit=comments_limit,
                            comments_depth=comments_depth,
                            comments_recheck_posts=comments_recheck_posts,
                            media_enabled=media_enabled,
                        ),
                        timeout=max(int(settings.telegram_fetch_timeout_seconds), 30),
                    )
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Telegram messages fetch timeout after {max(int(settings.telegram_fetch_timeout_seconds), 30)}s"
                ) from exc
            if backfill_mode == "full":
                self._update_full_backfill_state(
                    target=target,
                    account_id=account.id,
                    requested_limit=limit,
                    fetched_root_count=root_count,
                    oldest_root_message_id=oldest_root_message_id,
                )
            elif mode == "gap_fill":
                max_message_id = None
                max_observed_at = None
                for item in messages:
                    if str(item.get("event_type") or "") != "telegram_message":
                        continue
                    msg_id = item.get("message_id")
                    if msg_id is not None:
                        msg_id_int = int(msg_id)
                        max_message_id = msg_id_int if max_message_id is None else max(max_message_id, msg_id_int)
                    msg_date = _parse_iso_datetime(item.get("date"))
                    if msg_date and (max_observed_at is None or msg_date > max_observed_at):
                        max_observed_at = msg_date
                update_gapfill_state(
                    session=session,
                    target_id=target.id,
                    account_id=account.id,
                    max_message_id=max_message_id,
                    max_observed_at=max_observed_at,
                    batch_count=root_count,
                    batch_limit=limit,
                    source="gap_fill",
                )

        events: list[ParsedEvent] = []
        for item in messages:
            observed_at = dt.datetime.fromisoformat(item["date"]) if item.get("date") else None
            event_type = str(item.get("event_type") or "telegram_message")
            message_id = item.get("message_id")
            chat_id = item.get("chat_id")
            sender = item.get("sender") or {}
            sender_id = sender.get("id")
            if event_type == "telegram_comment":
                external_id = f"comment:{chat_id}:{message_id}"
            elif event_type == "telegram_participant":
                external_id = f"participant:{chat_id}:{sender_id}:job:{job.id}"
            else:
                external_id = str(message_id) if message_id else None
            events.append(
                ParsedEvent(
                    external_id=external_id,
                    observed_at=observed_at,
                    payload=item,
                    files=[
                        FileRef(
                            source_ref=str(entry.get("source_ref") or ""),
                            filename=entry.get("filename"),
                            mime=entry.get("mime"),
                            size=entry.get("size"),
                            sha256=entry.get("sha256"),
                        )
                        for entry in (item.get("files") or [])
                    ],
                    **normalize_telegram_payload(item),
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
            await client.get_entity(_entity_ref(identifier))
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
