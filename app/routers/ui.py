import asyncio
import datetime as dt
import json
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.darknet.adapters import (
    list_darknet_adapters,
    normalize_darknet_adapter,
    suggest_adapter_for_detected,
)
from app.config import get_settings
from app.darknet.detection import detect_adapter
from app.deps import get_current_user
from app.db import get_db
from app.models import (
    JobStatus,
    ParseJob,
    ParserAccount,
    ParserType,
    RawEvent,
    Target,
    TargetAccountLink,
    TelegramAuthSession,
    TelegramMembership,
    TelegramOffset,
    TelegramUser,
)
from app.plugins.registry import plugin_registry
from app.services.scheduler import schedule_once, schedule_target_once, sync_telegram_memberships
from app.services.telegram_accounts import (
    account_parallel_limits,
    compute_account_load_score,
    find_dialog_match,
    normalize_telegram_identifier,
    parse_bulk_targets_input,
    refresh_account_dialogs_sync,
)

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="app/templates")
KYIV_TZ = ZoneInfo("Europe/Kyiv")
settings = get_settings()

MODULE_META = {
    "telegram": {
        "title": "Telegram Модуль",
        "description": "Збір даних з каналів, чатів та історії користувачів.",
        "supports_accounts": True,
        "supports_links": True,
        "supports_membership_sync": True,
    },
    "darknet": {
        "title": "Darknet Модуль",
        "description": "Збір даних з onion-ресурсів та форумів через Tor.",
        "supports_accounts": True,
        "supports_links": True,
        "supports_membership_sync": False,
    },
}


def _get_module_meta(parser_name: str) -> dict:
    default = {
        "title": f"{parser_name.title()} Модуль",
        "description": "Модуль парсингу даних.",
        "supports_accounts": True,
        "supports_links": True,
        "supports_membership_sync": False,
    }
    return {**default, **MODULE_META.get(parser_name, {})}


def _require_parser_type(parser_name: str) -> ParserType:
    try:
        parser_type = ParserType(parser_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Модуль не знайдено") from exc
    if parser_name not in plugin_registry.list_types():
        raise HTTPException(status_code=404, detail="Модуль не знайдено")
    return parser_type


def _redirect_back(request: Request, fallback: str) -> RedirectResponse:
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        parsed = urlparse(referer)
        current_host = request.url.netloc

        if parsed.scheme in {"http", "https"} and parsed.netloc == current_host:
            url = parsed.path or "/"
            if parsed.query:
                url = f"{url}?{parsed.query}"
            return RedirectResponse(url=url, status_code=302)

        if not parsed.scheme and not parsed.netloc and (parsed.path or "").startswith("/"):
            url = parsed.path
            if parsed.query:
                url = f"{url}?{parsed.query}"
            return RedirectResponse(url=url, status_code=302)

    return RedirectResponse(url=fallback, status_code=302)


def _darknet_thread_marker_for_adapter(adapter_name: str) -> str:
    if adapter_name == "phpbb_like":
        return "/viewtopic.php"
    return "/threads/"


def _suggest_darknet_adapter_for_target(config: dict | None, available: list[str]) -> str:
    available_set = set(available)
    data = config or {}

    manual = str(data.get("adapter") or "").strip()
    if manual:
        try:
            normalized = normalize_darknet_adapter(manual)
        except ValueError:
            normalized = ""
        if normalized in available_set:
            return normalized

    suggested = str(data.get("adapter_suggested") or data.get("adapter_detected") or "").strip()
    normalized = suggest_adapter_for_detected(suggested)
    if normalized in available_set:
        return normalized

    if "xenforo_like" in available_set:
        return "xenforo_like"
    return available[0] if available else "xenforo_like"


def _format_kyiv_datetime(value: dt.datetime | None) -> str:
    if not value:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    kyiv = value.astimezone(KYIV_TZ)
    return kyiv.strftime("%d.%m.%Y %H:%M:%S")


def _format_age_short(delta: dt.timedelta) -> str:
    total_seconds = max(int(delta.total_seconds()), 0)
    if total_seconds < 60:
        return f"{total_seconds}с"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}хв"
    hours = minutes // 60
    minutes_rest = minutes % 60
    if hours < 24:
        return f"{hours}г {minutes_rest}хв"
    days = hours // 24
    hours_rest = hours % 24
    return f"{days}д {hours_rest}г"


def _is_telegram_network_error(error: str | None) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    markers = [
        "connection refused",
        "connectionrefused",
        "connect call failed",
        "connection to telegram failed",
        "timed out",
        "timeout",
        "server closed the connection",
        "network is unreachable",
        "socket",
    ]
    return any(marker in text for marker in markers)


def _parse_datetime_input(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return dt.datetime.fromisoformat(normalized)


def _normalize_backfill_datetime_input(iso_value: str, local_value: str) -> str:
    raw_value = (iso_value or "").strip() or (local_value or "").strip()
    if not raw_value:
        return ""
    try:
        parsed = _parse_datetime_input(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Невірний формат дати/часу: {raw_value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV_TZ)
    return parsed.astimezone(dt.UTC).isoformat()


def _iso_to_kyiv_datetime_local(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = _parse_datetime_input(raw)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    kyiv = parsed.astimezone(KYIV_TZ)
    return kyiv.strftime("%Y-%m-%dT%H:%M")


def _load_cached_dialogs(account: ParserAccount) -> list[dict]:
    creds = dict(account.credentials or {})
    dialogs = creds.get("dialogs_cache")
    if isinstance(dialogs, list):
        return [dict(item) for item in dialogs if isinstance(item, dict)]
    return []


def _refresh_account_dialogs(db: Session, account: ParserAccount) -> list[dict]:
    dialogs = refresh_account_dialogs_sync(dict(account.credentials or {}), limit=700)
    creds = dict(account.credentials or {})
    creds["dialogs_cache"] = dialogs
    creds["dialogs_cached_at"] = dt.datetime.now(dt.UTC).isoformat()
    account.credentials = creds
    db.add(account)
    return dialogs


def _match_entry_for_account(entry: str, dialogs: list[dict]) -> dict | None:
    return find_dialog_match(entry, dialogs)


def _telegram_target_config(
    limit: int,
    poll_interval_seconds: int,
    live_enabled: bool,
    gapfill_limit: int,
    backfill_enabled: bool,
    backfill_mode: str,
    backfill_from: str,
    backfill_to: str,
    backfill_chunk_days: int,
    backfill_limit: int,
    backfill_full_batch_size: int = 300,
    comments_enabled: bool = True,
    gapfill_comments_enabled: bool = True,
    backfill_comments_enabled: bool = True,
    comments_limit: int = 20,
    comments_depth: int = 2,
    comments_recheck_posts: int = 30,
    participants_sync_enabled: bool = True,
    participants_sync_interval_seconds: int = 3600,
    participants_limit: int = 1000,
) -> dict:
    normalized_mode = str(backfill_mode or "range").strip().lower()
    if normalized_mode not in {"range", "full"}:
        normalized_mode = "range"
    normalized_from = backfill_from.strip() or None
    normalized_to = backfill_to.strip() or None
    if normalized_mode == "full":
        normalized_from = None
        normalized_to = None

    return {
        "limit": max(int(limit), 1),
        "poll_interval_seconds": max(int(poll_interval_seconds), 30),
        "live_enabled": bool(live_enabled),
        "gapfill_limit": max(int(gapfill_limit), 1),
        "backfill_limit": max(int(backfill_limit), 1),
        "backfill_full_batch_size": max(int(backfill_full_batch_size), 1),
        "comments_enabled": bool(comments_enabled),
        "gapfill_comments_enabled": bool(gapfill_comments_enabled),
        "backfill_comments_enabled": bool(backfill_comments_enabled),
        "comments_limit": max(int(comments_limit), 1),
        "comments_depth": max(int(comments_depth), 1),
        "comments_recheck_posts": max(int(comments_recheck_posts), 1),
        "participants_sync_enabled": bool(participants_sync_enabled),
        "participants_sync_interval_seconds": max(int(participants_sync_interval_seconds), 300),
        "participants_limit": max(int(participants_limit), 1),
        "backfill": {
            "enabled": bool(backfill_enabled),
            "mode": normalized_mode,
            "from": normalized_from,
            "to": normalized_to,
            "chunk_days": max(int(backfill_chunk_days), 1),
        },
    }


def _telegram_job_parallel_limit(job: ParseJob, account: ParserAccount) -> int:
    parallel_jobs, backfill_parallel_jobs = account_parallel_limits(
        account=account,
        default_parallel_jobs=settings.telegram_parallel_jobs_per_account,
        default_backfill_parallel_jobs=settings.telegram_backfill_parallel_jobs_per_account,
    )
    key = str(job.job_key or "")
    if key.startswith("gapfill:") or key.startswith("poll:"):
        return parallel_jobs
    if key.startswith("backfill-full:") or key.startswith("backfill:"):
        return backfill_parallel_jobs
    if key.startswith("participants:"):
        return 1
    return 1


async def _telegram_send_code(api_id: str, api_hash: str, phone: str) -> tuple[str, str]:
    client = TelegramClient(StringSession(), int(api_id), api_hash.strip())
    await client.connect()
    if not await client.is_user_authorized():
        sent = await client.send_code_request(phone.strip())
        temp_session = client.session.save()
        await client.disconnect()
        return temp_session, sent.phone_code_hash
    temp_session = client.session.save()
    await client.disconnect()
    return temp_session, ""


async def _telegram_verify_code(
    api_id: str,
    api_hash: str,
    phone: str,
    temp_session_string: str,
    phone_code_hash: str,
    code: str,
    password: str | None,
) -> str:
    client = TelegramClient(StringSession(temp_session_string), int(api_id), api_hash.strip())
    await client.connect()
    try:
        await client.sign_in(phone=phone.strip(), code=code.strip(), phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Потрібен пароль 2FA")
        await client.sign_in(password=password)

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(status_code=400, detail="Авторизація Telegram не завершена")

    session_string = client.session.save()
    await client.disconnect()
    return session_string


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    module_cards = []

    for parser_name in plugin_registry.list_types():
        parser_type = ParserType(parser_name)
        meta = _get_module_meta(parser_name)

        target_count = db.scalar(select(func.count()).select_from(Target).where(Target.parser_type == parser_type)) or 0
        account_count = (
            db.scalar(
                select(func.count()).select_from(ParserAccount).where(ParserAccount.parser_type == parser_type)
            )
            or 0
        )
        pending_count = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(ParseJob.parser_type == parser_type, ParseJob.status.in_([JobStatus.pending, JobStatus.retry]))
            )
            or 0
        )
        running_count = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.running)
            )
            or 0
        )
        failed_count = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.failed)
            )
            or 0
        )
        raw_events_count = (
            db.scalar(select(func.count()).select_from(RawEvent).where(RawEvent.parser_type == parser_type)) or 0
        )
        last_event_at = db.scalar(
            select(func.max(RawEvent.created_at)).where(RawEvent.parser_type == parser_type)
        )
        recent_jobs = (
            db.execute(
                select(ParseJob)
                .where(ParseJob.parser_type == parser_type)
                .order_by(desc(ParseJob.created_at))
                .limit(5)
            )
            .scalars()
            .all()
        )

        if target_count == 0:
            module_status = "Не налаштовано"
            status_tone = "warn"
        elif failed_count > 0:
            module_status = "Потребує уваги"
            status_tone = "error"
        elif running_count > 0:
            module_status = "Активний"
            status_tone = "ok"
        else:
            module_status = "Готовий"
            status_tone = "neutral"

        module_cards.append(
            {
                "parser_type": parser_name,
                "title": meta["title"],
                "description": meta["description"],
                "status": module_status,
                "status_tone": status_tone,
                "targets": target_count,
                "accounts": account_count,
                "pending_jobs": pending_count,
                "running_jobs": running_count,
                "failed_jobs": failed_count,
                "raw_events": raw_events_count,
                "last_event_at": last_event_at,
                "recent_jobs": recent_jobs,
                "meta": meta,
            }
        )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "module_cards": module_cards,
        },
    )


@router.get("/data", response_class=HTMLResponse)
def parsed_data_page(
    request: Request,
    parser_name: str = "",
    target_id: int | None = None,
    q: str = "",
    selected_event_id: int | None = None,
    limit: int = 100,
    chat_limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_value = (parser_name or "").strip().lower()
    parser_type_filter: ParserType | None = None
    if parser_value:
        try:
            parser_type_filter = ParserType(parser_value)
        except ValueError:
            parser_value = ""
            parser_type_filter = None

    safe_limit = max(20, min(int(limit or 100), 500))
    safe_chat_limit = max(50, min(int(chat_limit or 200), 1000))
    query_text = (q or "").strip().lower()

    stmt = select(RawEvent).order_by(desc(RawEvent.created_at))
    if parser_type_filter:
        stmt = stmt.where(RawEvent.parser_type == parser_type_filter)
    if target_id:
        stmt = stmt.where(RawEvent.target_id == int(target_id))

    fetch_limit = safe_limit if not query_text else min(1000, safe_limit * 5)
    events = db.execute(stmt.limit(fetch_limit)).scalars().all()

    target_ids = {event.target_id for event in events}
    account_ids = {event.account_id for event in events if event.account_id}

    targets = (
        db.execute(select(Target).where(Target.id.in_(target_ids))).scalars().all()
        if target_ids
        else []
    )
    accounts = (
        db.execute(select(ParserAccount).where(ParserAccount.id.in_(account_ids))).scalars().all()
        if account_ids
        else []
    )
    target_lookup = {item.id: item for item in targets}
    account_lookup = {item.id: item for item in accounts}

    rows: list[dict] = []
    for event in events:
        target = target_lookup.get(event.target_id)
        account = account_lookup.get(event.account_id) if event.account_id else None
        preview = event.payload if isinstance(event.payload, dict) else {}
        preview_text = str(preview.get("text") or "")

        if query_text:
            haystack_parts = [
                str(event.external_id or ""),
                str(preview_text),
                str(target.name if target else ""),
                str(target.identifier if target else ""),
                json.dumps(preview, ensure_ascii=False, default=str),
            ]
            haystack = " ".join(haystack_parts).lower()
            if query_text not in haystack:
                continue

        rows.append(
            {
                "id": event.id,
                "parser_type": event.parser_type,
                "target_name": target.name if target else f"Ціль #{event.target_id}",
                "target_identifier": target.identifier if target else "-",
                "account_label": account.label if account else (str(event.account_id) if event.account_id else "-"),
                "external_id": event.external_id or "-",
                "preview_text": preview_text or "-",
                "created_at_text": _format_kyiv_datetime(event.created_at),
                "observed_at_text": _format_kyiv_datetime(event.observed_at),
            }
        )
        if len(rows) >= safe_limit:
            break

    selected_event = None
    selected_payload_pretty = ""
    if selected_event_id:
        candidate = db.get(RawEvent, int(selected_event_id))
        if candidate:
            if parser_type_filter and candidate.parser_type != parser_type_filter:
                candidate = None
            if target_id and candidate and candidate.target_id != int(target_id):
                candidate = None
        if candidate:
            payload = candidate.payload
            selected_event = candidate
            if payload is not None:
                selected_payload_pretty = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            else:
                selected_payload_pretty = "Payload недоступний"

    targets_for_filter_stmt = select(Target).order_by(Target.name.asc(), Target.id.asc())
    if parser_type_filter:
        targets_for_filter_stmt = targets_for_filter_stmt.where(Target.parser_type == parser_type_filter)
    targets_for_filter = db.execute(targets_for_filter_stmt).scalars().all()

    telegram_mode = parser_value == ParserType.telegram.value
    telegram_targets_overview: list[dict] = []
    telegram_chat_messages: list[dict] = []
    telegram_users: list[dict] = []
    telegram_active_target: Target | None = None
    selected_target_id_value = int(target_id) if target_id else None

    if telegram_mode:
        telegram_targets = (
            db.execute(select(Target).where(Target.parser_type == ParserType.telegram).order_by(Target.name.asc(), Target.id.asc()))
            .scalars()
            .all()
        )
        tg_stats_rows = (
            db.execute(
                select(RawEvent.target_id, func.count(RawEvent.id), func.max(RawEvent.created_at))
                .where(RawEvent.parser_type == ParserType.telegram)
                .group_by(RawEvent.target_id)
            )
            .all()
        )
        tg_stats_map = {int(row[0]): (int(row[1] or 0), row[2]) for row in tg_stats_rows}

        if selected_target_id_value:
            telegram_active_target = next((item for item in telegram_targets if item.id == selected_target_id_value), None)
        if telegram_active_target is None and telegram_targets:
            telegram_active_target = telegram_targets[0]
            selected_target_id_value = telegram_active_target.id

        for item in telegram_targets:
            total_events, last_event_at = tg_stats_map.get(item.id, (0, None))
            telegram_targets_overview.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "identifier": item.identifier,
                    "total_events": total_events,
                    "last_event_text": _format_kyiv_datetime(last_event_at),
                    "is_selected": bool(telegram_active_target and telegram_active_target.id == item.id),
                }
            )

        if telegram_active_target:
            tg_events = (
                db.execute(
                    select(RawEvent)
                    .where(
                        RawEvent.parser_type == ParserType.telegram,
                        RawEvent.target_id == telegram_active_target.id,
                    )
                    .order_by(desc(RawEvent.observed_at), desc(RawEvent.id))
                    .limit(max(safe_chat_limit * 3, safe_chat_limit))
                )
                .scalars()
                .all()
            )

            chat_items: list[dict] = []
            for event in tg_events:
                payload = event.payload
                if not isinstance(payload, dict):
                    continue

                event_type = str(payload.get("event_type") or "")
                if event_type not in {"telegram_message", "telegram_comment"}:
                    continue

                sender_raw = payload.get("sender")
                sender = sender_raw if isinstance(sender_raw, dict) else {}
                username = str(sender.get("username") or "").strip().removeprefix("@")
                first_name = str(sender.get("first_name") or "").strip()
                last_name = str(sender.get("last_name") or "").strip()
                full_name = " ".join(part for part in [first_name, last_name] if part).strip()
                sender_id_raw = sender.get("id")
                try:
                    sender_id = int(sender_id_raw) if sender_id_raw is not None else None
                except Exception:
                    sender_id = None

                if username:
                    sender_label = f"@{username}"
                elif full_name:
                    sender_label = full_name
                elif sender_id is not None:
                    sender_label = f"ID {sender_id}"
                else:
                    sender_label = "Невідомий автор"

                text_value = str(payload.get("text") or "").strip()
                if not text_value:
                    text_value = "(без тексту)"

                observed_at = None
                raw_date = str(payload.get("date") or "").strip()
                if raw_date:
                    try:
                        observed_at = _parse_datetime_input(raw_date)
                    except Exception:
                        observed_at = None
                if observed_at and observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=dt.UTC)
                if observed_at is None:
                    observed_at = event.observed_at or event.created_at

                if query_text:
                    message_haystack = " ".join(
                        [
                            text_value,
                            sender_label,
                            str(sender_id or ""),
                            str(event.external_id or ""),
                        ]
                    ).lower()
                    if query_text not in message_haystack:
                        continue

                chat_items.append(
                    {
                        "id": event.id,
                        "external_id": event.external_id or "-",
                        "sender_label": sender_label,
                        "sender_username": f"@{username}" if username else "-",
                        "sender_id": sender_id,
                        "text": text_value,
                        "is_comment": event_type == "telegram_comment",
                        "message_kind": "Коментар" if event_type == "telegram_comment" else "Повідомлення",
                        "root_post_id": payload.get("root_post_id"),
                        "parent_message_id": payload.get("parent_message_id"),
                        "observed_at_text": _format_kyiv_datetime(observed_at),
                        "sort_key": observed_at or event.created_at,
                    }
                )

            chat_items.sort(key=lambda item: (item["sort_key"], item["id"]))
            if len(chat_items) > safe_chat_limit:
                chat_items = chat_items[-safe_chat_limit:]
            for item in chat_items:
                item.pop("sort_key", None)
            telegram_chat_messages = chat_items

            membership_rows = (
                db.execute(
                    select(TelegramMembership, TelegramUser)
                    .join(TelegramUser, TelegramMembership.telegram_user_ref_id == TelegramUser.id)
                    .where(TelegramMembership.target_id == telegram_active_target.id)
                    .order_by(desc(TelegramMembership.last_seen_at), desc(TelegramMembership.updated_at))
                    .limit(5000)
                )
                .all()
            )

            users_by_telegram_id: dict[int, dict] = {}
            for membership, tg_user in membership_rows:
                tg_user_id = int(tg_user.telegram_user_id)
                row = users_by_telegram_id.get(tg_user_id)
                if row is None:
                    full_name = " ".join(
                        part for part in [str(tg_user.first_name or "").strip(), str(tg_user.last_name or "").strip()] if part
                    ).strip()
                    if tg_user.username:
                        display_name = f"@{tg_user.username}"
                    elif full_name:
                        display_name = full_name
                    else:
                        display_name = f"ID {tg_user.telegram_user_id}"

                    row = {
                        "telegram_user_id": tg_user_id,
                        "display_name": display_name,
                        "username": f"@{tg_user.username}" if tg_user.username else "-",
                        "full_name": full_name or "-",
                        "is_bot": bool(tg_user.is_bot),
                        "is_verified": bool(tg_user.is_verified),
                        "is_deleted": bool(tg_user.is_deleted),
                        "memberships_count": 0,
                        "is_active_any": False,
                        "statuses": set(),
                        "last_seen_at": None,
                        "last_seen_text": "-",
                    }
                    users_by_telegram_id[tg_user_id] = row

                row["memberships_count"] += 1
                row["is_active_any"] = bool(row["is_active_any"] or bool(membership.is_active))
                status_value = str(membership.membership_status or "").strip()
                if status_value:
                    row["statuses"].add(status_value)
                current_last_seen = membership.last_seen_at or membership.updated_at or membership.created_at
                if current_last_seen and (row["last_seen_at"] is None or current_last_seen > row["last_seen_at"]):
                    row["last_seen_at"] = current_last_seen

            users_list = list(users_by_telegram_id.values())
            users_list.sort(
                key=lambda item: (
                    item["last_seen_at"] or dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
                    item["telegram_user_id"],
                ),
                reverse=True,
            )
            telegram_users = []
            for item in users_list[:500]:
                statuses_sorted = sorted(item["statuses"])
                status_text = ", ".join(statuses_sorted[:2]) if statuses_sorted else "-"
                if len(statuses_sorted) > 2:
                    status_text = f"{status_text} +{len(statuses_sorted) - 2}"
                telegram_users.append(
                    {
                        "telegram_user_id": item["telegram_user_id"],
                        "display_name": item["display_name"],
                        "username": item["username"],
                        "full_name": item["full_name"],
                        "is_bot": item["is_bot"],
                        "is_verified": item["is_verified"],
                        "is_deleted": item["is_deleted"],
                        "memberships_count": item["memberships_count"],
                        "is_active_any": item["is_active_any"],
                        "status_text": status_text,
                        "last_seen_text": _format_kyiv_datetime(item["last_seen_at"]),
                    }
                )

    return templates.TemplateResponse(
        "data.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "parser_name": parser_value,
            "selected_target_id": selected_target_id_value,
            "q": q,
            "limit": safe_limit,
            "chat_limit": safe_chat_limit,
            "targets_for_filter": targets_for_filter,
            "selected_event_id": int(selected_event_id) if selected_event_id else None,
            "selected_event": selected_event,
            "selected_payload_pretty": selected_payload_pretty,
            "telegram_mode": telegram_mode,
            "telegram_targets_overview": telegram_targets_overview,
            "telegram_active_target": telegram_active_target,
            "telegram_chat_messages": telegram_chat_messages,
            "telegram_users": telegram_users,
        },
    )


@router.get("/modules/{parser_name}", response_class=HTMLResponse)
def module_page(parser_name: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    parser_type = _require_parser_type(parser_name)
    meta = _get_module_meta(parser_name)
    pending_auths: list[TelegramAuthSession] = []
    darknet_adapters: list[str] = []
    darknet_default_adapter = "xenforo_like"
    darknet_target_defaults: dict[int, str] = {}
    target_runtime: dict[int, dict] = {}
    account_runtime: dict[int, dict] = {}
    edit_target: Target | None = None
    edit_target_config: dict = {}
    offset_runtime_by_target: dict[int, dict] = {}
    job_wait_reason: dict[int, str] = {}

    targets = db.execute(select(Target).where(Target.parser_type == parser_type).order_by(desc(Target.created_at))).scalars().all()
    accounts = (
        db.execute(select(ParserAccount).where(ParserAccount.parser_type == parser_type).order_by(desc(ParserAccount.created_at)))
        .scalars()
        .all()
    )
    target_lookup = {target.id: {"name": target.name, "identifier": target.identifier} for target in targets}
    account_lookup = {account.id: account.label for account in accounts}

    links = []
    if meta["supports_links"]:
        links = (
            db.execute(
                select(TargetAccountLink)
                .join(Target, TargetAccountLink.target_id == Target.id)
                .where(Target.parser_type == parser_type)
                .order_by(desc(TargetAccountLink.id))
            )
            .scalars()
            .all()
        )

    jobs = (
        db.execute(
            select(ParseJob)
            .where(
                ParseJob.parser_type == parser_type,
                ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry, JobStatus.failed]),
            )
            .order_by(desc(ParseJob.created_at))
            .limit(50)
        )
        .scalars()
        .all()
    )
    recent_success_jobs = (
        db.execute(select(ParseJob).where(ParseJob.parser_type == parser_type).order_by(desc(ParseJob.created_at)).limit(50))
        .scalars()
        .all()
    )
    recent_success_jobs = [job for job in recent_success_jobs if job.status == JobStatus.succeeded][:20]
    events_count = db.scalar(select(func.count()).select_from(RawEvent).where(RawEvent.parser_type == parser_type)) or 0
    telegram_network_issue_by_target: dict[int, str] = {}
    telegram_network_issue_targets: set[int] = set()
    if parser_name == "telegram":
        for job in jobs:
            if _is_telegram_network_error(job.last_error):
                target_key = int(job.target_id)
                telegram_network_issue_targets.add(target_key)
                if target_key not in telegram_network_issue_by_target:
                    short_error = str(job.last_error or "").strip().splitlines()[0][:140]
                    telegram_network_issue_by_target[target_key] = short_error

    if parser_name == "telegram" and targets:
        target_ids = [target.id for target in targets]
        offsets = (
            db.execute(select(TelegramOffset).where(TelegramOffset.target_id.in_(target_ids)))
            .scalars()
            .all()
        )
        for offset in offsets:
            bucket = offset_runtime_by_target.setdefault(
                offset.target_id,
                {
                    "max_message_id": 0,
                    "updated_at": None,
                    "last_event_at": None,
                    "accounts": 0,
                    "caught_up_accounts": 0,
                },
            )
            bucket["accounts"] += 1
            bucket["max_message_id"] = max(int(bucket["max_message_id"]), int(offset.max_message_id or 0))
            if offset.is_caught_up:
                bucket["caught_up_accounts"] += 1
            if offset.updated_at and (bucket["updated_at"] is None or offset.updated_at > bucket["updated_at"]):
                bucket["updated_at"] = offset.updated_at
            if offset.last_event_at and (bucket["last_event_at"] is None or offset.last_event_at > bucket["last_event_at"]):
                bucket["last_event_at"] = offset.last_event_at

    now_for_target_runtime = dt.datetime.now(dt.UTC)
    for target in targets:
        running = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(
                    ParseJob.parser_type == parser_type,
                    ParseJob.target_id == target.id,
                    ParseJob.status == JobStatus.running,
                )
            )
            or 0
        )
        queued = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(
                    ParseJob.parser_type == parser_type,
                    ParseJob.target_id == target.id,
                    ParseJob.status.in_([JobStatus.pending, JobStatus.retry]),
                )
            )
            or 0
        )
        failed = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(
                    ParseJob.parser_type == parser_type,
                    ParseJob.target_id == target.id,
                    ParseJob.status == JobStatus.failed,
                )
            )
            or 0
        )
        last_success = db.scalar(
            select(func.max(ParseJob.finished_at)).where(
                ParseJob.parser_type == parser_type,
                ParseJob.target_id == target.id,
                ParseJob.status == JobStatus.succeeded,
            )
        )
        target_events_row = db.execute(
            select(func.count(RawEvent.id), func.max(RawEvent.created_at)).where(
                RawEvent.parser_type == parser_type,
                RawEvent.target_id == target.id,
            )
        ).one()
        target_events = int(target_events_row[0] or 0)
        last_raw_created_at = target_events_row[1]
        progress_hint = "-"

        if not target.is_active:
            process_text = "Зупинено"
            tone = "neutral"
        elif running > 0:
            process_text = f"Виконується ({running})"
            tone = "ok"
            if last_raw_created_at:
                age = now_for_target_runtime - last_raw_created_at
                if age >= dt.timedelta(minutes=3):
                    progress_hint = f"Без нових даних {_format_age_short(age)}"
                else:
                    progress_hint = f"Останні нові дані {_format_age_short(age)} тому"
            else:
                progress_hint = "Ще немає збережених даних"
        elif queued > 0:
            process_text = f"У черзі ({queued})"
            tone = "warn"
            progress_hint = "Очікує вільний слот/воркер"
        elif failed > 0:
            process_text = f"Є помилки ({failed})"
            tone = "error"
        else:
            if last_success:
                process_text = "Працює за розкладом"
            else:
                process_text = "Готово до першого запуску"
            tone = "neutral"

        target_runtime[target.id] = {
            "running": running,
            "queued": queued,
            "failed": failed,
            "events": target_events,
            "last_success": last_success,
            "last_success_text": _format_kyiv_datetime(last_success),
            "process_text": process_text,
            "tone": tone,
            "progress_hint": progress_hint,
            "last_raw_created_text": _format_kyiv_datetime(last_raw_created_at),
        }
        if parser_name == "telegram":
            cfg = dict(target.config or {})
            agg = offset_runtime_by_target.get(target.id, {})
            accounts_with_offsets = int(agg.get("accounts") or 0)
            caught_up_accounts = int(agg.get("caught_up_accounts") or 0)
            live_enabled = bool(cfg.get("live_enabled", True))
            target_runtime[target.id].update(
                {
                    "ingest_mode": "live+recovery" if live_enabled else "polling",
                    "offset_max_message_id": int(agg.get("max_message_id") or 0),
                    "offset_updated_text": _format_kyiv_datetime(agg.get("updated_at")),
                    "offset_last_event_text": _format_kyiv_datetime(agg.get("last_event_at")),
                    "offset_accounts_text": f"{caught_up_accounts}/{accounts_with_offsets}" if accounts_with_offsets > 0 else "-",
                    "offset_caught_up": bool(accounts_with_offsets > 0 and caught_up_accounts == accounts_with_offsets),
                }
            )
            network_issue_text = telegram_network_issue_by_target.get(target.id)
            if network_issue_text:
                target_runtime[target.id]["network_issue_text"] = (
                    f"Проблема мережі Telegram: автоповтор задач ({network_issue_text})"
                )
            else:
                target_runtime[target.id]["network_issue_text"] = ""

    if parser_name == "telegram" and jobs:
        now = dt.datetime.now(dt.UTC)
        account_ids_for_jobs = {int(job.account_id) for job in jobs if job.account_id}
        running_counts_by_account: dict[int, int] = {}
        if account_ids_for_jobs:
            rows = (
                db.execute(
                    select(ParseJob.account_id, func.count(ParseJob.id))
                    .where(
                        ParseJob.parser_type == ParserType.telegram,
                        ParseJob.status == JobStatus.running,
                        ParseJob.account_id.in_(account_ids_for_jobs),
                    )
                    .group_by(ParseJob.account_id)
                )
                .all()
            )
            running_counts_by_account = {int(account_id): int(count) for account_id, count in rows if account_id is not None}

        account_map = {account.id: account for account in accounts}
        for job in jobs:
            reason = "-"
            if job.status in (JobStatus.pending, JobStatus.retry):
                if job.run_after and job.run_after > now:
                    reason = f"Чекає таймер до {_format_kyiv_datetime(job.run_after)}"
                elif _is_telegram_network_error(job.last_error):
                    reason = "Мережевий збій Telegram, автоповтор задачі"
                elif not job.account_id:
                    reason = "Чекає вільний воркер"
                else:
                    account = account_map.get(int(job.account_id))
                    if not account:
                        reason = "Акаунт не знайдено"
                    elif not account.is_active:
                        reason = "Акаунт вимкнено"
                    elif account.cooldown_until and account.cooldown_until > now:
                        reason = f"Пауза акаунта до {_format_kyiv_datetime(account.cooldown_until)}"
                    else:
                        running_count = int(running_counts_by_account.get(int(job.account_id), 0))
                        limit = _telegram_job_parallel_limit(job, account)
                        if running_count >= limit:
                            reason = f"Чекає вільний слот акаунта ({running_count}/{limit})"
                        else:
                            reason = "Чекає чергу воркера"
            elif job.status == JobStatus.running:
                if job.updated_at:
                    lag = now - job.updated_at
                    if lag >= dt.timedelta(minutes=3):
                        if int(job.target_id) in telegram_network_issue_targets:
                            reason = f"Ймовірний мережевий збій Telegram ({_format_age_short(lag)})"
                        else:
                            reason = f"Виконується без прогресу {_format_age_short(lag)}"
                    else:
                        reason = "Задача виконується"
                else:
                    reason = "Задача виконується"
            elif job.status == JobStatus.failed and job.last_error:
                reason = "Завершено з помилкою"
            job_wait_reason[job.id] = reason

    if parser_name == "telegram":
        now = dt.datetime.now(dt.UTC)
        expired = (
            db.execute(
                select(TelegramAuthSession).where(
                    TelegramAuthSession.expires_at < now,
                    TelegramAuthSession.is_completed.is_(False),
                )
            )
            .scalars()
            .all()
        )
        for item in expired:
            db.delete(item)
        if expired:
            db.commit()

        pending_auths = (
            db.execute(
                select(TelegramAuthSession)
                .where(TelegramAuthSession.is_completed.is_(False))
                .order_by(desc(TelegramAuthSession.created_at))
            )
            .scalars()
            .all()
        )

        for account in accounts:
            queued_jobs = (
                db.scalar(
                    select(func.count())
                    .select_from(ParseJob)
                    .where(
                        ParseJob.parser_type == ParserType.telegram,
                        ParseJob.account_id == account.id,
                        ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
                    )
                )
                or 0
            )
            load_score = compute_account_load_score(account, queued_jobs=queued_jobs, now=now)
            utilization = min(
                100,
                int((int(account.hour_window_count or 0) / max(int(account.hourly_limit or 1), 1)) * 100),
            )
            dialogs_count = len(_load_cached_dialogs(account))
            parallel_jobs, backfill_parallel_jobs = account_parallel_limits(
                account=account,
                default_parallel_jobs=settings.telegram_parallel_jobs_per_account,
                default_backfill_parallel_jobs=settings.telegram_backfill_parallel_jobs_per_account,
            )

            if not account.is_active:
                state_text = "Вимкнено"
                state_tone = "neutral"
            elif account.cooldown_until and account.cooldown_until > now:
                state_text = f"Пауза до {_format_kyiv_datetime(account.cooldown_until)}"
                state_tone = "warn"
            elif account.health_score < 20:
                state_text = "Низький ресурс"
                state_tone = "error"
            elif queued_jobs > 0:
                state_text = f"В роботі ({queued_jobs})"
                state_tone = "ok"
            else:
                state_text = "Готовий"
                state_tone = "ok"

            account_runtime[account.id] = {
                "queued_jobs": queued_jobs,
                "load_score": round(load_score, 2),
                "utilization": utilization,
                "dialogs_count": dialogs_count,
                "parallel_jobs": parallel_jobs,
                "backfill_parallel_jobs": backfill_parallel_jobs,
                "state_text": state_text,
                "state_tone": state_tone,
                "last_success_text": _format_kyiv_datetime(account.last_success_at),
            }
    elif parser_name == "darknet":
        darknet_adapters = list_darknet_adapters()
        for target in targets:
            selected = _suggest_darknet_adapter_for_target(target.config, darknet_adapters)
            darknet_target_defaults[target.id] = selected
        if targets:
            darknet_default_adapter = darknet_target_defaults.get(targets[0].id, darknet_default_adapter)

    if parser_name == "telegram":
        raw_edit_target_id = str(request.query_params.get("edit_target_id") or "").strip()
        if raw_edit_target_id.isdigit():
            candidate = db.get(Target, int(raw_edit_target_id))
            if candidate and candidate.parser_type == parser_type:
                edit_target = candidate
                cfg = dict(candidate.config or {})
                backfill_cfg = dict(cfg.get("backfill") or {})
                backfill_mode = str(backfill_cfg.get("mode") or "range").strip().lower()
                if backfill_mode not in {"range", "full"}:
                    backfill_mode = "range"
                edit_target_config = {
                    "limit": int(cfg.get("limit", 200)),
                    "poll_interval_seconds": int(cfg.get("poll_interval_seconds", 300)),
                    "live_enabled": bool(cfg.get("live_enabled", True)),
                    "gapfill_limit": int(cfg.get("gapfill_limit", cfg.get("limit", 200))),
                    "gapfill_comments_enabled": bool(cfg.get("gapfill_comments_enabled", True)),
                    "participants_sync_enabled": bool(cfg.get("participants_sync_enabled", True)),
                    "participants_sync_interval_seconds": int(cfg.get("participants_sync_interval_seconds", 3600)),
                    "participants_limit": int(cfg.get("participants_limit", 1000)),
                    "backfill_enabled": bool(backfill_cfg.get("enabled", False)),
                    "backfill_mode": backfill_mode,
                    "backfill_from": backfill_cfg.get("from") or "",
                    "backfill_from_local": _iso_to_kyiv_datetime_local(backfill_cfg.get("from")),
                    "backfill_to": backfill_cfg.get("to") or "",
                    "backfill_to_local": _iso_to_kyiv_datetime_local(backfill_cfg.get("to")),
                    "backfill_chunk_days": int(backfill_cfg.get("chunk_days", 7)),
                    "backfill_limit": int(cfg.get("backfill_limit", 5000)),
                    "backfill_full_batch_size": int(cfg.get("backfill_full_batch_size", 300)),
                }

    return templates.TemplateResponse(
        "module.html",
        {
            "request": request,
            "user": user,
            "parser_name": parser_name,
            "parser_type": parser_type,
            "meta": meta,
            "targets": targets,
            "accounts": accounts,
            "links": links,
            "jobs": jobs,
            "recent_success_jobs": recent_success_jobs,
            "events_count": events_count,
            "pending_auths": pending_auths,
            "darknet_adapters": darknet_adapters,
            "darknet_default_adapter": darknet_default_adapter,
            "darknet_target_defaults": darknet_target_defaults,
            "target_runtime": target_runtime,
            "account_runtime": account_runtime,
            "edit_target": edit_target,
            "edit_target_config": edit_target_config,
            "target_lookup": target_lookup,
            "account_lookup": account_lookup,
            "job_wait_reason": job_wait_reason,
        },
    )


@router.post("/modules/{parser_name}/targets")
def create_module_target(
    parser_name: str,
    request: Request,
    name: str = Form(...),
    identifier: str = Form(...),
    limit: int = Form(200),
    poll_interval_seconds: int = Form(300),
    live_enabled: bool = Form(False),
    gapfill_limit: int = Form(200),
    participants_sync_enabled: bool = Form(False),
    participants_sync_interval_seconds: int = Form(3600),
    participants_limit: int = Form(1000),
    backfill_enabled: bool = Form(False),
    backfill_mode: str = Form("range"),
    backfill_from: str = Form(""),
    backfill_from_local: str = Form(""),
    backfill_to: str = Form(""),
    backfill_to_local: str = Form(""),
    backfill_chunk_days: int = Form(7),
    backfill_limit: int = Form(5000),
    adapter: str = Form("xenforo_like"),
    start_urls_text: str = Form(""),
    login_required: bool = Form(False),
    collect_maximum: bool = Form(True),
    reparse_existing_threads: bool = Form(False),
    thread_reparse_interval_minutes: int = Form(15),
    max_threads_per_cycle: int = Form(0),
    max_pages_per_thread: int = Form(0),
    max_posts_per_thread: int = Form(0),
    max_discover_pages_per_start: int = Form(0),
    thread_url_contains: str = Form("/threads/"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = _require_parser_type(parser_name)
    config: dict = {}

    if parser_type == ParserType.telegram:
        normalized_backfill_from = _normalize_backfill_datetime_input(backfill_from, backfill_from_local)
        normalized_backfill_to = _normalize_backfill_datetime_input(backfill_to, backfill_to_local)
        config = _telegram_target_config(
            limit=limit,
            poll_interval_seconds=poll_interval_seconds,
            live_enabled=live_enabled,
            gapfill_limit=gapfill_limit,
            participants_sync_enabled=participants_sync_enabled,
            participants_sync_interval_seconds=participants_sync_interval_seconds,
            participants_limit=participants_limit,
            backfill_enabled=backfill_enabled,
            backfill_mode=backfill_mode,
            backfill_from=normalized_backfill_from,
            backfill_to=normalized_backfill_to,
            backfill_chunk_days=backfill_chunk_days,
            backfill_limit=backfill_limit,
        )
    elif parser_type == ParserType.darknet:
        try:
            normalized_adapter = normalize_darknet_adapter(adapter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw_start_urls = [line.strip() for line in start_urls_text.splitlines() if line.strip()]
        start_urls = raw_start_urls if raw_start_urls else [identifier.strip()]
        thread_marker = thread_url_contains.strip() or _darknet_thread_marker_for_adapter(normalized_adapter)
        config = {
            "adapter": normalized_adapter,
            "start_urls": start_urls,
            "login_required": bool(login_required),
            "collect_maximum": bool(collect_maximum),
            "reparse_existing_threads": bool(reparse_existing_threads),
            "thread_reparse_interval_minutes": max(int(thread_reparse_interval_minutes), 1),
            "max_threads_per_cycle": max(int(max_threads_per_cycle), 0),
            "max_pages_per_thread": max(int(max_pages_per_thread), 0),
            "max_posts_per_thread": max(int(max_posts_per_thread), 0),
            "max_discover_pages_per_start": max(int(max_discover_pages_per_start), 0),
            "thread_url_contains": thread_marker,
        }

    target = Target(parser_type=parser_type, name=name.strip(), identifier=identifier.strip(), config=config)
    db.add(target)
    db.commit()
    return RedirectResponse(url=f"/modules/{parser_name}", status_code=302)


@router.post("/modules/telegram/targets/{target_id}/update")
def update_telegram_target(
    target_id: int,
    request: Request,
    name: str = Form(...),
    identifier: str = Form(...),
    limit: int = Form(200),
    poll_interval_seconds: int = Form(300),
    live_enabled: bool = Form(False),
    gapfill_limit: int = Form(200),
    participants_sync_enabled: bool = Form(False),
    participants_sync_interval_seconds: int = Form(3600),
    participants_limit: int = Form(1000),
    backfill_enabled: bool = Form(False),
    backfill_mode: str = Form("range"),
    backfill_from: str = Form(""),
    backfill_from_local: str = Form(""),
    backfill_to: str = Form(""),
    backfill_to_local: str = Form(""),
    backfill_chunk_days: int = Form(7),
    backfill_limit: int = Form(5000),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    target = db.get(Target, target_id)
    if not target or target.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram ціль не знайдена")

    normalized_backfill_from = _normalize_backfill_datetime_input(backfill_from, backfill_from_local)
    normalized_backfill_to = _normalize_backfill_datetime_input(backfill_to, backfill_to_local)

    target.name = name.strip()
    target.identifier = identifier.strip()
    target.config = _telegram_target_config(
        limit=limit,
        poll_interval_seconds=poll_interval_seconds,
        live_enabled=live_enabled,
        gapfill_limit=gapfill_limit,
        participants_sync_enabled=participants_sync_enabled,
        participants_sync_interval_seconds=participants_sync_interval_seconds,
        participants_limit=participants_limit,
        backfill_enabled=backfill_enabled,
        backfill_mode=backfill_mode,
        backfill_from=normalized_backfill_from,
        backfill_to=normalized_backfill_to,
        backfill_chunk_days=backfill_chunk_days,
        backfill_limit=backfill_limit,
    )
    db.commit()
    return RedirectResponse(url="/modules/telegram", status_code=302)


@router.post("/modules/darknet/targets/{target_id}/adapter")
def update_darknet_target_adapter(
    target_id: int,
    request: Request,
    adapter: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    target = db.get(Target, target_id)
    if not target or target.parser_type != ParserType.darknet:
        raise HTTPException(status_code=404, detail="Darknet ціль не знайдена")

    try:
        normalized = normalize_darknet_adapter(adapter)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    config = dict(target.config or {})
    config["adapter"] = normalized

    marker = str(config.get("thread_url_contains") or "").strip()
    if not marker or marker in {"/threads/", "/viewtopic.php"}:
        config["thread_url_contains"] = _darknet_thread_marker_for_adapter(normalized)

    target.config = config
    db.commit()
    return RedirectResponse(url="/modules/darknet", status_code=302)


@router.post("/modules/darknet/targets/{target_id}/detect-adapter")
def detect_darknet_target_adapter(
    target_id: int,
    request: Request,
    max_urls: int = Form(3),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    target = db.get(Target, target_id)
    if not target or target.parser_type != ParserType.darknet:
        raise HTTPException(status_code=404, detail="Darknet ціль не знайдена")

    config = dict(target.config or {})
    configured_start_urls = config.get("start_urls")
    start_urls: list[str] = []
    if isinstance(configured_start_urls, list):
        start_urls = [str(item).strip() for item in configured_start_urls if str(item).strip()]
    if not start_urls and target.identifier.strip():
        start_urls = [target.identifier.strip()]

    result = asyncio.run(detect_adapter(start_urls, max_urls=max(int(max_urls), 1)))
    result_payload = result.as_dict()

    config["adapter_detected"] = result_payload["detected"]
    config["adapter_suggested"] = result_payload["recommended_adapter"]
    config["adapter_detected_scores"] = result_payload["scores"]
    config["adapter_detected_signals"] = result_payload["matches"]
    config["adapter_detected_urls"] = result_payload["checked_urls"]
    config["adapter_detected_error"] = result_payload["error"]
    config["adapter_detected_at"] = dt.datetime.now(dt.UTC).isoformat()
    target.config = config
    db.commit()
    return RedirectResponse(url="/modules/darknet", status_code=302)


@router.post("/modules/{parser_name}/targets/{target_id}/start")
def start_target_parsing(
    parser_name: str,
    target_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = _require_parser_type(parser_name)
    target = db.get(Target, target_id)
    if not target or target.parser_type != parser_type:
        raise HTTPException(status_code=404, detail="Ціль не знайдена")

    target.is_active = True
    db.commit()
    return _redirect_back(request, fallback=f"/modules/{parser_name}")


@router.post("/modules/{parser_name}/targets/{target_id}/stop")
def stop_target_parsing(
    parser_name: str,
    target_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = _require_parser_type(parser_name)
    target = db.get(Target, target_id)
    if not target or target.parser_type != parser_type:
        raise HTTPException(status_code=404, detail="Ціль не знайдена")

    target.is_active = False
    pending_jobs = (
        db.execute(
            select(ParseJob).where(
                ParseJob.parser_type == parser_type,
                ParseJob.target_id == target.id,
                ParseJob.status.in_([JobStatus.pending, JobStatus.retry]),
            )
        )
        .scalars()
        .all()
    )
    now = dt.datetime.now(dt.UTC)
    for job in pending_jobs:
        job.status = JobStatus.failed
        job.finished_at = now
        job.last_error = "Зупинено користувачем"
        job.locked_by = None
        job.lock_expires_at = None

    db.commit()
    return _redirect_back(request, fallback=f"/modules/{parser_name}")


@router.post("/modules/{parser_name}/targets/{target_id}/run-now")
def run_target_now(
    parser_name: str,
    target_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = _require_parser_type(parser_name)
    target = db.get(Target, target_id)
    if not target or target.parser_type != parser_type:
        raise HTTPException(status_code=404, detail="Ціль не знайдена")

    target.is_active = True
    schedule_target_once(db, target)
    db.commit()
    return _redirect_back(request, fallback=f"/modules/{parser_name}")


@router.post("/modules/telegram/targets/smart-add")
def smart_add_telegram_targets(
    request: Request,
    entries_text: str = Form(...),
    limit: int = Form(200),
    poll_interval_seconds: int = Form(300),
    live_enabled: bool = Form(False),
    gapfill_limit: int = Form(200),
    participants_sync_enabled: bool = Form(False),
    participants_sync_interval_seconds: int = Form(3600),
    participants_limit: int = Form(1000),
    backfill_enabled: bool = Form(False),
    backfill_mode: str = Form("range"),
    backfill_from: str = Form(""),
    backfill_from_local: str = Form(""),
    backfill_to: str = Form(""),
    backfill_to_local: str = Form(""),
    backfill_chunk_days: int = Form(7),
    backfill_limit: int = Form(5000),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    entries = parse_bulk_targets_input(entries_text)
    if not entries:
        return RedirectResponse(url="/modules/telegram", status_code=302)

    normalized_backfill_from = _normalize_backfill_datetime_input(backfill_from, backfill_from_local)
    normalized_backfill_to = _normalize_backfill_datetime_input(backfill_to, backfill_to_local)

    accounts = (
        db.execute(
            select(ParserAccount).where(
                ParserAccount.parser_type == ParserType.telegram,
                ParserAccount.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if not accounts:
        raise HTTPException(status_code=400, detail="Немає активних Telegram-акаунтів")

    now = dt.datetime.now(dt.UTC)
    account_info: dict[int, dict] = {}
    for account in accounts:
        dialogs = _load_cached_dialogs(account)
        if not dialogs:
            try:
                dialogs = _refresh_account_dialogs(db, account)
            except Exception:
                dialogs = []
        queued_jobs = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(
                    ParseJob.parser_type == ParserType.telegram,
                    ParseJob.account_id == account.id,
                    ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
                )
            )
            or 0
        )
        account_info[account.id] = {
            "account": account,
            "dialogs": dialogs,
            "base_score": compute_account_load_score(account, queued_jobs=queued_jobs, now=now),
            "assigned": 0,
        }

    for entry in entries:
        normalized_entry = normalize_telegram_identifier(entry)
        matches: list[tuple[ParserAccount, dict]] = []
        for account in accounts:
            dialog = _match_entry_for_account(entry, account_info[account.id]["dialogs"])
            if dialog:
                matches.append((account, dialog))

        if matches:
            canonical_identifier = str(matches[0][1].get("identifier") or normalized_entry or entry).strip()
            canonical_name = str(matches[0][1].get("title") or canonical_identifier)
            candidates = [account for account, _ in matches]
        else:
            canonical_identifier = normalized_entry or entry.strip()
            canonical_name = entry.strip()
            candidates = accounts

        existing_target = db.execute(
            select(Target).where(
                Target.parser_type == ParserType.telegram,
                Target.identifier == canonical_identifier,
            )
        ).scalar_one_or_none()
        if existing_target:
            target = existing_target
            target.is_active = True
            if not target.name:
                target.name = canonical_name
            if not target.config:
                target.config = _telegram_target_config(
                    limit=limit,
                    poll_interval_seconds=poll_interval_seconds,
                    live_enabled=live_enabled,
                    gapfill_limit=gapfill_limit,
                    participants_sync_enabled=participants_sync_enabled,
                    participants_sync_interval_seconds=participants_sync_interval_seconds,
                    participants_limit=participants_limit,
                    backfill_enabled=backfill_enabled,
                    backfill_mode=backfill_mode,
                    backfill_from=normalized_backfill_from,
                    backfill_to=normalized_backfill_to,
                    backfill_chunk_days=backfill_chunk_days,
                    backfill_limit=backfill_limit,
                )
        else:
            target = Target(
                parser_type=ParserType.telegram,
                name=canonical_name,
                identifier=canonical_identifier,
                config=_telegram_target_config(
                    limit=limit,
                    poll_interval_seconds=poll_interval_seconds,
                    live_enabled=live_enabled,
                    gapfill_limit=gapfill_limit,
                    participants_sync_enabled=participants_sync_enabled,
                    participants_sync_interval_seconds=participants_sync_interval_seconds,
                    participants_limit=participants_limit,
                    backfill_enabled=backfill_enabled,
                    backfill_mode=backfill_mode,
                    backfill_from=normalized_backfill_from,
                    backfill_to=normalized_backfill_to,
                    backfill_chunk_days=backfill_chunk_days,
                    backfill_limit=backfill_limit,
                ),
                is_active=True,
            )
            db.add(target)
            db.flush()

        ranked = sorted(
            candidates,
            key=lambda account: account_info[account.id]["base_score"] + (account_info[account.id]["assigned"] * 2.0),
        )
        selected_account = ranked[0]
        account_info[selected_account.id]["assigned"] += 1

        existing_link = db.execute(
            select(TargetAccountLink).where(
                TargetAccountLink.target_id == target.id,
                TargetAccountLink.account_id == selected_account.id,
            )
        ).scalar_one_or_none()
        if not existing_link:
            db.add(
                TargetAccountLink(
                    target_id=target.id,
                    account_id=selected_account.id,
                    is_active=True,
                    auto_detected=True,
                    last_checked_at=now,
                )
            )
        else:
            existing_link.is_active = True
            existing_link.auto_detected = True
            existing_link.last_checked_at = now

    db.commit()
    return RedirectResponse(url="/modules/telegram", status_code=302)


@router.get("/modules/telegram/accounts/{account_id}", response_class=HTMLResponse)
def telegram_account_page(account_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    account = db.get(ParserAccount, account_id)
    if not account or account.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram акаунт не знайдено")

    dialogs = _load_cached_dialogs(account)
    if not dialogs:
        try:
            dialogs = _refresh_account_dialogs(db, account)
            db.commit()
        except Exception:
            dialogs = []

    account_targets = (
        db.execute(
            select(Target)
            .join(TargetAccountLink, TargetAccountLink.target_id == Target.id)
            .where(Target.parser_type == ParserType.telegram, TargetAccountLink.account_id == account.id)
            .order_by(desc(Target.created_at))
        )
        .scalars()
        .all()
    )
    linked_identifiers = {normalize_telegram_identifier(target.identifier) for target in account_targets}

    dialogs_view = []
    for dialog in dialogs:
        identifier = normalize_telegram_identifier(str(dialog.get("identifier") or ""))
        dialogs_view.append(
            {
                **dialog,
                "is_linked": identifier in linked_identifiers,
            }
        )

    now = dt.datetime.now(dt.UTC)
    queued_jobs = (
        db.scalar(
            select(func.count())
            .select_from(ParseJob)
            .where(
                ParseJob.parser_type == ParserType.telegram,
                ParseJob.account_id == account.id,
                ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
            )
        )
        or 0
    )
    load_score = compute_account_load_score(account, queued_jobs=queued_jobs, now=now)
    utilization = min(100, int((int(account.hour_window_count or 0) / max(int(account.hourly_limit or 1), 1)) * 100))
    cooldown_text = _format_kyiv_datetime(account.cooldown_until) if account.cooldown_until else "-"
    last_success_text = _format_kyiv_datetime(account.last_success_at)
    parallel_jobs, backfill_parallel_jobs = account_parallel_limits(
        account=account,
        default_parallel_jobs=settings.telegram_parallel_jobs_per_account,
        default_backfill_parallel_jobs=settings.telegram_backfill_parallel_jobs_per_account,
    )

    return templates.TemplateResponse(
        "telegram_account.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "dialogs": dialogs_view,
            "targets": account_targets,
            "queued_jobs": queued_jobs,
            "load_score": round(load_score, 2),
            "utilization": utilization,
            "cooldown_text": cooldown_text,
            "last_success_text": last_success_text,
            "parallel_jobs": parallel_jobs,
            "backfill_parallel_jobs": backfill_parallel_jobs,
            "default_parallel_jobs": settings.telegram_parallel_jobs_per_account,
            "default_backfill_parallel_jobs": settings.telegram_backfill_parallel_jobs_per_account,
        },
    )


@router.post("/modules/telegram/accounts/{account_id}/settings")
def update_telegram_account_settings(
    account_id: int,
    request: Request,
    hourly_limit: int = Form(120),
    parallel_jobs: int = Form(3),
    backfill_parallel_jobs: int = Form(1),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = db.get(ParserAccount, account_id)
    if not account or account.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram акаунт не знайдено")

    account.hourly_limit = max(int(hourly_limit), 1)
    account.is_active = bool(is_active)
    creds = dict(account.credentials or {})
    creds["parallel_jobs"] = max(int(parallel_jobs), 1)
    creds["backfill_parallel_jobs"] = max(int(backfill_parallel_jobs), 1)
    account.credentials = creds
    db.commit()
    return RedirectResponse(url=f"/modules/telegram/accounts/{account_id}", status_code=302)


@router.post("/modules/telegram/accounts/{account_id}/refresh-dialogs")
def refresh_telegram_account_dialogs(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = db.get(ParserAccount, account_id)
    if not account or account.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram акаунт не знайдено")

    try:
        _refresh_account_dialogs(db, account)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не вдалося оновити список чатів: {exc}") from exc
    db.commit()
    return RedirectResponse(url=f"/modules/telegram/accounts/{account_id}", status_code=302)


@router.post("/modules/telegram/accounts/{account_id}/dialogs/add-target")
def add_target_from_account_dialog(
    account_id: int,
    request: Request,
    identifier: str = Form(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = db.get(ParserAccount, account_id)
    if not account or account.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram акаунт не знайдено")

    normalized_identifier = normalize_telegram_identifier(identifier)
    if not normalized_identifier:
        raise HTTPException(status_code=400, detail="Порожній ідентифікатор")

    target = db.execute(
        select(Target).where(
            Target.parser_type == ParserType.telegram,
            Target.identifier == normalized_identifier,
        )
    ).scalar_one_or_none()
    if not target:
        target = Target(
            parser_type=ParserType.telegram,
            name=title.strip() or normalized_identifier,
            identifier=normalized_identifier,
            config=_telegram_target_config(
                limit=200,
                poll_interval_seconds=300,
                live_enabled=True,
                gapfill_limit=200,
                backfill_enabled=True,
                backfill_mode="full",
                backfill_from="",
                backfill_to="",
                backfill_chunk_days=7,
                backfill_limit=5000,
            ),
            is_active=True,
        )
        db.add(target)
        db.flush()

    link = db.execute(
        select(TargetAccountLink).where(
            TargetAccountLink.target_id == target.id,
            TargetAccountLink.account_id == account.id,
        )
    ).scalar_one_or_none()
    if not link:
        db.add(
            TargetAccountLink(
                target_id=target.id,
                account_id=account.id,
                is_active=True,
                auto_detected=False,
                last_checked_at=dt.datetime.now(dt.UTC),
            )
        )
    else:
        link.is_active = True
        link.last_checked_at = dt.datetime.now(dt.UTC)

    db.commit()
    return RedirectResponse(url=f"/modules/telegram/accounts/{account_id}", status_code=302)


@router.post("/modules/telegram/accounts/start-auth")
def start_telegram_account_auth(
    request: Request,
    label: str = Form(...),
    api_id: str = Form(...),
    api_hash: str = Form(...),
    phone: str = Form(...),
    hourly_limit: int = Form(120),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    existing = db.execute(select(ParserAccount).where(ParserAccount.label == label.strip())).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Акаунт з такою міткою вже існує")

    try:
        temp_session_string, phone_code_hash = asyncio.run(_telegram_send_code(api_id.strip(), api_hash.strip(), phone.strip()))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не вдалося надіслати код: {exc}") from exc

    if not phone_code_hash:
        raise HTTPException(status_code=400, detail="Не вдалося отримати phone_code_hash")

    auth = TelegramAuthSession(
        label=label.strip(),
        hourly_limit=max(int(hourly_limit), 1),
        api_id=api_id.strip(),
        api_hash=api_hash.strip(),
        phone=phone.strip(),
        temp_session_string=temp_session_string,
        phone_code_hash=phone_code_hash,
        is_completed=False,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15),
    )
    db.add(auth)
    db.commit()
    return RedirectResponse(url="/modules/telegram", status_code=302)


@router.post("/modules/telegram/accounts/verify-auth")
def verify_telegram_account_auth(
    request: Request,
    auth_id: int = Form(...),
    code: str = Form(...),
    password: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    auth = db.get(TelegramAuthSession, auth_id)
    if not auth or auth.is_completed:
        raise HTTPException(status_code=404, detail="Сесія авторизації не знайдена")
    if auth.expires_at < dt.datetime.now(dt.UTC):
        db.delete(auth)
        db.commit()
        raise HTTPException(status_code=400, detail="Сесія авторизації прострочена. Запустіть знову.")

    existing = db.execute(select(ParserAccount).where(ParserAccount.label == auth.label)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Акаунт з такою міткою вже існує")

    try:
        session_string = asyncio.run(
            _telegram_verify_code(
                api_id=auth.api_id,
                api_hash=auth.api_hash,
                phone=auth.phone,
                temp_session_string=auth.temp_session_string,
                phone_code_hash=auth.phone_code_hash,
                code=code,
                password=password or None,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не вдалося підтвердити код: {exc}") from exc

    account = ParserAccount(
        parser_type=ParserType.telegram,
        label=auth.label,
        credentials={
            "api_id": auth.api_id,
            "api_hash": auth.api_hash,
            "session_string": session_string,
        },
        hourly_limit=auth.hourly_limit,
    )
    db.add(account)
    auth.is_completed = True
    db.commit()
    return RedirectResponse(url="/modules/telegram", status_code=302)


@router.post("/modules/{parser_name}/accounts")
def create_module_account(
    parser_name: str,
    request: Request,
    label: str = Form(...),
    api_id: str = Form(""),
    api_hash: str = Form(""),
    session_string: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    login_page_path: str = Form("/login/"),
    login_submit_path: str = Form("/login/login"),
    hourly_limit: int = Form(120),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = _require_parser_type(parser_name)
    meta = _get_module_meta(parser_name)
    if not meta["supports_accounts"]:
        raise HTTPException(status_code=400, detail="Цей модуль не використовує акаунти")
    if parser_type == ParserType.telegram:
        raise HTTPException(
            status_code=400,
            detail="Для Telegram використовуйте авторизацію через код в модулі Telegram",
        )

    credentials = {}
    if parser_type == ParserType.darknet:
        credentials = {
            "username": username.strip(),
            "password": password.strip(),
            "login_page_path": login_page_path.strip() or "/login/",
            "login_submit_path": login_submit_path.strip() or "/login/login",
        }

    account = ParserAccount(
        parser_type=parser_type,
        label=label.strip(),
        credentials=credentials,
        hourly_limit=max(int(hourly_limit), 1),
    )
    db.add(account)
    db.commit()
    return RedirectResponse(url=f"/modules/{parser_name}", status_code=302)


@router.post("/modules/{parser_name}/links")
def create_module_link(
    parser_name: str,
    request: Request,
    target_id: int = Form(...),
    account_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = _require_parser_type(parser_name)
    meta = _get_module_meta(parser_name)
    if not meta["supports_links"]:
        raise HTTPException(status_code=400, detail="Цей модуль не використовує прив'язки акаунтів")

    target = db.get(Target, target_id)
    account = db.get(ParserAccount, account_id)
    if not target or not account:
        raise HTTPException(status_code=404, detail="Ціль або акаунт не знайдено")
    if target.parser_type != parser_type or account.parser_type != parser_type:
        raise HTTPException(status_code=400, detail="Невірний тип модуля для цілі або акаунта")

    existing = db.execute(
        select(TargetAccountLink).where(TargetAccountLink.target_id == target_id, TargetAccountLink.account_id == account_id)
    ).scalar_one_or_none()
    if not existing:
        db.add(TargetAccountLink(target_id=target_id, account_id=account_id, is_active=True, auto_detected=False))
    else:
        existing.is_active = True
    db.commit()
    return RedirectResponse(url=f"/modules/{parser_name}", status_code=302)


@router.get("/targets", response_class=HTMLResponse)
def targets_page(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    targets = db.execute(select(Target).order_by(desc(Target.created_at))).scalars().all()
    accounts = db.execute(select(ParserAccount).order_by(ParserAccount.label)).scalars().all()
    links = db.execute(select(TargetAccountLink)).scalars().all()
    return templates.TemplateResponse(
        "targets.html",
        {"request": request, "user": user, "targets": targets, "accounts": accounts, "links": links, "parser_types": ParserType},
    )


@router.post("/targets")
def create_target(
    request: Request,
    parser_type: ParserType = Form(...),
    name: str = Form(...),
    identifier: str = Form(...),
    limit: int = Form(200),
    poll_interval_seconds: int = Form(300),
    live_enabled: bool = Form(False),
    gapfill_limit: int = Form(200),
    backfill_enabled: bool = Form(False),
    backfill_mode: str = Form("range"),
    backfill_from: str = Form(""),
    backfill_from_local: str = Form(""),
    backfill_to: str = Form(""),
    backfill_to_local: str = Form(""),
    backfill_chunk_days: int = Form(7),
    backfill_limit: int = Form(5000),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    normalized_backfill_from = _normalize_backfill_datetime_input(backfill_from, backfill_from_local)
    normalized_backfill_to = _normalize_backfill_datetime_input(backfill_to, backfill_to_local)
    config = _telegram_target_config(
        limit=limit,
        poll_interval_seconds=poll_interval_seconds,
        live_enabled=live_enabled,
        gapfill_limit=gapfill_limit,
        backfill_enabled=backfill_enabled,
        backfill_mode=backfill_mode,
        backfill_from=normalized_backfill_from,
        backfill_to=normalized_backfill_to,
        backfill_chunk_days=backfill_chunk_days,
        backfill_limit=backfill_limit,
    )
    target = Target(parser_type=parser_type, name=name.strip(), identifier=identifier.strip(), config=config)
    db.add(target)
    db.commit()
    return RedirectResponse(url="/targets", status_code=302)


@router.post("/accounts")
def create_account(
    request: Request,
    parser_type: ParserType = Form(...),
    label: str = Form(...),
    api_id: str = Form(""),
    api_hash: str = Form(""),
    session_string: str = Form(""),
    hourly_limit: int = Form(120),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    credentials = {}
    if parser_type == ParserType.telegram:
        credentials = {
            "api_id": api_id.strip(),
            "api_hash": api_hash.strip(),
            "session_string": session_string.strip(),
        }

    account = ParserAccount(
        parser_type=parser_type,
        label=label.strip(),
        credentials=credentials,
        hourly_limit=max(int(hourly_limit), 1),
    )
    db.add(account)
    db.commit()
    return RedirectResponse(url="/targets", status_code=302)


@router.post("/links")
def create_link(
    request: Request,
    target_id: int = Form(...),
    account_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    existing = db.execute(
        select(TargetAccountLink).where(TargetAccountLink.target_id == target_id, TargetAccountLink.account_id == account_id)
    ).scalar_one_or_none()
    if not existing:
        db.add(TargetAccountLink(target_id=target_id, account_id=account_id, is_active=True, auto_detected=False))
    else:
        existing.is_active = True
    db.commit()
    return RedirectResponse(url="/targets", status_code=302)


@router.post("/scheduler/run")
def run_scheduler_ui(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    schedule_once(db)
    db.commit()
    return _redirect_back(request, fallback="/")


@router.post("/modules/{parser_name}/jobs/retry-failed")
def retry_failed_jobs_ui(
    parser_name: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = _require_parser_type(parser_name)
    now = dt.datetime.now(dt.UTC)

    failed_jobs = (
        db.execute(select(ParseJob).where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.failed))
        .scalars()
        .all()
    )
    for job in failed_jobs:
        job.status = JobStatus.retry
        job.attempt = 0
        job.run_after = now
        job.last_error = None
        job.locked_by = None
        job.lock_expires_at = None
        job.finished_at = None

    db.commit()
    return _redirect_back(request, fallback=f"/modules/{parser_name}")


@router.post("/modules/telegram/accounts/reset-cooldown")
def reset_telegram_account_cooldown_ui(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    accounts = (
        db.execute(select(ParserAccount).where(ParserAccount.parser_type == ParserType.telegram, ParserAccount.is_active.is_(True)))
        .scalars()
        .all()
    )
    for account in accounts:
        account.cooldown_until = None
        account.fail_count = 0
        account.last_error = None
        account.health_score = max(account.health_score, 80.0)

    db.commit()
    return _redirect_back(request, fallback="/modules/telegram")


@router.post("/telegram/sync-memberships")
def sync_telegram_ui(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    sync_telegram_memberships(db)
    db.commit()
    return RedirectResponse(url="/modules/telegram", status_code=302)
