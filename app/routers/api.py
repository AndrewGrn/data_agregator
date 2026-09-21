import datetime as dt
import asyncio
import re
import secrets
import shlex
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import get_settings
from app.darknet.adapters import list_darknet_adapters, normalize_darknet_adapter
from app.darknet.browser_state import parse_storage_state, summarize_storage_state
from app.darknet.detection import detect_adapter
from app.deps import get_current_admin, get_current_user
from app.db import get_db
from app.models import (
    DarknetUser,
    DarknetUserMembership,
    DarknetAuthToken,
    JobStatus,
    ParseJob,
    ParserAccount,
    ParserType,
    RawEvent,
    RegistrationToken,
    Target,
    TargetAccountLink,
    TelegramMembership,
    TelegramOffset,
    TelegramUser,
    User,
    UserRole,
)
from app.plugins.registry import plugin_registry
from app.schemas import CreateAccountRequest, CreateTargetRequest, LinkAccountRequest
from app.security import (
    build_totp_uri,
    generate_registration_token,
    generate_totp_secret,
    hash_token,
    hash_password,
    verify_password,
    verify_totp_code,
)
from app.services.darknet_profiles import upsert_darknet_profile_from_event
from app.services.search_autosync import get_search_autosync_state
from app.services.search_index import search_index
from app.services.scheduler import schedule_once, schedule_target_once, sync_telegram_memberships
from app.services.telegram_accounts import (
    account_parallel_limits,
    compute_account_load_score,
    find_dialog_match,
    normalize_telegram_identifier,
    parse_bulk_targets_input,
    refresh_account_dialogs_sync,
    refresh_account_session_info_sync,
)

router = APIRouter(prefix="/api", tags=["api"])
KYIV_TZ = ZoneInfo("Europe/Kyiv")
settings = get_settings()


def _format_kyiv_datetime(value: dt.datetime | None) -> str:
    if not value:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M:%S")


def _coerce_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _is_telegram_account_available_for_schedule(account: ParserAccount, now: dt.datetime) -> bool:
    if not bool(account.is_active):
        return False
    if account.cooldown_until and account.cooldown_until > now:
        return False
    if float(account.health_score or 0.0) < 20.0:
        return False
    if account.hour_window_start and (now - account.hour_window_start) < dt.timedelta(hours=1):
        return int(account.hour_window_count or 0) < max(int(account.hourly_limit or 1), 1)
    return True


def _normalize_username(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Логін не може бути порожнім")
    if len(cleaned) > 64:
        raise HTTPException(status_code=400, detail="Логін занадто довгий")
    return cleaned


def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned:
        return None
    if "@" not in cleaned or len(cleaned) > 255:
        raise HTTPException(status_code=400, detail="Некоректний email")
    return cleaned


def _normalize_full_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > 128:
        raise HTTPException(status_code=400, detail="Ім'я занадто довге")
    return cleaned


def _job_kind_from_key(job_key: str | None) -> str:
    key = str(job_key or "")
    if key.startswith("darknet:discover:"):
        return "Пошук тем"
    if key.startswith("darknet:thread:"):
        return "Парсинг теми"
    if key.startswith("gapfill:"):
        return "Добір пропусків"
    if key.startswith("poll:"):
        return "Повідомлення"
    if key.startswith("participants:"):
        return "Учасники"
    if key.startswith("backfill-full:"):
        return "Повна історія"
    if key.startswith("backfill:"):
        return "Історія"
    return key or "-"


def _darknet_event_type_text(event_type: str) -> str:
    normalized = str(event_type or "").strip().lower()
    if normalized == "forum_post":
        return "Повідомлення"
    if normalized == "forum_user":
        return "Профіль"
    if normalized == "thread_summary":
        return "Підсумок теми"
    if normalized == "darknet_discovery":
        return "Пошук тем"
    return normalized or "-"


def _darknet_auth_mode_text(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"browser_state", "storage_state", "cookies"}:
        return "browser_state"
    if mode in {"form_login", "password"}:
        return "form_login"
    return "form_login"


def _normalize_darknet_username(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    cleaned = raw.removeprefix("@").strip().lower()
    return cleaned or None


def _darknet_account_auth_snapshot(credentials: dict | None) -> tuple[str, str, int, int]:
    creds = credentials if isinstance(credentials, dict) else {}
    mode = _darknet_auth_mode_text(str(creds.get("auth_mode") or ""))
    storage_state = creds.get("storage_state")
    if storage_state is None and isinstance(creds.get("storage_state_json"), str):
        try:
            storage_state = parse_storage_state(str(creds.get("storage_state_json") or ""))
        except Exception:
            storage_state = None
    summary = summarize_storage_state(storage_state if isinstance(storage_state, dict) else None)
    cookies_count = int(summary["cookies_count"])
    origins_count = int(summary["origins_count"])
    username = str(creds.get("username") or "").strip()
    if mode == "browser_state":
        if cookies_count > 0:
            return mode, f"Browser state: {cookies_count} cookies", cookies_count, origins_count
        return mode, "Browser state не завантажено", cookies_count, origins_count
    if username:
        return mode, f"Login: {username}", cookies_count, origins_count
    return mode, "Login/password", cookies_count, origins_count


def _request_client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip() or None
    if request.client and request.client.host:
        return str(request.client.host)
    return None


def _default_telegram_target_config() -> dict:
    return {
        "limit": 200,
        "poll_interval_seconds": 300,
        "live_enabled": True,
        "gapfill_limit": 200,
        "backfill_limit": 5000,
        "backfill_full_batch_size": 300,
        "comments_enabled": True,
        "gapfill_comments_enabled": True,
        "backfill_comments_enabled": True,
        "comments_limit": 20,
        "comments_depth": 2,
        "comments_recheck_posts": 30,
        "participants_sync_enabled": True,
        "participants_sync_interval_seconds": 3600,
        "participants_limit": 1000,
        "is_risky": False,
        "risk_label": "",
        "backfill": {
            "enabled": False,
            "mode": "range",
            "from": None,
            "to": None,
            "chunk_days": 7,
        },
    }


def _parse_datetime_input(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return dt.datetime.fromisoformat(normalized)


def _normalize_backfill_datetime_input(value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    try:
        parsed = _parse_datetime_input(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Невірний формат дати/часу: {raw_value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV_TZ)
    return parsed.astimezone(dt.UTC).isoformat()


def _telegram_target_config(
    limit: int,
    poll_interval_seconds: int,
    live_enabled: bool,
    gapfill_limit: int,
    participants_sync_enabled: bool,
    participants_sync_interval_seconds: int,
    participants_limit: int,
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
    is_risky: bool = False,
    risk_label: str = "",
) -> dict:
    normalized_mode = str(backfill_mode or "range").strip().lower()
    if normalized_mode not in {"range", "full"}:
        normalized_mode = "range"
    normalized_from = str(backfill_from or "").strip() or None
    normalized_to = str(backfill_to or "").strip() or None
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
        "is_risky": bool(is_risky),
        "risk_label": str(risk_label or "").strip(),
        "backfill": {
            "enabled": bool(backfill_enabled),
            "mode": normalized_mode,
            "from": normalized_from,
            "to": normalized_to,
            "chunk_days": max(int(backfill_chunk_days), 1),
        },
    }


def _telegram_account_session_snapshot(credentials: dict) -> tuple[bool | None, str, str, str | None, str | None, int | None]:
    creds = dict(credentials or {})
    status = creds.get("session_status")
    if not isinstance(status, dict):
        return None, "Не перевірено", "-", None, None, None

    alive_raw = status.get("alive")
    alive = bool(alive_raw) if isinstance(alive_raw, bool) else None
    if alive is True:
        status_text = "Живий"
    elif alive is False:
        status_text = "Не живий"
    else:
        status_text = "Невідомо"

    checked_at_raw = str(status.get("checked_at") or "").strip()
    checked_at = None
    if checked_at_raw:
        try:
            checked_at = dt.datetime.fromisoformat(checked_at_raw.replace("Z", "+00:00"))
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=dt.UTC)
        except Exception:
            checked_at = None

    last_check_text = _format_kyiv_datetime(checked_at)
    phone = str(status.get("phone") or "").strip() or None
    username = str(status.get("username") or "").strip()
    if username and not username.startswith("@"):
        username = f"@{username}"
    username = username or None
    user_id = status.get("user_id")
    try:
        user_id = int(user_id) if user_id is not None else None
    except Exception:
        user_id = None

    return alive, status_text, last_check_text, phone, username, user_id


_TG_USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{3,64})")


def _normalize_telegram_username(value: str) -> str | None:
    raw = str(value or "").strip().removeprefix("@").lower()
    if not raw:
        return None
    if not re.fullmatch(r"[a-z0-9_]{3,64}", raw):
        return None
    return raw


def _extract_telegram_usernames(text: str) -> list[str]:
    found = {_normalize_telegram_username(item) for item in _TG_USERNAME_RE.findall(str(text or ""))}
    return sorted([item for item in found if item])


def _target_risk_meta(target: Target) -> tuple[bool, str | None]:
    cfg = dict(target.config or {})
    label = str(cfg.get("risk_label") or "").strip()
    risky = bool(cfg.get("is_risky", False)) or bool(label)
    return risky, (label or None)


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
            raise HTTPException(status_code=400, detail="Потрібен пароль 2FA для Telegram")
        await client.sign_in(password=password)

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(status_code=400, detail="Авторизація Telegram не завершена")

    session_string = client.session.save()
    await client.disconnect()
    return session_string


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


def _is_telegram_floodwait_error(error: str | None) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    markers = [
        "floodwait",
        "flood wait",
        "a wait of",
        "too many requests",
    ]
    return any(marker in text for marker in markers)


def _is_admin(user: User) -> bool:
    return bool(user.is_admin or user.role == UserRole.admin)


def _ensure_target_access(target: Target | None, user: User) -> Target:
    if not target:
        raise HTTPException(status_code=404, detail="Ціль не знайдена")
    if _is_admin(user):
        return target
    if target.owner_user_id is None or int(target.owner_user_id) != int(user.id):
        raise HTTPException(status_code=403, detail="Немає доступу до цієї цілі")
    return target


def _ensure_account_access(account: ParserAccount | None, user: User) -> ParserAccount:
    if not account:
        raise HTTPException(status_code=404, detail="Акаунт не знайдений")
    if _is_admin(user):
        return account
    if account.owner_user_id is None or int(account.owner_user_id) != int(user.id):
        raise HTTPException(status_code=403, detail="Немає доступу до цього акаунта")
    return account


def _owned_targets_stmt(user: User):
    stmt = select(Target)
    if not _is_admin(user):
        stmt = stmt.where(Target.owner_user_id == int(user.id))
    return stmt


def _owned_accounts_stmt(user: User):
    stmt = select(ParserAccount)
    if not _is_admin(user):
        stmt = stmt.where(ParserAccount.owner_user_id == int(user.id))
    return stmt


def _owned_jobs_stmt(user: User):
    stmt = select(ParseJob)
    if not _is_admin(user):
        stmt = stmt.where(ParseJob.owner_user_id == int(user.id))
    return stmt


def _owned_events_stmt(user: User):
    stmt = select(RawEvent)
    if not _is_admin(user):
        stmt = stmt.where(RawEvent.owner_user_id == int(user.id))
    return stmt


def _ensure_event_access(db: Session, event: RawEvent | None, user: User) -> RawEvent:
    if not event:
        raise HTTPException(status_code=404, detail="Подію не знайдено")
    if _is_admin(user):
        return event
    if event.owner_user_id is not None and int(event.owner_user_id) == int(user.id):
        return event
    target = db.get(Target, event.target_id)
    if target and target.owner_user_id is not None and int(target.owner_user_id) == int(user.id):
        return event
    raise HTTPException(status_code=403, detail="Немає доступу до цієї події")


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/auth/me")
def auth_me(user=Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "is_admin": bool(user.is_admin),
        "role": user.role.value if user.role else ("admin" if user.is_admin else "user"),
        "is_active": bool(user.is_active),
        "totp_confirmed": bool(user.totp_confirmed),
    }


@router.get("/profile/me")
def profile_me(user=Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.value if user.role else ("admin" if user.is_admin else "user"),
        "is_active": bool(user.is_active),
        "totp_confirmed": bool(user.totp_confirmed),
    }


@router.post("/profile/update")
def profile_update(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    username_in = payload.get("username")
    full_name_in = payload.get("full_name")
    email_in = payload.get("email")

    if username_in is not None:
        username = _normalize_username(str(username_in))
        existing = db.execute(select(User).where(User.username == username, User.id != user.id)).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=400, detail="Логін вже зайнятий")
        user.username = username

    if full_name_in is not None:
        user.full_name = _normalize_full_name(full_name_in)

    if email_in is not None:
        email = _normalize_email(email_in)
        if email:
            existing_email = db.execute(select(User).where(User.email == email, User.id != user.id)).scalar_one_or_none()
            if existing_email:
                raise HTTPException(status_code=400, detail="Email вже використовується")
        user.email = email

    db.add(user)
    db.commit()
    return {"ok": True}


@router.post("/profile/change-password")
def profile_change_password(payload: dict, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    current_password = str(payload.get("current_password") or "")
    new_password = str(payload.get("new_password") or "")
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Невірний поточний пароль")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Новий пароль має містити щонайменше 8 символів")
    user.password_hash = hash_password(new_password)
    user.session_version = int(user.session_version or 1) + 1
    db.add(user)
    db.commit()
    request.session["session_version"] = int(user.session_version)
    return {"ok": True}


@router.post("/profile/revoke-sessions")
def profile_revoke_sessions(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    user.session_version = int(user.session_version or 1) + 1
    db.add(user)
    db.commit()
    request.session["session_version"] = int(user.session_version)
    return {"ok": True}


@router.post("/auth/login")
def auth_login(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    otp_code = str(payload.get("otp_code") or "").strip()
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Невірний логін або пароль")
    if not bool(user.is_active):
        raise HTTPException(status_code=403, detail="Користувач деактивований")
    if not bool(user.totp_enabled) or not bool(user.totp_confirmed):
        raise HTTPException(status_code=403, detail="Потрібно завершити налаштування 2FA")
    if not verify_totp_code(user.totp_secret, otp_code):
        raise HTTPException(status_code=401, detail="Невірний 2FA код")
    request.session["user_id"] = user.id
    request.session["session_version"] = int(user.session_version or 1)
    return {
        "ok": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "is_admin": bool(user.is_admin),
            "role": user.role.value if user.role else ("admin" if user.is_admin else "user"),
        },
    }


@router.post("/auth/setup-2fa/start")
def auth_setup_2fa_start(payload: dict, db: Session = Depends(get_db)):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Невірний логін або пароль")
    if not bool(user.is_active):
        raise HTTPException(status_code=403, detail="Користувач деактивований")
    if not user.totp_secret:
        user.totp_secret = generate_totp_secret()
    user.totp_enabled = True
    db.add(user)
    db.commit()
    return {
        "ok": True,
        "username": user.username,
        "otp_secret": user.totp_secret,
        "otp_uri": build_totp_uri(user.totp_secret, user.username),
        "already_confirmed": bool(user.totp_confirmed),
    }


@router.post("/auth/setup-2fa/confirm")
def auth_setup_2fa_confirm(payload: dict, db: Session = Depends(get_db)):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    otp_code = str(payload.get("otp_code") or "").strip()
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Невірний логін або пароль")
    if not bool(user.is_active):
        raise HTTPException(status_code=403, detail="Користувач деактивований")
    if not verify_totp_code(user.totp_secret, otp_code):
        raise HTTPException(status_code=401, detail="Невірний 2FA код")
    user.totp_enabled = True
    user.totp_confirmed = True
    db.add(user)
    db.commit()
    return {"ok": True}


@router.post("/auth/register")
def auth_register(payload: dict, db: Session = Depends(get_db)):
    invite_token = str(payload.get("invite_token") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not invite_token or not username or not password:
        raise HTTPException(status_code=400, detail="invite_token, username і password обов'язкові")

    token_hash = hash_token(invite_token)
    token = db.execute(select(RegistrationToken).where(RegistrationToken.token_hash == token_hash)).scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=400, detail="Невірний токен реєстрації")
    if not token.is_active:
        raise HTTPException(status_code=400, detail="Токен реєстрації неактивний")
    expires_at = _coerce_utc(token.expires_at)
    if expires_at and expires_at < dt.datetime.now(dt.UTC):
        raise HTTPException(status_code=400, detail="Токен реєстрації прострочений")
    if int(token.used_count or 0) >= int(token.max_uses or 1):
        raise HTTPException(status_code=400, detail="Ліміт використань токена вичерпано")

    existing = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Користувач з таким логіном вже існує")

    secret = generate_totp_secret()
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=False,
        role=UserRole.user,
        is_active=True,
        totp_secret=secret,
        totp_enabled=True,
        totp_confirmed=False,
        created_by_token_id=token.id,
    )
    token.used_count = int(token.used_count or 0) + 1
    if token.used_count >= int(token.max_uses or 1):
        token.is_active = False
    db.add(user)
    db.add(token)
    db.commit()
    db.refresh(user)

    # store who consumed this token most recently
    token.used_by_user_id = user.id
    db.add(token)
    db.commit()

    return {
        "ok": True,
        "username": user.username,
        "otp_secret": secret,
        "otp_uri": build_totp_uri(secret, user.username),
        "message": "Реєстрація створена. Підтвердіть 2FA кодом.",
    }


@router.post("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/admin/registration-tokens")
def admin_list_registration_tokens(db: Session = Depends(get_db), user=Depends(get_current_admin)):
    creator_ids: set[int] = set()
    consumer_ids: set[int] = set()
    tokens = (
        db.execute(select(RegistrationToken).order_by(desc(RegistrationToken.created_at)).limit(200))
        .scalars()
        .all()
    )
    for token in tokens:
        if token.created_by_user_id:
            creator_ids.add(int(token.created_by_user_id))
        if token.used_by_user_id:
            consumer_ids.add(int(token.used_by_user_id))
    users = (
        db.execute(select(User).where(User.id.in_(list(creator_ids | consumer_ids)))).scalars().all()
        if (creator_ids or consumer_ids)
        else []
    )
    usernames = {int(item.id): item.username for item in users}
    return [
        {
            "id": token.id,
            "label": token.label,
            "is_active": bool(token.is_active),
            "max_uses": int(token.max_uses or 1),
            "used_count": int(token.used_count or 0),
            "created_by_user_id": token.created_by_user_id,
            "created_by_username": usernames.get(int(token.created_by_user_id)) if token.created_by_user_id else None,
            "used_by_user_id": token.used_by_user_id,
            "used_by_username": usernames.get(int(token.used_by_user_id)) if token.used_by_user_id else None,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "created_at": token.created_at.isoformat() if token.created_at else None,
            "is_expired": bool(_coerce_utc(token.expires_at) and _coerce_utc(token.expires_at) < dt.datetime.now(dt.UTC)),
        }
        for token in tokens
    ]


@router.post("/admin/registration-tokens")
def admin_create_registration_token(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_admin)):
    label = str(payload.get("label") or "").strip() or None
    max_uses = max(int(payload.get("max_uses") or 1), 1)
    expires_in_hours = int(payload.get("expires_in_hours") or 24)
    expires_at = None
    if expires_in_hours > 0:
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=expires_in_hours)
    raw_token = generate_registration_token()
    item = RegistrationToken(
        label=label,
        token_hash=hash_token(raw_token),
        created_by_user_id=user.id,
        max_uses=max_uses,
        used_count=0,
        expires_at=expires_at,
        is_active=True,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "ok": True,
        "id": item.id,
        "token": raw_token,
        "label": item.label,
        "max_uses": item.max_uses,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
    }


@router.post("/admin/registration-tokens/{token_id}/deactivate")
def admin_deactivate_registration_token(token_id: int, db: Session = Depends(get_db), user=Depends(get_current_admin)):
    token = db.get(RegistrationToken, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Токен реєстрації не знайдено")
    token.is_active = False
    db.add(token)
    db.commit()
    return {"ok": True}


@router.get("/admin/users")
def admin_list_users(db: Session = Depends(get_db), user=Depends(get_current_admin)):
    rows = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    return [
        {
            "id": item.id,
            "username": item.username,
            "full_name": item.full_name,
            "email": item.email,
            "role": item.role.value if item.role else ("admin" if item.is_admin else "user"),
            "is_admin": bool(item.is_admin),
            "is_active": bool(item.is_active),
            "session_version": int(item.session_version or 1),
            "totp_enabled": bool(item.totp_enabled),
            "totp_confirmed": bool(item.totp_confirmed),
            "created_by_token_id": item.created_by_token_id,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in rows
    ]


@router.post("/admin/users/{user_id}")
def admin_update_user(user_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_admin)):
    item = db.get(User, user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Користувача не знайдено")

    role_value = str(payload.get("role") or "").strip().lower()
    if role_value:
        if role_value not in {UserRole.admin.value, UserRole.user.value}:
            raise HTTPException(status_code=400, detail="Невідома роль")
        item.role = UserRole(role_value)
        item.is_admin = bool(item.role == UserRole.admin)

    if "is_active" in payload:
        item.is_active = bool(payload.get("is_active"))

    if "username" in payload:
        username = _normalize_username(str(payload.get("username") or ""))
        existing = db.execute(select(User).where(User.username == username, User.id != item.id)).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=400, detail="Логін вже зайнятий")
        item.username = username

    if "full_name" in payload:
        item.full_name = _normalize_full_name(payload.get("full_name"))

    if "email" in payload:
        email = _normalize_email(payload.get("email"))
        if email:
            existing_email = db.execute(select(User).where(User.email == email, User.id != item.id)).scalar_one_or_none()
            if existing_email:
                raise HTTPException(status_code=400, detail="Email вже використовується")
        item.email = email

    db.add(item)
    db.commit()
    return {"ok": True}


@router.post("/admin/users/{user_id}/reset-password")
def admin_reset_user_password(user_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_admin)):
    item = db.get(User, user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Користувача не знайдено")

    raw_new_password = str(payload.get("new_password") or "").strip()
    generated = False
    if raw_new_password:
        if len(raw_new_password) < 8:
            raise HTTPException(status_code=400, detail="Новий пароль має містити щонайменше 8 символів")
        new_password = raw_new_password
    else:
        new_password = f"Tmp-{secrets.token_urlsafe(10)}"
        generated = True

    item.password_hash = hash_password(new_password)
    item.session_version = int(item.session_version or 1) + 1
    db.add(item)
    db.commit()
    return {"ok": True, "generated": generated, "new_password": new_password}


@router.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_user_sessions(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_admin),
):
    item = db.get(User, user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Користувача не знайдено")
    item.session_version = int(item.session_version or 1) + 1
    db.add(item)
    db.commit()
    if int(user.id) == int(item.id):
        request.session["session_version"] = int(item.session_version)
    return {"ok": True}


@router.get("/admin/resources")
def admin_resources(db: Session = Depends(get_db), user=Depends(get_current_admin)):
    users = db.execute(select(User.id, User.username)).all()
    usernames = {int(uid): uname for uid, uname in users}

    accounts = db.execute(select(ParserAccount).order_by(desc(ParserAccount.created_at)).limit(500)).scalars().all()
    targets = db.execute(select(Target).order_by(desc(Target.created_at)).limit(500)).scalars().all()
    links = db.execute(select(TargetAccountLink).order_by(desc(TargetAccountLink.id)).limit(1000)).scalars().all()

    return {
        "accounts": [
            {
                "id": item.id,
                "label": item.label,
                "parser_type": item.parser_type,
                "owner_user_id": item.owner_user_id,
                "owner_username": usernames.get(int(item.owner_user_id)) if item.owner_user_id else None,
                "is_active": bool(item.is_active),
            }
            for item in accounts
        ],
        "targets": [
            {
                "id": item.id,
                "name": item.name,
                "identifier": item.identifier,
                "parser_type": item.parser_type,
                "owner_user_id": item.owner_user_id,
                "owner_username": usernames.get(int(item.owner_user_id)) if item.owner_user_id else None,
                "is_active": bool(item.is_active),
            }
            for item in targets
        ],
        "links": [
            {
                "id": item.id,
                "target_id": item.target_id,
                "account_id": item.account_id,
                "owner_user_id": item.owner_user_id,
                "owner_username": usernames.get(int(item.owner_user_id)) if item.owner_user_id else None,
                "is_active": bool(item.is_active),
            }
            for item in links
        ],
    }


@router.get("/targets")
def list_targets(db: Session = Depends(get_db), user=Depends(get_current_user)):
    targets = db.execute(_owned_targets_stmt(user).order_by(desc(Target.created_at))).scalars().all()
    return [
        {
            "id": item.id,
            "parser_type": item.parser_type,
            "name": item.name,
            "identifier": item.identifier,
            "owner_user_id": item.owner_user_id,
            "config": item.config,
            "onboarding_status": item.onboarding_status.value,
            "is_active": item.is_active,
        }
        for item in targets
    ]


@router.post("/targets")
def create_target(payload: CreateTargetRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = Target(
        parser_type=payload.parser_type,
        name=payload.name.strip(),
        identifier=payload.identifier.strip(),
        owner_user_id=user.id,
        config=payload.config,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return {"id": target.id}


@router.post("/accounts")
def create_account(payload: CreateAccountRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    account = ParserAccount(
        parser_type=payload.parser_type,
        label=payload.label.strip(),
        owner_user_id=user.id,
        credentials=payload.credentials,
        hourly_limit=max(payload.hourly_limit, 1),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"id": account.id}


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db), user=Depends(get_current_user)):
    accounts = db.execute(_owned_accounts_stmt(user).order_by(ParserAccount.created_at.desc())).scalars().all()
    return [
        {
            "id": a.id,
            "parser_type": a.parser_type,
            "label": a.label,
            "owner_user_id": a.owner_user_id,
            "is_active": a.is_active,
            "health_score": a.health_score,
            "hourly_limit": a.hourly_limit,
            "hour_window_count": a.hour_window_count,
            "cooldown_until": a.cooldown_until.isoformat() if a.cooldown_until else None,
            "success_count": a.success_count,
            "fail_count": a.fail_count,
            "last_error": a.last_error,
            "last_success_at": a.last_success_at.isoformat() if a.last_success_at else None,
        }
        for a in accounts
    ]


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user=Depends(get_current_user)):
    modules: list[dict] = []
    for parser_name in plugin_registry.list_types():
        parser_type = ParserType(parser_name)
        if _is_admin(user):
            targets = db.scalar(select(func.count()).select_from(Target).where(Target.parser_type == parser_type)) or 0
            accounts = (
                db.scalar(select(func.count()).select_from(ParserAccount).where(ParserAccount.parser_type == parser_type))
                or 0
            )
            pending_jobs = (
                db.scalar(
                    select(func.count())
                    .select_from(ParseJob)
                    .where(ParseJob.parser_type == parser_type, ParseJob.status.in_([JobStatus.pending, JobStatus.retry]))
                )
                or 0
            )
            running_jobs = (
                db.scalar(
                    select(func.count())
                    .select_from(ParseJob)
                    .where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.running)
                )
                or 0
            )
            failed_jobs = (
                db.scalar(
                    select(func.count())
                    .select_from(ParseJob)
                    .where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.failed)
                )
                or 0
            )
            raw_events = db.scalar(select(func.count()).select_from(RawEvent).where(RawEvent.parser_type == parser_type)) or 0
            last_event_at = db.scalar(select(func.max(RawEvent.created_at)).where(RawEvent.parser_type == parser_type))
        else:
            owner_id = int(user.id)
            targets = (
                db.scalar(
                    select(func.count()).select_from(Target).where(
                        Target.parser_type == parser_type,
                        Target.owner_user_id == owner_id,
                    )
                )
                or 0
            )
            accounts = (
                db.scalar(
                    select(func.count()).select_from(ParserAccount).where(
                        ParserAccount.parser_type == parser_type,
                        ParserAccount.owner_user_id == owner_id,
                    )
                )
                or 0
            )
            pending_jobs = (
                db.scalar(
                    select(func.count())
                    .select_from(ParseJob)
                    .where(
                        ParseJob.parser_type == parser_type,
                        ParseJob.owner_user_id == owner_id,
                        ParseJob.status.in_([JobStatus.pending, JobStatus.retry]),
                    )
                )
                or 0
            )
            running_jobs = (
                db.scalar(
                    select(func.count())
                    .select_from(ParseJob)
                    .where(
                        ParseJob.parser_type == parser_type,
                        ParseJob.owner_user_id == owner_id,
                        ParseJob.status == JobStatus.running,
                    )
                )
                or 0
            )
            failed_jobs = (
                db.scalar(
                    select(func.count())
                    .select_from(ParseJob)
                    .where(
                        ParseJob.parser_type == parser_type,
                        ParseJob.owner_user_id == owner_id,
                        ParseJob.status == JobStatus.failed,
                    )
                )
                or 0
            )
            raw_events = (
                db.scalar(
                    select(func.count()).select_from(RawEvent).where(
                        RawEvent.parser_type == parser_type,
                        RawEvent.owner_user_id == owner_id,
                    )
                )
                or 0
            )
            last_event_at = db.scalar(
                select(func.max(RawEvent.created_at)).where(
                    RawEvent.parser_type == parser_type,
                    RawEvent.owner_user_id == owner_id,
                )
            )

        modules.append(
            {
                "parser_type": parser_name,
                "title": "Telegram Модуль" if parser_name == "telegram" else "Darknet Модуль",
                "targets": int(targets),
                "accounts": int(accounts),
                "pending_jobs": int(pending_jobs),
                "running_jobs": int(running_jobs),
                "failed_jobs": int(failed_jobs),
                "raw_events": int(raw_events),
                "last_event_at": last_event_at.isoformat() if last_event_at else None,
                "last_event_text": _format_kyiv_datetime(last_event_at),
            }
        )
    return {"modules": modules}


@router.get("/modules/telegram")
def telegram_module_data(
    refresh_session_status: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = ParserType.telegram
    targets_stmt = select(Target).where(Target.parser_type == parser_type)
    accounts_stmt = select(ParserAccount).where(ParserAccount.parser_type == parser_type)
    jobs_stmt = select(ParseJob).where(
        ParseJob.parser_type == parser_type,
        ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry, JobStatus.failed]),
    )
    if not _is_admin(user):
        owner_id = int(user.id)
        targets_stmt = targets_stmt.where(Target.owner_user_id == owner_id)
        accounts_stmt = accounts_stmt.where(ParserAccount.owner_user_id == owner_id)
        jobs_stmt = jobs_stmt.where(ParseJob.owner_user_id == owner_id)

    targets = db.execute(targets_stmt.order_by(desc(Target.created_at))).scalars().all()
    accounts = db.execute(accounts_stmt.order_by(desc(ParserAccount.created_at))).scalars().all()
    jobs = db.execute(jobs_stmt.order_by(desc(ParseJob.created_at)).limit(100)).scalars().all()

    if refresh_session_status and accounts:
        now_refresh = dt.datetime.now(dt.UTC)
        for account in accounts:
            creds = dict(account.credentials or {})
            status = refresh_account_session_info_sync(creds)
            status["checked_at"] = now_refresh.isoformat()
            creds["session_status"] = status
            if status.get("phone"):
                creds["phone"] = status.get("phone")
            if status.get("username"):
                creds["username"] = status.get("username")
            account.credentials = creds
            db.add(account)
        db.commit()
        accounts = db.execute(accounts_stmt.order_by(desc(ParserAccount.created_at))).scalars().all()

    target_ids = [target.id for target in targets]
    account_ids = [account.id for account in accounts]
    accounts_by_id = {int(account.id): account for account in accounts}
    target_accounts_by_target_id: dict[int, list[dict]] = {}
    target_account_ids_by_target_id: dict[int, list[int]] = {}
    if target_ids and account_ids:
        link_rows = db.execute(
            select(TargetAccountLink.target_id, ParserAccount.id, ParserAccount.label)
            .join(ParserAccount, ParserAccount.id == TargetAccountLink.account_id)
            .where(
                TargetAccountLink.target_id.in_(target_ids),
                TargetAccountLink.account_id.in_(account_ids),
                TargetAccountLink.is_active.is_(True),
                ParserAccount.parser_type == parser_type,
            )
            .order_by(ParserAccount.label.asc(), ParserAccount.id.asc())
        ).all()
        for target_id, account_id, account_label in link_rows:
            bucket = target_accounts_by_target_id.setdefault(int(target_id), [])
            bucket.append({"id": int(account_id), "label": str(account_label)})
            ids_bucket = target_account_ids_by_target_id.setdefault(int(target_id), [])
            ids_bucket.append(int(account_id))

    offset_rows = (
        db.execute(select(TelegramOffset).where(TelegramOffset.target_id.in_(target_ids))).scalars().all()
        if target_ids
        else []
    )
    offsets_by_target: dict[int, dict] = {}
    for offset in offset_rows:
        bucket = offsets_by_target.setdefault(
            int(offset.target_id),
            {"max_message_id": 0, "accounts": 0, "caught_up_accounts": 0, "updated_at": None},
        )
        bucket["accounts"] += 1
        bucket["max_message_id"] = max(int(bucket["max_message_id"]), int(offset.max_message_id or 0))
        if offset.is_caught_up:
            bucket["caught_up_accounts"] += 1
        if offset.updated_at and (bucket["updated_at"] is None or offset.updated_at > bucket["updated_at"]):
            bucket["updated_at"] = offset.updated_at

    now = dt.datetime.now(dt.UTC)
    target_rows: list[dict] = []
    target_name_map = {target.id: target.name for target in targets}
    account_name_map = {account.id: account.label for account in accounts}

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
        events_count = (
            db.scalar(
                select(func.count()).select_from(RawEvent).where(RawEvent.parser_type == parser_type, RawEvent.target_id == target.id)
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
        target_offsets = offsets_by_target.get(target.id, {})
        if not target.is_active:
            process_text = "Зупинено"
        elif running > 0:
            process_text = f"Виконується ({running})"
        elif queued > 0:
            process_text = f"У черзі ({queued})"
        elif failed > 0:
            process_text = f"Є помилки ({failed})"
        else:
            process_text = "Працює за розкладом" if last_success else "Очікує першого запуску"

        process_wait_text = ""
        if target.is_active and running == 0 and queued == 0 and failed == 0 and not last_success:
            linked_account_ids = target_account_ids_by_target_id.get(int(target.id), [])
            if not linked_account_ids:
                process_wait_text = "Немає привʼязаного акаунта."
            else:
                available_accounts = [
                    acc_id
                    for acc_id in linked_account_ids
                    if (account := accounts_by_id.get(int(acc_id))) is not None
                    and _is_telegram_account_available_for_schedule(account, now)
                ]
                if not available_accounts:
                    process_wait_text = "Немає вільного акаунта (ліміт/пауза/health)."
                else:
                    process_wait_text = "Очікує постановку задачі планувальником."

        target_rows.append(
            {
                "id": target.id,
                "name": target.name,
                "identifier": target.identifier,
                "linked_accounts": target_accounts_by_target_id.get(int(target.id), []),
                "config": target.config or {},
                "is_active": bool(target.is_active),
                "running": int(running),
                "queued": int(queued),
                "failed": int(failed),
                "events_count": int(events_count),
                "last_success_at": last_success.isoformat() if last_success else None,
                "last_success_text": _format_kyiv_datetime(last_success),
                "process_text": process_text,
                "process_wait_text": process_wait_text,
                "ingest_mode": "live+recovery" if bool((target.config or {}).get("live_enabled", True)) else "polling",
                "offset_max_message_id": int(target_offsets.get("max_message_id") or 0),
                "offset_accounts_text": (
                    f"{int(target_offsets.get('caught_up_accounts') or 0)}/{int(target_offsets.get('accounts') or 0)}"
                    if int(target_offsets.get("accounts") or 0) > 0
                    else "-"
                ),
            }
        )

    account_rows: list[dict] = []
    account_rows_by_id: dict[int, dict] = {}
    for account in accounts:
        queued_jobs = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(
                    ParseJob.parser_type == parser_type,
                    ParseJob.account_id == account.id,
                    ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
                )
            )
            or 0
        )
        dialogs_count = 0
        creds = dict(account.credentials or {})
        dialogs = creds.get("dialogs_cache")
        if isinstance(dialogs, list):
            dialogs_count = len(dialogs)
        utilization = min(100, int((int(account.hour_window_count or 0) / max(int(account.hourly_limit or 1), 1)) * 100))
        parallel_jobs, backfill_parallel_jobs = account_parallel_limits(
            account=account,
            default_parallel_jobs=settings.telegram_parallel_jobs_per_account,
            default_backfill_parallel_jobs=settings.telegram_backfill_parallel_jobs_per_account,
        )
        alive, status_text, last_check_text, phone, username, user_id = _telegram_account_session_snapshot(creds)
        session_error = str((creds.get("session_status") or {}).get("error") or "").strip() or None
        account_rows.append(
            {
                "id": account.id,
                "label": account.label,
                "is_active": bool(account.is_active),
                "utilization": int(utilization),
                "queued_jobs": int(queued_jobs),
                "dialogs_count": int(dialogs_count),
                "parallel_jobs": int(parallel_jobs),
                "backfill_parallel_jobs": int(backfill_parallel_jobs),
                "last_success_text": _format_kyiv_datetime(account.last_success_at),
                "session_alive": alive,
                "session_status_text": status_text,
                "session_last_check_text": last_check_text,
                "phone": phone,
                "username": username,
                "telegram_user_id": user_id,
                "session_error": session_error,
                "activity_text": "",
            }
        )
        account_rows_by_id[int(account.id)] = account_rows[-1]

    account_map = {account.id: account for account in accounts}
    running_count_rows = (
        db.execute(
            select(ParseJob.account_id, func.count(ParseJob.id))
            .where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.running, ParseJob.account_id.is_not(None))
            .group_by(ParseJob.account_id)
        ).all()
    )
    running_counts_by_account = {int(acc_id): int(cnt) for acc_id, cnt in running_count_rows if acc_id is not None}
    running_descriptions_by_account: dict[int, list[str]] = {}
    wait_reason_by_account: dict[int, str] = {}

    job_rows: list[dict] = []
    for job in jobs:
        reason = "-"
        if job.status in (JobStatus.pending, JobStatus.retry):
            if job.run_after and job.run_after > now:
                reason = f"Чекає таймер до {_format_kyiv_datetime(job.run_after)}"
            elif _is_telegram_network_error(job.last_error):
                reason = "Мережевий збій Telegram, автоповтор задачі"
            elif job.account_id is not None:
                account = account_map.get(int(job.account_id))
                if account:
                    running_count = int(running_counts_by_account.get(int(job.account_id), 0))
                    parallel_jobs, backfill_parallel_jobs = account_parallel_limits(
                        account=account,
                        default_parallel_jobs=settings.telegram_parallel_jobs_per_account,
                        default_backfill_parallel_jobs=settings.telegram_backfill_parallel_jobs_per_account,
                    )
                    job_key = str(job.job_key or "")
                    limit = parallel_jobs
                    if job_key.startswith("backfill:") or job_key.startswith("backfill-full:"):
                        limit = backfill_parallel_jobs
                    elif job_key.startswith("participants:"):
                        limit = 1
                    if running_count >= limit:
                        reason = f"Чекає вільний слот акаунта ({running_count}/{limit})"
                    else:
                        reason = "Чекає чергу воркера"
                else:
                    reason = "Акаунт не знайдено"
            else:
                reason = "Чекає вільний воркер"
        elif job.status == JobStatus.running:
            reason = "Задача виконується"
        elif job.status == JobStatus.failed and job.last_error:
            reason = "Завершено з помилкою"

        job_rows.append(
            {
                "id": job.id,
                "target_id": job.target_id,
                "target_name": target_name_map.get(job.target_id, f"Ціль #{job.target_id}"),
                "account_id": job.account_id,
                "account_label": account_name_map.get(job.account_id, str(job.account_id) if job.account_id else "-"),
                "status": job.status.value,
                "job_type": _job_kind_from_key(job.job_key),
                "attempt": int(job.attempt),
                "error": job.last_error if job.status in (JobStatus.failed, JobStatus.retry) else None,
                "wait_reason": reason,
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
                "updated_at_text": _format_kyiv_datetime(job.updated_at),
                "error_at_text": _format_kyiv_datetime(job.updated_at)
                if job.last_error and job.status in (JobStatus.failed, JobStatus.retry)
                else "-",
            }
        )
        if job.account_id is not None:
            account_id = int(job.account_id)
            if job.status == JobStatus.running:
                target_label = target_name_map.get(job.target_id, f"Ціль #{job.target_id}")
                running_descriptions_by_account.setdefault(account_id, []).append(f"{_job_kind_from_key(job.job_key)}: {target_label}")
            elif job.status in (JobStatus.pending, JobStatus.retry) and account_id not in wait_reason_by_account:
                wait_reason_by_account[account_id] = reason

    for account in accounts:
        row = account_rows_by_id.get(int(account.id))
        if row is None:
            continue

        if row.get("session_alive") is False:
            activity_text = "Сесія неактивна"
        elif account.cooldown_until and account.cooldown_until > now:
            if _is_telegram_floodwait_error(account.last_error):
                activity_text = f"FloodWait до {_format_kyiv_datetime(account.cooldown_until)}"
            else:
                activity_text = f"Пауза до {_format_kyiv_datetime(account.cooldown_until)}"
        else:
            running_descriptions = running_descriptions_by_account.get(int(account.id), [])
            if running_descriptions:
                if len(running_descriptions) > 1:
                    activity_text = f"Виконує: {running_descriptions[0]} (+{len(running_descriptions) - 1})"
                else:
                    activity_text = f"Виконує: {running_descriptions[0]}"
            elif int(row.get("queued_jobs") or 0) > 0:
                wait_reason = wait_reason_by_account.get(int(account.id))
                activity_text = f"Очікує: {wait_reason}" if wait_reason else "Очікує чергу"
            elif not bool(row.get("is_active")):
                activity_text = "Вимкнено"
            else:
                activity_text = "Простій (очікує розклад)"

        row["activity_text"] = activity_text

    return {
        "summary": {
            "targets": len(targets),
            "accounts": len(accounts),
            "events_count": int(sum(item["events_count"] for item in target_rows)),
            "problem_jobs": int(sum(1 for item in job_rows if item["status"] in {"failed", "retry"})),
        },
        "targets": target_rows,
        "accounts": account_rows,
        "jobs": job_rows,
    }


@router.get("/modules/{parser_name}/jobs/error-log")
def module_jobs_error_log(
    parser_name: str,
    limit: int = 200,
    target_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        parser_type = ParserType(parser_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="parser not found") from exc

    clamped_limit = max(1, min(int(limit or 200), 500))
    stmt = (
        select(ParseJob, Target.name, ParserAccount.label)
        .outerjoin(Target, Target.id == ParseJob.target_id)
        .outerjoin(ParserAccount, ParserAccount.id == ParseJob.account_id)
        .where(
            ParseJob.parser_type == parser_type,
            ParseJob.status.in_([JobStatus.failed, JobStatus.retry]),
            ParseJob.last_error.is_not(None),
        )
        .order_by(desc(ParseJob.updated_at), desc(ParseJob.id))
        .limit(clamped_limit)
    )

    if target_id is not None:
        stmt = stmt.where(ParseJob.target_id == int(target_id))

    if not _is_admin(user):
        stmt = stmt.where(ParseJob.owner_user_id == int(user.id))

    rows = db.execute(stmt).all()
    result: list[dict] = []
    for job, target_name, account_label in rows:
        result.append(
            {
                "id": int(job.id),
                "target_id": int(job.target_id),
                "target_name": str(target_name or f"Ціль #{job.target_id}"),
                "account_id": int(job.account_id) if job.account_id is not None else None,
                "account_label": str(account_label or (str(job.account_id) if job.account_id is not None else "-")),
                "status": job.status.value,
                "job_type": _job_kind_from_key(job.job_key),
                "attempt": int(job.attempt or 0),
                "error": str(job.last_error or ""),
                "error_at": job.updated_at.isoformat() if job.updated_at else None,
                "error_at_text": _format_kyiv_datetime(job.updated_at),
            }
        )

    return {"rows": result}


@router.get("/modules/darknet")
def darknet_module_data(db: Session = Depends(get_db), user=Depends(get_current_user)):
    parser_type = ParserType.darknet
    targets_stmt = select(Target).where(Target.parser_type == parser_type)
    accounts_stmt = select(ParserAccount).where(ParserAccount.parser_type == parser_type)
    jobs_stmt = select(ParseJob).where(
        ParseJob.parser_type == parser_type,
        ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry, JobStatus.failed]),
    )
    if not _is_admin(user):
        owner_id = int(user.id)
        targets_stmt = targets_stmt.where(Target.owner_user_id == owner_id)
        accounts_stmt = accounts_stmt.where(ParserAccount.owner_user_id == owner_id)
        jobs_stmt = jobs_stmt.where(ParseJob.owner_user_id == owner_id)

    targets = db.execute(targets_stmt.order_by(desc(Target.created_at))).scalars().all()
    accounts = db.execute(accounts_stmt.order_by(desc(ParserAccount.created_at))).scalars().all()
    jobs = db.execute(jobs_stmt.order_by(desc(ParseJob.created_at)).limit(100)).scalars().all()

    target_rows: list[dict] = []
    target_name_map = {target.id: target.name for target in targets}
    account_name_map = {account.id: account.label for account in accounts}
    now = dt.datetime.now(dt.UTC)
    target_ids = [int(target.id) for target in targets]

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
        events_count = (
            db.scalar(
                select(func.count()).select_from(RawEvent).where(RawEvent.parser_type == parser_type, RawEvent.target_id == target.id)
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
        config = dict(target.config or {})
        try:
            adapter_value = normalize_darknet_adapter(str(config.get("adapter") or "xenforo"))
        except Exception:
            adapter_value = "xenforo"
        if not target.is_active:
            process_text = "Зупинено"
        elif running > 0:
            process_text = f"Виконується ({running})"
        elif queued > 0:
            process_text = f"У черзі ({queued})"
        elif failed > 0:
            process_text = f"Є помилки ({failed})"
        else:
            process_text = "Працює за розкладом" if last_success else "Готово до першого запуску"

        start_urls = config.get("start_urls")
        start_urls_count = len(start_urls) if isinstance(start_urls, list) else 0
        target_rows.append(
            {
                "id": target.id,
                "name": target.name,
                "identifier": target.identifier,
                "config": config,
                "is_active": bool(target.is_active),
                "running": int(running),
                "queued": int(queued),
                "failed": int(failed),
                "events_count": int(events_count),
                "last_success_at": last_success.isoformat() if last_success else None,
                "last_success_text": _format_kyiv_datetime(last_success),
                "process_text": process_text,
                "adapter": adapter_value,
                "login_required": bool(config.get("login_required", False)),
                "start_urls_count": int(start_urls_count),
                "adapter_detected": config.get("adapter_detected"),
                "adapter_suggested": config.get("adapter_suggested"),
                "adapter_detected_error": config.get("adapter_detected_error"),
            }
        )

    account_map = {account.id: account for account in accounts}
    running_count_rows = (
        db.execute(
            select(ParseJob.account_id, func.count(ParseJob.id))
            .where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.running, ParseJob.account_id.is_not(None))
            .group_by(ParseJob.account_id)
        ).all()
    )
    running_counts_by_account = {int(acc_id): int(cnt) for acc_id, cnt in running_count_rows if acc_id is not None}

    account_rows: list[dict] = []
    parallel_limit_by_account: dict[int, int] = {}
    for account in accounts:
        creds = dict(account.credentials or {})
        try:
            account_parallel_limit = int(creds.get("parallel_jobs", settings.darknet_parallel_jobs_per_account))
        except Exception:
            account_parallel_limit = int(settings.darknet_parallel_jobs_per_account)
        account_parallel_limit = max(int(account_parallel_limit), 1)
        parallel_limit_by_account[int(account.id)] = int(account_parallel_limit)

        queued_jobs = (
            db.scalar(
                select(func.count())
                .select_from(ParseJob)
                .where(
                    ParseJob.parser_type == parser_type,
                    ParseJob.account_id == account.id,
                    ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
                )
            )
            or 0
        )
        running_jobs = int(running_counts_by_account.get(int(account.id), 0))
        waiting_jobs = max(int(queued_jobs) - running_jobs, 0)

        # Darknet load reflects worker slots + queue pressure (not Telegram hourly counter).
        slot_ratio = min(float(running_jobs) / float(account_parallel_limit), 1.0)
        queue_ratio = min(float(waiting_jobs) / float(account_parallel_limit), 1.0)
        utilization = int(min(1.0, slot_ratio + (queue_ratio * 0.5)) * 100.0)
        if int(queued_jobs) > 0 and utilization == 0:
            utilization = 5

        auth_mode, auth_state_text, state_cookies_count, state_origins_count = _darknet_account_auth_snapshot(creds)
        account_rows.append(
            {
                "id": account.id,
                "label": account.label,
                "is_active": bool(account.is_active),
                "utilization": int(utilization),
                "queued_jobs": int(queued_jobs),
                "running_jobs": int(running_jobs),
                "waiting_jobs": int(waiting_jobs),
                "parallel_limit": int(account_parallel_limit),
                "last_success_text": _format_kyiv_datetime(account.last_success_at),
                "auth_mode": auth_mode,
                "auth_state_text": auth_state_text,
                "username": str(creds.get("username") or "").strip() or None,
                "proxy_url": str(creds.get("proxy_url") or "").strip() or "default",
                "state_cookies_count": int(state_cookies_count),
                "state_origins_count": int(state_origins_count),
            }
        )

    job_rows: list[dict] = []
    for job in jobs:
        reason = "-"
        if job.status in (JobStatus.pending, JobStatus.retry):
            if job.run_after and job.run_after > now:
                reason = f"Чекає таймер до {_format_kyiv_datetime(job.run_after)}"
            elif job.account_id is not None:
                account = account_map.get(int(job.account_id))
                if account:
                    running_count = int(running_counts_by_account.get(int(job.account_id), 0))
                    account_parallel_limit = int(
                        parallel_limit_by_account.get(
                            int(job.account_id),
                            max(int(settings.darknet_parallel_jobs_per_account), 1),
                        )
                    )
                    if running_count >= account_parallel_limit:
                        reason = f"Чекає вільний слот акаунта ({running_count}/{account_parallel_limit})"
                    else:
                        reason = "Чекає чергу воркера"
                else:
                    reason = "Акаунт не знайдено"
            else:
                reason = "Чекає вільний воркер"
        elif job.status == JobStatus.running:
            reason = "Задача виконується"
        elif job.status == JobStatus.failed and job.last_error:
            reason = "Завершено з помилкою"

        job_rows.append(
            {
                "id": job.id,
                "target_id": job.target_id,
                "target_name": target_name_map.get(job.target_id, f"Ціль #{job.target_id}"),
                "account_id": job.account_id,
                "account_label": account_name_map.get(job.account_id, str(job.account_id) if job.account_id else "-"),
                "status": job.status.value,
                "job_type": _job_kind_from_key(job.job_key),
                "attempt": int(job.attempt),
                "error": job.last_error if job.status in (JobStatus.failed, JobStatus.retry) else None,
                "wait_reason": reason,
            }
        )

    total_events = 0
    message_events = 0
    profile_events = 0
    service_events = 0
    if target_ids:
        raw_counts = db.execute(
            select(
                func.count(RawEvent.id).label("total"),
                func.count(RawEvent.id).filter(RawEvent.external_id.like("%#post:%")).label("message_events"),
                func.count(RawEvent.id).filter(RawEvent.external_id.like("%#user:%")).label("profile_events"),
            ).where(
                RawEvent.parser_type == parser_type,
                RawEvent.target_id.in_(target_ids),
            )
        ).one()
        total_events = int(raw_counts.total or 0)
        message_events = int(raw_counts.message_events or 0)
        profile_events = int(raw_counts.profile_events or 0)
        service_events = max(total_events - message_events - profile_events, 0)

    return {
        "summary": {
            "targets": len(targets),
            "accounts": len(accounts),
            "events_count": int(total_events),
            "message_events_count": int(message_events),
            "profile_events_count": int(profile_events),
            "service_events_count": int(service_events),
            "problem_jobs": int(sum(1 for item in job_rows if item["status"] in {"failed", "retry"})),
        },
        "targets": target_rows,
        "accounts": account_rows,
        "jobs": job_rows,
    }


@router.get("/modules/darknet/events/recent")
def darknet_recent_events(
    limit: int = 100,
    target_id: int | None = None,
    include_service: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_type = ParserType.darknet
    safe_limit = max(1, min(int(limit or 100), 300))

    targets = db.execute(_owned_targets_stmt(user).where(Target.parser_type == parser_type)).scalars().all()
    if not targets:
        return {"rows": []}

    target_map = {
        int(target.id): {
            "name": str(target.name or f"Ціль #{target.id}"),
            "identifier": str(target.identifier or ""),
        }
        for target in targets
    }
    target_ids = set(target_map.keys())

    if target_id is not None:
        target = _ensure_target_access(db.get(Target, int(target_id)), user)
        if target.parser_type != parser_type:
            raise HTTPException(status_code=404, detail="Ціль не знайдена")
        target_ids = {int(target.id)}

    if not target_ids:
        return {"rows": []}

    accounts = db.execute(_owned_accounts_stmt(user).where(ParserAccount.parser_type == parser_type)).scalars().all()
    account_name_map = {int(account.id): str(account.label or f"Акаунт #{account.id}") for account in accounts}

    rows = (
        db.execute(
            (
                select(RawEvent)
                .where(
                    RawEvent.parser_type == parser_type,
                    RawEvent.target_id.in_(target_ids),
                )
                .where(or_(RawEvent.external_id.is_(None), ~RawEvent.external_id.like("discover:%")))
                .where(or_(RawEvent.external_id.is_(None), ~RawEvent.external_id.like("thread_summary:%")))
                if not include_service
                else select(RawEvent).where(
                    RawEvent.parser_type == parser_type,
                    RawEvent.target_id.in_(target_ids),
                )
            )
            .order_by(desc(func.coalesce(RawEvent.observed_at, RawEvent.created_at)), desc(RawEvent.id))
            .limit(safe_limit)
        )
        .scalars()
        .all()
    )

    result: list[dict] = []
    for event in rows:
        payload = event.payload
        event_type = str(payload.get("event_type") or "").strip()
        if not event_type:
            external_id = str(event.external_id or "")
            if "#post:" in external_id:
                event_type = "forum_post"
            elif "#user:" in external_id:
                event_type = "forum_user"
            elif external_id.startswith("thread_summary:"):
                event_type = "thread_summary"
            elif external_id.startswith("discover:"):
                event_type = "darknet_discovery"

        author = str(payload.get("author") or "").strip() or None
        if not author:
            user_payload = payload.get("user")
            if isinstance(user_payload, dict):
                author = str(user_payload.get("username") or user_payload.get("display_name") or "").strip() or None

        preview_text = str(payload.get("text") or "").strip()
        if not preview_text:
            if event_type == "thread_summary":
                preview_text = (
                    f"posts={int(payload.get('posts_count') or 0)}, "
                    f"new_posts={int(payload.get('new_posts_count') or 0)}, "
                    f"users={int(payload.get('users_count') or 0)}"
                )
            elif event_type == "darknet_discovery":
                preview_text = (
                    f"threads={int(payload.get('thread_urls_found') or 0)}, "
                    f"selected={int(payload.get('thread_urls_selected') or 0)}, "
                    f"jobs={int(payload.get('jobs_created') or 0)}"
                )
            elif event_type == "forum_user":
                user_payload = payload.get("user")
                if isinstance(user_payload, dict):
                    profile_url = str(user_payload.get("profile_url") or "").strip()
                    preview_text = profile_url or "Оновлено профіль користувача"
                else:
                    preview_text = "Оновлено профіль користувача"
            else:
                preview_text = "-"
        if len(preview_text) > 240:
            preview_text = f"{preview_text[:240]}..."

        thread_url = str(payload.get("thread_url") or "").strip() or None
        thread_title = str(payload.get("thread_title") or "").strip() or None
        observed_at = _coerce_utc(event.observed_at) or _coerce_utc(event.created_at)
        target_meta = target_map.get(int(event.target_id), {})

        result.append(
            {
                "id": int(event.id),
                "target_id": int(event.target_id),
                "target_name": str(target_meta.get("name") or f"Ціль #{event.target_id}"),
                "target_identifier": str(target_meta.get("identifier") or ""),
                "account_id": int(event.account_id) if event.account_id is not None else None,
                "account_label": account_name_map.get(
                    int(event.account_id),
                    str(event.account_id) if event.account_id is not None else "-",
                ),
                "event_type": event_type or "-",
                "event_type_text": _darknet_event_type_text(event_type),
                "author": author,
                "thread_url": thread_url,
                "thread_title": thread_title,
                "preview_text": preview_text,
                "external_id": event.external_id,
                "observed_at": observed_at.isoformat() if observed_at else None,
                "observed_at_text": _format_kyiv_datetime(observed_at),
            }
        )

    return {"rows": result}


@router.post("/links")
def create_link(payload: LinkAccountRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = _ensure_target_access(db.get(Target, payload.target_id), user)
    account = _ensure_account_access(db.get(ParserAccount, payload.account_id), user)
    if target.parser_type != account.parser_type:
        raise HTTPException(status_code=400, detail="parser types mismatch")
    if not _is_admin(user) and (target.owner_user_id != user.id or account.owner_user_id != user.id):
        raise HTTPException(status_code=403, detail="Немає доступу до привʼязки цих ресурсів")

    existing = db.execute(
        select(TargetAccountLink).where(
            TargetAccountLink.target_id == payload.target_id,
            TargetAccountLink.account_id == payload.account_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.is_active = True
        if existing.owner_user_id is None:
            existing.owner_user_id = target.owner_user_id or account.owner_user_id
    else:
        db.add(
            TargetAccountLink(
                target_id=payload.target_id,
                account_id=payload.account_id,
                owner_user_id=target.owner_user_id or account.owner_user_id,
                is_active=True,
            )
        )
    db.commit()
    return {"ok": True}


@router.post("/scheduler/run")
def run_scheduler(db: Session = Depends(get_db), user=Depends(get_current_user)):
    result = schedule_once(db, None if _is_admin(user) else int(user.id))
    db.commit()
    return result


@router.post("/telegram/sync-memberships")
def sync_memberships(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Синхронізація memberships доступна адміністратору")
    result = sync_telegram_memberships(db)
    db.commit()
    return result


@router.post("/modules/{parser_name}/targets/{target_id}/run-now")
def run_target_now(parser_name: str, target_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        parser_type = ParserType(parser_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Модуль не знайдено") from exc
    target = db.get(Target, target_id)
    if not target or target.parser_type != parser_type:
        raise HTTPException(status_code=404, detail="Ціль не знайдена")
    target = _ensure_target_access(target, user)
    target.is_active = True
    schedule_target_once(db, target)
    db.commit()
    return {"ok": True}


@router.post("/modules/{parser_name}/targets/{target_id}/stop")
def stop_target(parser_name: str, target_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        parser_type = ParserType(parser_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Модуль не знайдено") from exc
    target = db.get(Target, target_id)
    if not target or target.parser_type != parser_type:
        raise HTTPException(status_code=404, detail="Ціль не знайдена")
    target = _ensure_target_access(target, user)
    target.is_active = False
    now = dt.datetime.now(dt.UTC)
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
    for job in pending_jobs:
        job.status = JobStatus.failed
        job.finished_at = now
        job.last_error = "Зупинено користувачем"
        job.locked_by = None
        job.lock_expires_at = None
    db.commit()
    return {"ok": True}


@router.post("/modules/{parser_name}/targets/{target_id}/start")
def start_target(parser_name: str, target_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        parser_type = ParserType(parser_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Модуль не знайдено") from exc
    target = db.get(Target, target_id)
    if not target or target.parser_type != parser_type:
        raise HTTPException(status_code=404, detail="Ціль не знайдена")
    target = _ensure_target_access(target, user)
    target.is_active = True
    db.commit()
    return {"ok": True}


@router.post("/modules/{parser_name}/jobs/retry-failed")
def retry_failed_jobs(parser_name: str, payload: dict | None = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        parser_type = ParserType(parser_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Модуль не знайдено") from exc

    now = dt.datetime.now(dt.UTC)
    target_id = None
    if isinstance(payload, dict) and payload.get("target_id") is not None:
        target_id = int(payload.get("target_id"))

    stmt = select(ParseJob).where(ParseJob.parser_type == parser_type, ParseJob.status == JobStatus.failed)
    if target_id is not None:
        stmt = stmt.where(ParseJob.target_id == target_id)
    if not _is_admin(user):
        stmt = stmt.where(ParseJob.owner_user_id == int(user.id))

    failed_jobs = db.execute(stmt).scalars().all()
    for job in failed_jobs:
        job.status = JobStatus.retry
        job.attempt = 0
        job.run_after = now
        job.last_error = None
        job.locked_by = None
        job.lock_expires_at = None
        job.finished_at = None
    db.commit()
    return {"ok": True, "updated": len(failed_jobs)}


@router.post("/modules/{parser_name}/jobs/{job_id}/retry")
def retry_single_job(parser_name: str, job_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        parser_type = ParserType(parser_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Модуль не знайдено") from exc

    job = db.get(ParseJob, job_id)
    if not job or job.parser_type != parser_type:
        raise HTTPException(status_code=404, detail="Задачу не знайдено")
    if not _is_admin(user):
        has_access = int(job.owner_user_id or 0) == int(user.id)
        if not has_access:
            target = db.get(Target, int(job.target_id))
            has_access = bool(target and int(target.owner_user_id or 0) == int(user.id))
        if not has_access:
            raise HTTPException(status_code=403, detail="Немає доступу до цієї задачі")
    if job.status == JobStatus.running:
        raise HTTPException(status_code=400, detail="Задача вже виконується")

    now = dt.datetime.now(dt.UTC)
    job.status = JobStatus.retry
    job.attempt = 0
    job.run_after = now
    job.last_error = None
    job.locked_by = None
    job.lock_expires_at = None
    job.finished_at = None
    db.commit()
    return {"ok": True, "job_id": int(job.id), "status": job.status.value}


@router.post("/modules/telegram/accounts/reset-cooldown")
def reset_telegram_accounts_cooldown(db: Session = Depends(get_db), user=Depends(get_current_user)):
    stmt = select(ParserAccount).where(ParserAccount.parser_type == ParserType.telegram, ParserAccount.is_active.is_(True))
    if not _is_admin(user):
        stmt = stmt.where(ParserAccount.owner_user_id == int(user.id))
    accounts = db.execute(stmt).scalars().all()
    for account in accounts:
        account.cooldown_until = None
        account.fail_count = 0
        account.last_error = None
        account.health_score = max(float(account.health_score or 0.0), 80.0)
    db.commit()
    return {"ok": True, "updated": len(accounts)}


@router.post("/modules/telegram/accounts/refresh-status")
def refresh_telegram_accounts_status(db: Session = Depends(get_db), user=Depends(get_current_user)):
    stmt = select(ParserAccount).where(ParserAccount.parser_type == ParserType.telegram)
    if not _is_admin(user):
        stmt = stmt.where(ParserAccount.owner_user_id == int(user.id))
    accounts = db.execute(stmt).scalars().all()

    now = dt.datetime.now(dt.UTC)
    updated = 0
    for account in accounts:
        creds = dict(account.credentials or {})
        status = refresh_account_session_info_sync(creds)
        status["checked_at"] = now.isoformat()
        creds["session_status"] = status
        if status.get("phone"):
            creds["phone"] = status.get("phone")
        if status.get("username"):
            creds["username"] = status.get("username")
        account.credentials = creds
        db.add(account)
        updated += 1
    db.commit()
    return {"ok": True, "updated": int(updated)}


@router.post("/modules/telegram/targets/enable-comments-defaults")
def enable_comments_defaults_for_telegram_targets(db: Session = Depends(get_db), user=Depends(get_current_user)):
    targets = (
        db.execute(_owned_targets_stmt(user).where(Target.parser_type == ParserType.telegram))
        .scalars()
        .all()
    )
    updated = 0
    for target in targets:
        cfg = dict(target.config or {})
        backfill_cfg = dict(cfg.get("backfill") or {})
        target.config = _telegram_target_config(
            limit=int(cfg.get("limit", 200)),
            poll_interval_seconds=int(cfg.get("poll_interval_seconds", 300)),
            live_enabled=bool(cfg.get("live_enabled", True)),
            gapfill_limit=int(cfg.get("gapfill_limit", cfg.get("limit", 200))),
            participants_sync_enabled=bool(cfg.get("participants_sync_enabled", True)),
            participants_sync_interval_seconds=int(cfg.get("participants_sync_interval_seconds", 3600)),
            participants_limit=int(cfg.get("participants_limit", 1000)),
            backfill_enabled=bool(backfill_cfg.get("enabled", False)),
            backfill_mode=str(backfill_cfg.get("mode", "range")),
            backfill_from=str(backfill_cfg.get("from") or ""),
            backfill_to=str(backfill_cfg.get("to") or ""),
            backfill_chunk_days=int(backfill_cfg.get("chunk_days", 7)),
            backfill_limit=int(cfg.get("backfill_limit", 5000)),
            backfill_full_batch_size=int(cfg.get("backfill_full_batch_size", 300)),
            comments_enabled=True,
            gapfill_comments_enabled=True,
            backfill_comments_enabled=True,
            comments_limit=int(cfg.get("comments_limit", 20)),
            comments_depth=int(cfg.get("comments_depth", 2)),
            comments_recheck_posts=int(cfg.get("comments_recheck_posts", 30)),
        )
        updated += 1
    db.commit()
    return {"ok": True, "updated": int(updated)}


@router.post("/modules/telegram/accounts/{account_id}/update-label")
def update_telegram_account_label(
    account_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = _ensure_account_access(db.get(ParserAccount, account_id), user)
    if account.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram акаунт не знайдено")

    new_label = str(payload.get("label") or "").strip()
    if not new_label:
        raise HTTPException(status_code=400, detail="Мітка акаунта не може бути порожньою")

    duplicate = db.execute(select(ParserAccount).where(ParserAccount.label == new_label, ParserAccount.id != account.id)).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=400, detail="Мітка вже зайнята")

    account.label = new_label
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"ok": True, "account_id": int(account.id), "label": account.label}


@router.post("/modules/telegram/targets/{target_id}/update")
def update_telegram_target(target_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = _ensure_target_access(db.get(Target, target_id), user)
    if target.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram ціль не знайдена")

    current_cfg = dict(target.config or {})
    current_backfill = dict(current_cfg.get("backfill") or {})
    from_value = str(payload.get("backfill_from") or current_backfill.get("from") or "")
    to_value = str(payload.get("backfill_to") or current_backfill.get("to") or "")

    normalized_from = _normalize_backfill_datetime_input(from_value) if from_value.strip() else ""
    normalized_to = _normalize_backfill_datetime_input(to_value) if to_value.strip() else ""

    target.name = str(payload.get("name") or target.name).strip() or target.name
    target.identifier = normalize_telegram_identifier(str(payload.get("identifier") or target.identifier)) or target.identifier
    target.config = _telegram_target_config(
        limit=int(payload.get("limit") or current_cfg.get("limit", 200)),
        poll_interval_seconds=int(payload.get("poll_interval_seconds") or current_cfg.get("poll_interval_seconds", 300)),
        live_enabled=bool(payload.get("live_enabled", current_cfg.get("live_enabled", True))),
        gapfill_limit=int(payload.get("gapfill_limit") or current_cfg.get("gapfill_limit", current_cfg.get("limit", 200))),
        participants_sync_enabled=bool(payload.get("participants_sync_enabled", current_cfg.get("participants_sync_enabled", True))),
        participants_sync_interval_seconds=int(
            payload.get("participants_sync_interval_seconds") or current_cfg.get("participants_sync_interval_seconds", 3600)
        ),
        participants_limit=int(payload.get("participants_limit") or current_cfg.get("participants_limit", 1000)),
        backfill_enabled=bool(payload.get("backfill_enabled", current_backfill.get("enabled", False))),
        backfill_mode=str(payload.get("backfill_mode") or current_backfill.get("mode", "range")),
        backfill_from=normalized_from,
        backfill_to=normalized_to,
        backfill_chunk_days=int(payload.get("backfill_chunk_days") or current_backfill.get("chunk_days", 7)),
        backfill_limit=int(payload.get("backfill_limit") or current_cfg.get("backfill_limit", 5000)),
        backfill_full_batch_size=int(payload.get("backfill_full_batch_size") or current_cfg.get("backfill_full_batch_size", 300)),
        comments_enabled=bool(payload.get("comments_enabled", current_cfg.get("comments_enabled", True))),
        gapfill_comments_enabled=bool(payload.get("gapfill_comments_enabled", current_cfg.get("gapfill_comments_enabled", True))),
        backfill_comments_enabled=bool(payload.get("backfill_comments_enabled", current_cfg.get("backfill_comments_enabled", True))),
        comments_limit=int(payload.get("comments_limit") or current_cfg.get("comments_limit", 20)),
        comments_depth=int(payload.get("comments_depth") or current_cfg.get("comments_depth", 2)),
        comments_recheck_posts=int(payload.get("comments_recheck_posts") or current_cfg.get("comments_recheck_posts", 30)),
        is_risky=bool(payload.get("is_risky", current_cfg.get("is_risky", False))),
        risk_label=str(payload.get("risk_label", current_cfg.get("risk_label", ""))).strip(),
    )
    db.commit()
    return {"ok": True, "target_id": int(target.id)}


@router.get("/modules/darknet/adapters")
def darknet_adapters_list():
    return {"adapters": list_darknet_adapters()}


@router.post("/modules/darknet/accounts/{account_id}/auth-helper/start")
def start_darknet_auth_helper(
    account_id: int,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = _ensure_account_access(db.get(ParserAccount, account_id), user)
    if account.parser_type != ParserType.darknet:
        raise HTTPException(status_code=404, detail="Darknet акаунт не знайдено")

    body = payload if isinstance(payload, dict) else {}
    expires_minutes = max(1, min(int(body.get("expires_minutes") or 15), 180))
    forum_url = str(body.get("forum_url") or "").strip()
    if not forum_url:
        target_row = db.execute(
            select(Target.identifier)
            .join(TargetAccountLink, TargetAccountLink.target_id == Target.id)
            .where(
                TargetAccountLink.account_id == int(account.id),
                TargetAccountLink.is_active.is_(True),
                Target.parser_type == ParserType.darknet,
            )
            .order_by(TargetAccountLink.id.asc())
            .limit(1)
        ).first()
        if target_row and target_row[0]:
            forum_url = str(target_row[0]).strip()
    if not forum_url:
        forum_url = "https://example.org/"

    api_base = str(body.get("api_base") or "").strip().rstrip("/")
    if not api_base:
        api_base = str(request.base_url).rstrip("/")

    now = dt.datetime.now(dt.UTC)
    old_rows = (
        db.execute(
            select(DarknetAuthToken).where(
                DarknetAuthToken.account_id == int(account.id),
                or_(
                    DarknetAuthToken.used_at.is_not(None),
                    DarknetAuthToken.expires_at < now,
                ),
            )
        )
        .scalars()
        .all()
    )
    for row in old_rows:
        db.delete(row)

    raw_token = f"dna_{secrets.token_urlsafe(28)}"
    token = DarknetAuthToken(
        account_id=int(account.id),
        owner_user_id=int(user.id),
        token_hash=hash_token(raw_token),
        expires_at=now + dt.timedelta(minutes=expires_minutes),
        created_from_ip=_request_client_ip(request),
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    helper_cmd = (
        "python3 scripts/darknet_auth_helper.py "
        f"--api-base {shlex.quote(api_base)} "
        f"--auth-token {shlex.quote(raw_token)} "
        f"--forum-url {shlex.quote(forum_url)}"
    )

    return {
        "ok": True,
        "account_id": int(account.id),
        "forum_url": forum_url,
        "expires_at": token.expires_at.isoformat(),
        "expires_at_text": _format_kyiv_datetime(token.expires_at),
        "auth_token": raw_token,
        "helper_command": helper_cmd,
    }


@router.post("/modules/darknet/auth-helper/complete")
def complete_darknet_auth_helper(payload: dict, request: Request, db: Session = Depends(get_db)):
    auth_token = str(payload.get("auth_token") or "").strip()
    if not auth_token:
        raise HTTPException(status_code=400, detail="auth_token обов'язковий")

    now = dt.datetime.now(dt.UTC)
    row = db.execute(
        select(DarknetAuthToken).where(
            DarknetAuthToken.token_hash == hash_token(auth_token),
            DarknetAuthToken.used_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=400, detail="Токен helper недійсний або вже використаний")
    if row.expires_at < now:
        raise HTTPException(status_code=400, detail="Токен helper прострочено")

    account = db.get(ParserAccount, int(row.account_id))
    if not account or account.parser_type != ParserType.darknet:
        raise HTTPException(status_code=400, detail="Darknet акаунт для токена не знайдено")

    storage_state_input = payload.get("storage_state")
    if storage_state_input is None:
        storage_state_input = payload.get("storage_state_json")
    try:
        storage_state = parse_storage_state(storage_state_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = summarize_storage_state(storage_state)
    if int(summary["cookies_count"]) <= 0:
        raise HTTPException(status_code=400, detail="Порожній storageState: cookies не знайдено")

    creds = dict(account.credentials or {})
    creds["auth_mode"] = "browser_state"
    creds["storage_state"] = storage_state
    creds["storage_state_updated_at"] = now.isoformat()
    if "proxy_url" in payload:
        creds["proxy_url"] = str(payload.get("proxy_url") or "").strip() or "default"
    if "browser_user_agent" in payload:
        ua = str(payload.get("browser_user_agent") or "").strip()
        if ua:
            creds["browser_user_agent"] = ua
        else:
            creds.pop("browser_user_agent", None)
    if "fallback_form_login" in payload:
        creds["fallback_form_login"] = bool(payload.get("fallback_form_login"))

    account.credentials = creds
    row.used_at = now
    row.used_from_ip = _request_client_ip(request)
    db.add(account)
    db.add(row)
    db.commit()
    return {
        "ok": True,
        "account_id": int(account.id),
        "cookies_count": int(summary["cookies_count"]),
        "origins_count": int(summary["origins_count"]),
        "used_at": now.isoformat(),
    }


@router.post("/modules/darknet/accounts")
def create_darknet_account(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    label = str(payload.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Мітка акаунта обов'язкова")
    duplicate = db.execute(select(ParserAccount).where(ParserAccount.label == label)).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=400, detail="Акаунт з такою міткою вже існує")

    hourly_limit = max(int(payload.get("hourly_limit") or 120), 1)
    auth_mode = _darknet_auth_mode_text(str(payload.get("auth_mode") or ""))
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    login_page_path = str(payload.get("login_page_path") or "/login/").strip() or "/login/"
    login_submit_path = str(payload.get("login_submit_path") or "/login/login").strip() or "/login/login"
    proxy_url = str(payload.get("proxy_url") or "").strip() or "default"
    browser_user_agent = str(payload.get("browser_user_agent") or "").strip()
    fallback_form_login = bool(payload.get("fallback_form_login", False))

    storage_state_input = payload.get("storage_state")
    if storage_state_input is None:
        storage_state_input = payload.get("storage_state_json")
    try:
        storage_state = parse_storage_state(storage_state_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    storage_summary = summarize_storage_state(storage_state)

    credentials: dict = {
        "auth_mode": auth_mode,
        "username": username,
        "password": password,
        "login_page_path": login_page_path,
        "login_submit_path": login_submit_path,
        "proxy_url": proxy_url,
        "fallback_form_login": fallback_form_login,
    }
    if browser_user_agent:
        credentials["browser_user_agent"] = browser_user_agent
    if int(storage_summary["cookies_count"]) > 0 or int(storage_summary["origins_count"]) > 0:
        credentials["storage_state"] = storage_state
        credentials["storage_state_updated_at"] = dt.datetime.now(dt.UTC).isoformat()

    account = ParserAccount(
        parser_type=ParserType.darknet,
        label=label,
        owner_user_id=int(user.id),
        credentials=credentials,
        hourly_limit=hourly_limit,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Акаунт з такою міткою вже існує") from exc
    db.refresh(account)
    return {
        "id": int(account.id),
        "auth_mode": auth_mode,
        "cookies_count": int(storage_summary["cookies_count"]),
        "origins_count": int(storage_summary["origins_count"]),
    }


@router.post("/modules/darknet/accounts/{account_id}/browser-state")
def update_darknet_account_browser_state(
    account_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = _ensure_account_access(db.get(ParserAccount, account_id), user)
    if account.parser_type != ParserType.darknet:
        raise HTTPException(status_code=404, detail="Darknet акаунт не знайдено")

    storage_state_input = payload.get("storage_state")
    if storage_state_input is None:
        storage_state_input = payload.get("storage_state_json")
    try:
        storage_state = parse_storage_state(storage_state_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    summary = summarize_storage_state(storage_state)
    if int(summary["cookies_count"]) <= 0:
        raise HTTPException(status_code=400, detail="Порожній storageState: cookies не знайдено")

    creds = dict(account.credentials or {})
    creds["auth_mode"] = "browser_state"
    creds["storage_state"] = storage_state
    creds["storage_state_updated_at"] = dt.datetime.now(dt.UTC).isoformat()

    if "proxy_url" in payload:
        creds["proxy_url"] = str(payload.get("proxy_url") or "").strip() or "default"
    if "browser_user_agent" in payload:
        ua = str(payload.get("browser_user_agent") or "").strip()
        if ua:
            creds["browser_user_agent"] = ua
        elif "browser_user_agent" in creds:
            creds.pop("browser_user_agent", None)
    if "fallback_form_login" in payload:
        creds["fallback_form_login"] = bool(payload.get("fallback_form_login"))

    account.credentials = creds
    db.add(account)
    db.commit()
    return {
        "ok": True,
        "account_id": int(account.id),
        "cookies_count": int(summary["cookies_count"]),
        "origins_count": int(summary["origins_count"]),
    }


@router.post("/modules/darknet/targets/{target_id}/update")
def update_darknet_target(target_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = _ensure_target_access(db.get(Target, target_id), user)
    if target.parser_type != ParserType.darknet:
        raise HTTPException(status_code=404, detail="Darknet ціль не знайдена")

    current_cfg = dict(target.config or {})

    def _to_non_negative_int(raw_value: object, fallback: int) -> int:
        if raw_value is None:
            return max(int(fallback), 0)
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Невірне числове значення: {raw_value}") from exc
        return max(parsed, 0)

    def _to_min_int(raw_value: object, fallback: int, minimum: int) -> int:
        if raw_value is None:
            return max(int(fallback), minimum)
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Невірне числове значення: {raw_value}") from exc
        return max(parsed, minimum)

    name = str(payload.get("name") or target.name).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Назва цілі не може бути порожньою")
    identifier = str(payload.get("identifier") or target.identifier).strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="URL / ідентифікатор цілі не може бути порожнім")

    adapter_raw = str(payload.get("adapter") or current_cfg.get("adapter") or "xenforo").strip()
    try:
        adapter_value = normalize_darknet_adapter(adapter_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    start_urls_payload = payload.get("start_urls")
    if isinstance(start_urls_payload, list):
        start_urls = [str(item).strip() for item in start_urls_payload if str(item).strip()]
    elif isinstance(start_urls_payload, str):
        start_urls = [line.strip() for line in start_urls_payload.splitlines() if line.strip()]
    else:
        cfg_start_urls = current_cfg.get("start_urls")
        start_urls = [str(item).strip() for item in cfg_start_urls] if isinstance(cfg_start_urls, list) else []

    if not start_urls:
        start_urls = [identifier]

    collect_maximum_raw = payload.get("collect_maximum")
    if collect_maximum_raw is None:
        collect_maximum = bool(current_cfg.get("collect_maximum", True))
    else:
        collect_maximum = bool(collect_maximum_raw)

    reparse_existing_threads_raw = payload.get("reparse_existing_threads")
    if reparse_existing_threads_raw is None:
        reparse_existing_threads = bool(current_cfg.get("reparse_existing_threads", True))
    else:
        reparse_existing_threads = bool(reparse_existing_threads_raw)

    login_required_raw = payload.get("login_required")
    if login_required_raw is None:
        login_required = bool(current_cfg.get("login_required", False))
    else:
        login_required = bool(login_required_raw)

    config = dict(current_cfg)
    config["adapter"] = adapter_value
    config["start_urls"] = start_urls
    config["login_required"] = login_required
    config["collect_maximum"] = collect_maximum
    config["reparse_existing_threads"] = reparse_existing_threads
    config["thread_reparse_interval_minutes"] = _to_min_int(
        payload.get("thread_reparse_interval_minutes"),
        int(current_cfg.get("thread_reparse_interval_minutes", 15)),
        1,
    )
    config["max_threads_per_cycle"] = _to_non_negative_int(
        payload.get("max_threads_per_cycle"),
        int(current_cfg.get("max_threads_per_cycle", 0)),
    )
    config["max_pages_per_thread"] = _to_non_negative_int(
        payload.get("max_pages_per_thread"),
        int(current_cfg.get("max_pages_per_thread", 0)),
    )
    config["max_posts_per_thread"] = _to_non_negative_int(
        payload.get("max_posts_per_thread"),
        int(current_cfg.get("max_posts_per_thread", 0)),
    )
    config["max_discover_pages_per_start"] = _to_non_negative_int(
        payload.get("max_discover_pages_per_start"),
        int(current_cfg.get("max_discover_pages_per_start", 0)),
    )
    config["discover_interval_seconds"] = _to_min_int(
        payload.get("discover_interval_seconds"),
        int(current_cfg.get("discover_interval_seconds", 60)),
        10,
    )

    marker_raw = str(payload.get("thread_url_contains") or config.get("thread_url_contains") or "").strip()
    if not marker_raw or marker_raw in {"/threads/", "/viewtopic.php"}:
        marker_raw = "/viewtopic.php" if adapter_value == "phpbb_like" else "/threads/"
    config["thread_url_contains"] = marker_raw

    target.name = name
    target.identifier = identifier
    target.config = config
    db.commit()
    return {"ok": True, "target_id": int(target.id)}


@router.post("/modules/darknet/targets/{target_id}/adapter")
def update_darknet_target_adapter(target_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = _ensure_target_access(db.get(Target, target_id), user)
    if target.parser_type != ParserType.darknet:
        raise HTTPException(status_code=404, detail="Darknet ціль не знайдена")

    adapter_raw = str(payload.get("adapter") or "").strip()
    if not adapter_raw:
        raise HTTPException(status_code=400, detail="adapter обов'язковий")
    try:
        normalized = normalize_darknet_adapter(adapter_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    config = dict(target.config or {})
    config["adapter"] = normalized
    marker = str(config.get("thread_url_contains") or "").strip()
    if not marker or marker in {"/threads/", "/viewtopic.php"}:
        config["thread_url_contains"] = "/viewtopic.php" if normalized == "phpbb_like" else "/threads/"
    target.config = config
    db.commit()
    return {"ok": True, "target_id": int(target.id), "adapter": normalized}


@router.post("/modules/darknet/targets/{target_id}/detect-adapter")
def detect_darknet_target_adapter(target_id: int, payload: dict | None = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = _ensure_target_access(db.get(Target, target_id), user)
    if target.parser_type != ParserType.darknet:
        raise HTTPException(status_code=404, detail="Darknet ціль не знайдена")

    max_urls = 3
    if isinstance(payload, dict) and payload.get("max_urls") is not None:
        max_urls = max(int(payload.get("max_urls")), 1)

    config = dict(target.config or {})
    configured_start_urls = config.get("start_urls")
    start_urls: list[str] = []
    if isinstance(configured_start_urls, list):
        start_urls = [str(item).strip() for item in configured_start_urls if str(item).strip()]
    if not start_urls and target.identifier.strip():
        start_urls = [target.identifier.strip()]

    result = asyncio.run(detect_adapter(start_urls, max_urls=max_urls))
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
    return {"ok": True, "target_id": int(target.id), "result": result_payload}


@router.post("/modules/telegram/accounts/start-auth")
def telegram_start_account_auth(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    api_id = str(payload.get("api_id") or "").strip()
    api_hash = str(payload.get("api_hash") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    if not api_id or not api_hash or not phone:
        raise HTTPException(status_code=400, detail="Поля api_id, api_hash і phone обов'язкові")

    try:
        int(api_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="api_id має бути числом") from exc

    try:
        temp_session_string, phone_code_hash = asyncio.run(_telegram_send_code(api_id=api_id, api_hash=api_hash, phone=phone))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не вдалося надіслати код: {exc}") from exc

    if not phone_code_hash:
        raise HTTPException(status_code=400, detail="Не вдалося отримати phone_code_hash")

    return {
        "ok": True,
        "auth_payload": {
            "api_id": api_id,
            "api_hash": api_hash,
            "phone": phone,
            "temp_session_string": temp_session_string,
            "phone_code_hash": phone_code_hash,
        },
    }


@router.post("/modules/telegram/accounts/complete-auth")
def telegram_complete_account_auth(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    label = str(payload.get("label") or "").strip()
    hourly_limit = int(payload.get("hourly_limit") or 120)
    api_id = str(payload.get("api_id") or "").strip()
    api_hash = str(payload.get("api_hash") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    temp_session_string = str(payload.get("temp_session_string") or "").strip()
    phone_code_hash = str(payload.get("phone_code_hash") or "").strip()
    code = str(payload.get("code") or "").strip()
    password = str(payload.get("password") or "").strip()

    if not label:
        raise HTTPException(status_code=400, detail="Мітка акаунта обов'язкова")
    if not api_id or not api_hash or not phone:
        raise HTTPException(status_code=400, detail="Немає даних авторизації. Почніть авторизацію заново.")
    if not temp_session_string or not phone_code_hash:
        raise HTTPException(status_code=400, detail="Немає тимчасової Telegram-сесії. Почніть авторизацію заново.")
    if not code:
        raise HTTPException(status_code=400, detail="Код підтвердження обов'язковий")

    try:
        int(api_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="api_id має бути числом") from exc

    existing_label = db.execute(select(ParserAccount).where(ParserAccount.label == label)).scalar_one_or_none()
    if existing_label:
        raise HTTPException(status_code=400, detail="Акаунт з такою міткою вже існує")

    try:
        session_string = asyncio.run(
            _telegram_verify_code(
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                temp_session_string=temp_session_string,
                phone_code_hash=phone_code_hash,
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
        label=label,
        owner_user_id=int(user.id),
        credentials={
            "api_id": api_id,
            "api_hash": api_hash,
            "phone": phone,
            "session_string": session_string,
            "session_status": {
                "alive": True,
                "is_authorized": True,
                "phone": phone,
                "checked_at": dt.datetime.now(dt.UTC).isoformat(),
            },
        },
        hourly_limit=max(int(hourly_limit), 1),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"ok": True, "account_id": int(account.id), "label": account.label}


@router.get("/modules/telegram/accounts/{account_id}/dialogs")
def telegram_account_dialogs(
    account_id: int,
    refresh: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = _ensure_account_access(db.get(ParserAccount, account_id), user)
    if account.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram акаунт не знайдено")

    creds = dict(account.credentials or {})
    dialogs = creds.get("dialogs_cache")
    if not isinstance(dialogs, list):
        dialogs = []

    if refresh or not dialogs:
        try:
            dialogs = refresh_account_dialogs_sync(creds, limit=700)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Не вдалося оновити список чатів: {exc}") from exc
        creds["dialogs_cache"] = dialogs
        creds["dialogs_cached_at"] = dt.datetime.now(dt.UTC).isoformat()
        account.credentials = creds
        db.add(account)
        db.commit()

    links_stmt = (
        select(TargetAccountLink, Target)
        .join(Target, Target.id == TargetAccountLink.target_id)
        .where(
            TargetAccountLink.account_id == account.id,
            Target.parser_type == ParserType.telegram,
        )
    )
    if not _is_admin(user):
        links_stmt = links_stmt.where(Target.owner_user_id == int(user.id))
    link_rows = db.execute(links_stmt).all()
    target_by_identifier: dict[str, Target] = {}
    for _, target in link_rows:
        normalized = normalize_telegram_identifier(str(target.identifier or ""))
        if normalized:
            target_by_identifier[normalized] = target

    dialogs_view: list[dict] = []
    for item in dialogs:
        if not isinstance(item, dict):
            continue
        identifier = normalize_telegram_identifier(str(item.get("identifier") or ""))
        linked_target = target_by_identifier.get(identifier)
        dialogs_view.append(
            {
                "dialog_id": item.get("dialog_id"),
                "title": str(item.get("title") or identifier or "-"),
                "identifier": identifier or str(item.get("identifier") or ""),
                "username": item.get("username"),
                "kind": str(item.get("kind") or "чат"),
                "is_linked": linked_target is not None,
                "target_id": int(linked_target.id) if linked_target else None,
                "target_name": str(linked_target.name) if linked_target else None,
            }
        )

    return {
        "account": {"id": account.id, "label": account.label},
        "cached_at": creds.get("dialogs_cached_at"),
        "dialogs": dialogs_view,
    }


@router.post("/modules/telegram/accounts/{account_id}/dialogs/add-target")
def telegram_account_add_target_from_dialog(
    account_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = _ensure_account_access(db.get(ParserAccount, account_id), user)
    if account.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram акаунт не знайдено")

    identifier_raw = str(payload.get("identifier") or "")
    title_raw = str(payload.get("title") or "")
    run_now = bool(payload.get("run_now", True))
    identifier = normalize_telegram_identifier(identifier_raw)
    if not identifier:
        raise HTTPException(status_code=400, detail="Порожній ідентифікатор")

    target_stmt = select(Target).where(Target.parser_type == ParserType.telegram, Target.identifier == identifier)
    if not _is_admin(user):
        target_stmt = target_stmt.where(Target.owner_user_id == int(user.id))
    target = db.execute(target_stmt).scalar_one_or_none()

    owner_user_id = account.owner_user_id or int(user.id)
    created = False
    if not target:
        default_cfg = _default_telegram_target_config()
        target = Target(
            parser_type=ParserType.telegram,
            name=title_raw.strip() or identifier,
            identifier=identifier,
            owner_user_id=owner_user_id,
            config=_telegram_target_config(
                limit=int(default_cfg.get("limit", 200)),
                poll_interval_seconds=int(default_cfg.get("poll_interval_seconds", 300)),
                live_enabled=bool(default_cfg.get("live_enabled", True)),
                gapfill_limit=int(default_cfg.get("gapfill_limit", default_cfg.get("limit", 200))),
                participants_sync_enabled=bool(default_cfg.get("participants_sync_enabled", True)),
                participants_sync_interval_seconds=int(default_cfg.get("participants_sync_interval_seconds", 3600)),
                participants_limit=int(default_cfg.get("participants_limit", 1000)),
                backfill_enabled=True,
                backfill_mode="full",
                backfill_from="",
                backfill_to="",
                backfill_chunk_days=int((default_cfg.get("backfill") or {}).get("chunk_days", 7)),
                backfill_limit=int(default_cfg.get("backfill_limit", 5000)),
                backfill_full_batch_size=int(default_cfg.get("backfill_full_batch_size", 300)),
                comments_enabled=bool(default_cfg.get("comments_enabled", True)),
                gapfill_comments_enabled=bool(default_cfg.get("gapfill_comments_enabled", True)),
                backfill_comments_enabled=bool(default_cfg.get("backfill_comments_enabled", True)),
                comments_limit=int(default_cfg.get("comments_limit", 20)),
                comments_depth=int(default_cfg.get("comments_depth", 2)),
                comments_recheck_posts=int(default_cfg.get("comments_recheck_posts", 30)),
            ),
            is_active=True,
        )
        db.add(target)
        db.flush()
        created = True
    else:
        target = _ensure_target_access(target, user)

    link = db.execute(
        select(TargetAccountLink).where(
            TargetAccountLink.target_id == target.id,
            TargetAccountLink.account_id == account.id,
        )
    ).scalar_one_or_none()
    linked_now = False
    if not link:
        link = TargetAccountLink(
            target_id=target.id,
            account_id=account.id,
            owner_user_id=owner_user_id,
            is_active=True,
            auto_detected=False,
            last_checked_at=dt.datetime.now(dt.UTC),
        )
        db.add(link)
        linked_now = True
    else:
        link.is_active = True
        link.last_checked_at = dt.datetime.now(dt.UTC)
        if link.owner_user_id is None:
            link.owner_user_id = owner_user_id

    target.is_active = True
    if run_now:
        schedule_target_once(db, target)
    db.commit()
    return {
        "ok": True,
        "target_id": int(target.id),
        "target_name": target.name,
        "identifier": target.identifier,
        "created": bool(created),
        "linked_now": bool(linked_now),
        "run_now": bool(run_now),
    }


@router.post("/modules/telegram/targets/smart-add")
def telegram_smart_add_targets(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    entries_text = str(payload.get("entries_text") or "")
    entries = parse_bulk_targets_input(entries_text)
    if not entries:
        return {"ok": True, "processed": 0, "created_targets": 0, "linked_accounts": 0, "scheduled": 0}

    limit = int(payload.get("limit") or 200)
    poll_interval_seconds = int(payload.get("poll_interval_seconds") or 300)
    live_enabled = bool(payload.get("live_enabled", True))
    gapfill_limit = int(payload.get("gapfill_limit") or 200)
    participants_sync_enabled = bool(payload.get("participants_sync_enabled", True))
    participants_sync_interval_seconds = int(payload.get("participants_sync_interval_seconds") or 3600)
    participants_limit = int(payload.get("participants_limit") or 1000)
    backfill_enabled = bool(payload.get("backfill_enabled", False))
    backfill_mode = str(payload.get("backfill_mode") or "range")
    backfill_chunk_days = int(payload.get("backfill_chunk_days") or 7)
    backfill_limit = int(payload.get("backfill_limit") or 5000)
    backfill_from = _normalize_backfill_datetime_input(str(payload.get("backfill_from") or ""))
    backfill_to = _normalize_backfill_datetime_input(str(payload.get("backfill_to") or ""))
    run_now = bool(payload.get("run_now", True))

    accounts_stmt = select(ParserAccount).where(
        ParserAccount.parser_type == ParserType.telegram,
        ParserAccount.is_active.is_(True),
    )
    if not _is_admin(user):
        accounts_stmt = accounts_stmt.where(ParserAccount.owner_user_id == int(user.id))
    accounts = db.execute(accounts_stmt).scalars().all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Немає активних Telegram-акаунтів")

    now = dt.datetime.now(dt.UTC)
    account_info: dict[int, dict] = {}
    for account in accounts:
        creds = dict(account.credentials or {})
        dialogs = creds.get("dialogs_cache")
        if not isinstance(dialogs, list):
            dialogs = []

        if not dialogs:
            try:
                dialogs = refresh_account_dialogs_sync(creds, limit=700)
            except Exception:
                dialogs = []
            if dialogs:
                creds["dialogs_cache"] = dialogs
                creds["dialogs_cached_at"] = now.isoformat()
                account.credentials = creds
                db.add(account)

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

    created_targets = 0
    linked_accounts = 0
    scheduled = 0
    scheduled_target_ids: set[int] = set()
    target_config = _telegram_target_config(
        limit=limit,
        poll_interval_seconds=poll_interval_seconds,
        live_enabled=live_enabled,
        gapfill_limit=gapfill_limit,
        participants_sync_enabled=participants_sync_enabled,
        participants_sync_interval_seconds=participants_sync_interval_seconds,
        participants_limit=participants_limit,
        backfill_enabled=backfill_enabled,
        backfill_mode=backfill_mode,
        backfill_from=backfill_from,
        backfill_to=backfill_to,
        backfill_chunk_days=backfill_chunk_days,
        backfill_limit=backfill_limit,
    )

    for entry in entries:
        normalized_entry = normalize_telegram_identifier(entry)
        matches: list[tuple[ParserAccount, dict]] = []
        for account in accounts:
            dialog = find_dialog_match(entry, account_info[account.id]["dialogs"])
            if dialog:
                matches.append((account, dialog))

        if matches:
            canonical_identifier = normalize_telegram_identifier(str(matches[0][1].get("identifier") or normalized_entry or entry))
            canonical_name = str(matches[0][1].get("title") or canonical_identifier)
            candidates = [account for account, _ in matches]
        else:
            canonical_identifier = normalized_entry or str(entry).strip()
            canonical_name = str(entry).strip()
            candidates = list(accounts)

        if not canonical_identifier:
            continue

        target_stmt = select(Target).where(
            Target.parser_type == ParserType.telegram,
            Target.identifier == canonical_identifier,
        )
        if not _is_admin(user):
            target_stmt = target_stmt.where(Target.owner_user_id == int(user.id))
        target = db.execute(target_stmt).scalar_one_or_none()

        selected_account = sorted(
            candidates,
            key=lambda account: account_info[account.id]["base_score"] + (account_info[account.id]["assigned"] * 2.0),
        )[0]
        account_info[selected_account.id]["assigned"] += 1

        owner_user_id = selected_account.owner_user_id or int(user.id)
        if target:
            target = _ensure_target_access(target, user)
            target.is_active = True
            if not target.name:
                target.name = canonical_name
            if not target.config:
                target.config = dict(target_config)
        else:
            target = Target(
                parser_type=ParserType.telegram,
                name=canonical_name,
                identifier=canonical_identifier,
                owner_user_id=owner_user_id,
                config=dict(target_config),
                is_active=True,
            )
            db.add(target)
            db.flush()
            created_targets += 1

        existing_link = db.execute(
            select(TargetAccountLink).where(
                TargetAccountLink.target_id == target.id,
                TargetAccountLink.account_id == selected_account.id,
            )
        ).scalar_one_or_none()
        if existing_link:
            existing_link.is_active = True
            existing_link.auto_detected = True
            existing_link.last_checked_at = now
            if existing_link.owner_user_id is None:
                existing_link.owner_user_id = owner_user_id
        else:
            db.add(
                TargetAccountLink(
                    target_id=target.id,
                    account_id=selected_account.id,
                    owner_user_id=owner_user_id,
                    is_active=True,
                    auto_detected=True,
                    last_checked_at=now,
                )
            )
            linked_accounts += 1

        if run_now and int(target.id) not in scheduled_target_ids:
            schedule_target_once(db, target)
            scheduled += 1
            scheduled_target_ids.add(int(target.id))

    db.commit()
    return {
        "ok": True,
        "processed": len(entries),
        "created_targets": int(created_targets),
        "linked_accounts": int(linked_accounts),
        "scheduled": int(scheduled),
    }


@router.get("/telegram/targets-overview")
def telegram_targets_overview(db: Session = Depends(get_db), user=Depends(get_current_user)):
    stmt = select(Target).where(Target.parser_type == ParserType.telegram)
    if not _is_admin(user):
        stmt = stmt.where(Target.owner_user_id == int(user.id))
    targets = db.execute(stmt.order_by(Target.name.asc(), Target.id.asc())).scalars().all()
    rows = (
        db.execute(
            select(RawEvent.target_id, func.count(RawEvent.id), func.max(RawEvent.created_at))
            .where(RawEvent.parser_type == ParserType.telegram)
            .group_by(RawEvent.target_id)
        )
        .all()
    )
    stats = {int(row[0]): {"events_count": int(row[1] or 0), "last_event_at": row[2]} for row in rows}
    return [
        {
            "id": target.id,
            "name": target.name,
            "identifier": target.identifier,
            "is_active": bool(target.is_active),
            "events_count": int(stats.get(target.id, {}).get("events_count", 0)),
            "last_event_at": stats.get(target.id, {}).get("last_event_at").isoformat()
            if stats.get(target.id, {}).get("last_event_at")
            else None,
            "last_event_text": _format_kyiv_datetime(stats.get(target.id, {}).get("last_event_at")),
        }
        for target in targets
    ]


@router.get("/search/status")
def search_status(db: Session = Depends(get_db), user=Depends(get_current_user)):
    payload = search_index.status()
    payload["autosync"] = get_search_autosync_state(db)
    return payload


@router.get("/search/messages")
def search_messages(
    q: str = "",
    limit: int = 100,
    parser_type: str = "",
    target_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_filter = str(parser_type or "").strip().lower()
    if parser_filter and parser_filter not in {ParserType.telegram.value, ParserType.darknet.value}:
        raise HTTPException(status_code=400, detail="Некоректний parser_type")

    if target_id is not None:
        target = _ensure_target_access(db.get(Target, int(target_id)), user)
        if parser_filter and target.parser_type != parser_filter:
            raise HTTPException(status_code=400, detail="target_id не відповідає parser_type")
        parser_filter = target.parser_type

    status = search_index.status()
    if not bool(status.get("enabled")):
        raise HTTPException(status_code=503, detail="OpenSearch вимкнено")
    if not bool(status.get("package_installed")):
        raise HTTPException(status_code=503, detail="Пакет opensearch-py не встановлено")

    owner_filter = None if _is_admin(user) else int(user.id)
    try:
        hits, total = search_index.search_messages(
            query=q,
            limit=limit,
            owner_user_id=owner_filter,
            parser_type=parser_filter or None,
            target_id=target_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    for item in hits:
        observed_raw = str(item.get("observed_at") or "").strip()
        observed_dt = None
        if observed_raw:
            try:
                observed_dt = dt.datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
                if observed_dt.tzinfo is None:
                    observed_dt = observed_dt.replace(tzinfo=dt.UTC)
            except Exception:
                observed_dt = None
        item["observed_at_text"] = _format_kyiv_datetime(observed_dt)

    return {"hits": hits, "total": int(total)}


@router.post("/search/reindex")
def reindex_messages_search(payload: dict | None = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    body = payload if isinstance(payload, dict) else {}
    parser_filter = str(body.get("parser_type") or "").strip().lower()
    if parser_filter and parser_filter not in {ParserType.telegram.value, ParserType.darknet.value}:
        raise HTTPException(status_code=400, detail="Некоректний parser_type")
    parser_enum = ParserType(parser_filter) if parser_filter else None

    target_id_raw = body.get("target_id")
    target_id = int(target_id_raw) if target_id_raw is not None else None
    if target_id is not None:
        target = _ensure_target_access(db.get(Target, int(target_id)), user)
        if parser_enum and target.parser_type != parser_enum:
            raise HTTPException(status_code=400, detail="target_id не відповідає parser_type")
        parser_enum = target.parser_type

    safe_limit = max(100, min(int(body.get("limit") or 2000), 5000))
    from_event_id = max(int(body.get("from_event_id") or 0), 0)

    status = search_index.status()
    if not bool(status.get("enabled")):
        raise HTTPException(status_code=503, detail="OpenSearch вимкнено")
    if not bool(status.get("package_installed")):
        raise HTTPException(status_code=503, detail="Пакет opensearch-py не встановлено")

    stmt = _owned_events_stmt(user).where(RawEvent.id > from_event_id)
    if parser_enum:
        stmt = stmt.where(RawEvent.parser_type == parser_enum)
    if target_id is not None:
        stmt = stmt.where(RawEvent.target_id == int(target_id))

    events = db.execute(stmt.order_by(RawEvent.id.asc()).limit(safe_limit)).scalars().all()
    target_cache: dict[int, Target | None] = {}
    indexed = 0
    skipped = 0

    for event in events:
        cached_target = target_cache.get(int(event.target_id), None)
        if int(event.target_id) not in target_cache:
            cached_target = db.get(Target, int(event.target_id))
            target_cache[int(event.target_id)] = cached_target
        target = cached_target
        if not target:
            skipped += 1
            continue

        event_payload = event.payload
        ok = search_index.index_raw_event(
            event=event,
            payload=event_payload,
            target=target,
        )
        if ok:
            indexed += 1
        else:
            skipped += 1

    last_event_id = int(events[-1].id) if events else int(from_event_id)
    return {
        "ok": True,
        "processed": int(len(events)),
        "indexed": int(indexed),
        "skipped": int(skipped),
        "last_event_id": last_event_id,
        "has_more": bool(len(events) == safe_limit),
    }


@router.get("/telegram/targets/{target_id}/messages")
def telegram_target_messages(
    target_id: int,
    limit: int = 200,
    q: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    target = db.get(Target, target_id)
    if not target or target.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram ціль не знайдена")
    _ensure_target_access(target, user)

    safe_limit = max(20, min(int(limit or 200), 1000))
    query = str(q or "").strip().lower()
    events = (
        db.execute(
            select(RawEvent)
            .where(RawEvent.parser_type == ParserType.telegram, RawEvent.target_id == target_id)
            .order_by(desc(RawEvent.observed_at), desc(RawEvent.id))
            .limit(max(safe_limit * 3, safe_limit))
        )
        .scalars()
        .all()
    )

    result: list[dict] = []
    for event in events:
        payload = event.payload
        event_type = str(payload.get("event_type") or "")
        if event_type not in {"telegram_message", "telegram_comment"}:
            continue

        sender_raw = payload.get("sender")
        sender = sender_raw if isinstance(sender_raw, dict) else {}
        username = str(sender.get("username") or "").strip().removeprefix("@")
        first_name = str(sender.get("first_name") or "").strip()
        last_name = str(sender.get("last_name") or "").strip()
        sender_id = sender.get("id")
        full_name = " ".join(part for part in [first_name, last_name] if part).strip()
        sender_label = f"@{username}" if username else (full_name or (f"ID {sender_id}" if sender_id else "Невідомий автор"))
        text = str(payload.get("text") or "").strip() or "(без тексту)"

        if query:
            haystack = " ".join([text, sender_label, str(sender_id or ""), str(event.external_id or "")]).lower()
            if query not in haystack:
                continue

        raw_date = str(payload.get("date") or "").strip()
        observed_at = event.observed_at or event.created_at
        if raw_date:
            try:
                parsed_date = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                observed_at = parsed_date if parsed_date.tzinfo else parsed_date.replace(tzinfo=dt.UTC)
            except Exception:
                pass
        result.append(
            {
                "id": event.id,
                "external_id": event.external_id,
                "message_kind": "Коментар" if event_type == "telegram_comment" else "Повідомлення",
                "is_comment": event_type == "telegram_comment",
                "sender_label": sender_label,
                "sender_id": sender_id,
                "text": text,
                "root_post_id": payload.get("root_post_id"),
                "parent_message_id": payload.get("parent_message_id"),
                "observed_at": observed_at.isoformat() if observed_at else None,
                "observed_at_text": _format_kyiv_datetime(observed_at),
                "_sort_observed_at": observed_at or dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
            }
        )
    result.sort(key=lambda item: (item["_sort_observed_at"], item["id"]))
    result = result[-safe_limit:]
    for item in result:
        item.pop("_sort_observed_at", None)
    return {"target": {"id": target.id, "name": target.name, "identifier": target.identifier}, "messages": result}


@router.get("/telegram/targets/{target_id}/users")
def telegram_target_users(target_id: int, limit: int = 500, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = db.get(Target, target_id)
    if not target or target.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram ціль не знайдена")
    _ensure_target_access(target, user)

    safe_limit = max(20, min(int(limit or 500), 5000))
    rows = (
        db.execute(
            select(TelegramMembership, TelegramUser)
            .join(TelegramUser, TelegramMembership.telegram_user_ref_id == TelegramUser.id)
            .where(TelegramMembership.target_id == target_id)
            .order_by(desc(TelegramMembership.last_seen_at), desc(TelegramMembership.updated_at))
            .limit(safe_limit * 5)
        )
        .all()
    )
    users_map: dict[int, dict] = {}
    for membership, tg_user in rows:
        tg_user_id = int(tg_user.telegram_user_id)
        row = users_map.get(tg_user_id)
        if row is None:
            full_name = " ".join(part for part in [str(tg_user.first_name or "").strip(), str(tg_user.last_name or "").strip()] if part).strip()
            display_name = f"@{tg_user.username}" if tg_user.username else (full_name or f"ID {tg_user.telegram_user_id}")
            row = {
                "telegram_user_id": tg_user_id,
                "display_name": display_name,
                "username": f"@{tg_user.username}" if tg_user.username else None,
                "full_name": full_name or None,
                "is_bot": bool(tg_user.is_bot),
                "is_verified": bool(tg_user.is_verified),
                "is_deleted": bool(tg_user.is_deleted),
                "memberships_count": 0,
                "statuses": set(),
                "is_active_any": False,
                "last_seen_at": None,
            }
            users_map[tg_user_id] = row
        row["memberships_count"] += 1
        row["is_active_any"] = bool(row["is_active_any"] or bool(membership.is_active))
        status = str(membership.membership_status or "").strip()
        if status:
            row["statuses"].add(status)
        seen = membership.last_seen_at or membership.updated_at or membership.created_at
        if seen and (row["last_seen_at"] is None or seen > row["last_seen_at"]):
            row["last_seen_at"] = seen

    items = list(users_map.values())
    items.sort(
        key=lambda item: (
            item["last_seen_at"] or dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
            item["telegram_user_id"],
        ),
        reverse=True,
    )
    response = []
    for item in items[:safe_limit]:
        statuses = sorted(item["statuses"])
        status_text = ", ".join(statuses[:2]) if statuses else "-"
        if len(statuses) > 2:
            status_text = f"{status_text} +{len(statuses) - 2}"
        response.append(
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
                "last_seen_at": item["last_seen_at"].isoformat() if item["last_seen_at"] else None,
                "last_seen_text": _format_kyiv_datetime(item["last_seen_at"]),
            }
        )
    return {"target": {"id": target.id, "name": target.name, "identifier": target.identifier}, "users": response}


@router.get("/telegram/targets/{target_id}/risk-users")
def telegram_target_risk_users(
    target_id: int,
    limit: int = 2000,
    risky_only: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    target = db.get(Target, target_id)
    if not target or target.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram ціль не знайдена")
    _ensure_target_access(target, user)

    safe_limit = max(20, min(int(limit or 2000), 20000))

    target_rows = (
        db.execute(
            select(TelegramMembership, TelegramUser)
            .join(TelegramUser, TelegramMembership.telegram_user_ref_id == TelegramUser.id)
            .where(TelegramMembership.target_id == int(target.id))
            .order_by(desc(TelegramMembership.last_seen_at), desc(TelegramMembership.updated_at))
            .limit(safe_limit * 5)
        )
        .all()
    )

    users_map: dict[int, dict] = {}
    for membership, tg_user in target_rows:
        ref_id = int(tg_user.id)
        row = users_map.get(ref_id)
        if row is None:
            first_name = str(tg_user.first_name or "").strip()
            last_name = str(tg_user.last_name or "").strip()
            full_name = " ".join(part for part in [first_name, last_name] if part).strip()
            username = str(tg_user.username or "").strip()
            display_name = f"@{username}" if username else (full_name or f"ID {int(tg_user.telegram_user_id)}")
            row = {
                "telegram_user_ref_id": ref_id,
                "telegram_user_id": int(tg_user.telegram_user_id),
                "display_name": display_name,
                "username": f"@{username}" if username else None,
                "full_name": full_name or None,
                "last_seen_at": None,
            }
            users_map[ref_id] = row
        seen_at = membership.last_seen_at or membership.updated_at or membership.created_at
        if seen_at and (row["last_seen_at"] is None or seen_at > row["last_seen_at"]):
            row["last_seen_at"] = seen_at

    if not users_map:
        return {
            "target": {"id": int(target.id), "name": str(target.name), "identifier": str(target.identifier)},
            "checked_users": 0,
            "risky_users": 0,
            "risky_only": bool(risky_only),
            "rows": [],
        }

    all_targets_stmt = select(Target).where(Target.parser_type == ParserType.telegram)
    if not _is_admin(user):
        all_targets_stmt = all_targets_stmt.where(Target.owner_user_id == int(user.id))
    all_targets = db.execute(all_targets_stmt).scalars().all()

    target_meta: dict[int, dict] = {}
    risky_target_ids: list[int] = []
    for item in all_targets:
        is_risky, risk_label = _target_risk_meta(item)
        item_id = int(item.id)
        target_meta[item_id] = {
            "target_id": item_id,
            "target_name": str(item.name),
            "target_identifier": str(item.identifier),
            "is_risky": bool(is_risky),
            "risk_label": risk_label,
        }
        if bool(is_risky) and item_id != int(target.id):
            risky_target_ids.append(item_id)

    risk_map: dict[int, dict[int, dict]] = {}
    if risky_target_ids:
        membership_rows = (
            db.execute(
                select(TelegramMembership)
                .where(
                    TelegramMembership.telegram_user_ref_id.in_(list(users_map.keys())),
                    TelegramMembership.target_id.in_(risky_target_ids),
                )
                .order_by(desc(TelegramMembership.last_seen_at), desc(TelegramMembership.updated_at))
                .limit(200000)
            )
            .scalars()
            .all()
        )
        for membership in membership_rows:
            user_map = risk_map.setdefault(int(membership.telegram_user_ref_id), {})
            target_state = user_map.get(int(membership.target_id))
            seen_at = membership.last_seen_at or membership.updated_at or membership.created_at
            if target_state is None:
                target_state = {
                    "status_set": set(),
                    "is_active_any": False,
                    "last_seen_at": seen_at,
                }
                user_map[int(membership.target_id)] = target_state
            status_value = str(membership.membership_status or "").strip()
            if status_value:
                target_state["status_set"].add(status_value)
            target_state["is_active_any"] = bool(target_state["is_active_any"] or bool(membership.is_active))
            if seen_at and (target_state["last_seen_at"] is None or seen_at > target_state["last_seen_at"]):
                target_state["last_seen_at"] = seen_at

    rows: list[dict] = []
    risky_users = 0
    for user_row in users_map.values():
        memberships_by_target = risk_map.get(int(user_row["telegram_user_ref_id"]), {})
        risky_targets: list[dict] = []
        for risky_target_id, state in memberships_by_target.items():
            meta = target_meta.get(int(risky_target_id))
            if not meta:
                continue
            statuses = sorted(state["status_set"])
            seen_at = state["last_seen_at"]
            risky_targets.append(
                {
                    "target_id": int(risky_target_id),
                    "target_name": meta["target_name"],
                    "target_identifier": meta["target_identifier"],
                    "risk_label": meta.get("risk_label"),
                    "is_active": bool(state["is_active_any"]),
                    "status": ", ".join(statuses[:3]) if statuses else "-",
                    "last_seen_at": seen_at.isoformat() if seen_at else None,
                    "last_seen_text": _format_kyiv_datetime(seen_at),
                }
            )
        risky_targets.sort(key=lambda item: item["last_seen_at"] or "", reverse=True)
        risky_total = len(risky_targets)
        if risky_total > 0:
            risky_users += 1
        if risky_only and risky_total == 0:
            continue
        rows.append(
            {
                "telegram_user_id": int(user_row["telegram_user_id"]),
                "display_name": user_row["display_name"],
                "username": user_row["username"],
                "full_name": user_row["full_name"],
                "last_seen_at": user_row["last_seen_at"].isoformat() if user_row["last_seen_at"] else None,
                "last_seen_text": _format_kyiv_datetime(user_row["last_seen_at"]),
                "risky_targets_total": risky_total,
                "risky_targets": risky_targets[:20],
            }
        )

    rows.sort(
        key=lambda item: (
            int(item["risky_targets_total"]),
            item["last_seen_at"] or "",
            int(item["telegram_user_id"]),
        ),
        reverse=True,
    )
    rows = rows[:safe_limit]
    return {
        "target": {"id": int(target.id), "name": str(target.name), "identifier": str(target.identifier)},
        "checked_users": len(users_map),
        "risky_users": int(risky_users),
        "risky_only": bool(risky_only),
        "rows": rows,
    }


@router.get("/telegram/users/search")
def telegram_users_search(q: str = "", limit: int = 50, db: Session = Depends(get_db), user=Depends(get_current_user)):
    safe_limit = max(5, min(int(limit or 50), 200))
    query = str(q or "").strip()
    username_like = query.removeprefix("@").strip().lower()
    like_value = f"%{username_like}%"
    last_seen_expr = func.max(func.coalesce(TelegramMembership.last_seen_at, TelegramMembership.updated_at, TelegramMembership.created_at)).label(
        "last_seen_at"
    )

    stmt = (
        select(
            TelegramUser.telegram_user_id,
            TelegramUser.username,
            TelegramUser.first_name,
            TelegramUser.last_name,
            TelegramUser.is_bot,
            TelegramUser.is_verified,
            TelegramUser.is_deleted,
            func.count(func.distinct(TelegramMembership.target_id)).label("targets_count"),
            last_seen_expr,
        )
        .join(TelegramMembership, TelegramMembership.telegram_user_ref_id == TelegramUser.id)
        .join(Target, Target.id == TelegramMembership.target_id)
        .where(Target.parser_type == ParserType.telegram)
    )
    if not _is_admin(user):
        stmt = stmt.where(Target.owner_user_id == int(user.id))

    if username_like:
        search_conditions = [
            func.lower(func.coalesce(TelegramUser.username, "")).like(like_value),
            func.lower(func.coalesce(TelegramUser.first_name, "")).like(like_value),
            func.lower(func.coalesce(TelegramUser.last_name, "")).like(like_value),
        ]
        if username_like.lstrip("-").isdigit():
            search_conditions.append(TelegramUser.telegram_user_id == int(username_like))
        stmt = stmt.where(or_(*search_conditions))

    rows = (
        db.execute(
            stmt.group_by(
                TelegramUser.id,
                TelegramUser.telegram_user_id,
                TelegramUser.username,
                TelegramUser.first_name,
                TelegramUser.last_name,
                TelegramUser.is_bot,
                TelegramUser.is_verified,
                TelegramUser.is_deleted,
            )
            .order_by(desc(last_seen_expr), TelegramUser.telegram_user_id.desc())
            .limit(safe_limit)
        )
        .all()
    )

    users = []
    for row in rows:
        tg_user_id = int(row.telegram_user_id)
        username = str(row.username or "").strip()
        first_name = str(row.first_name or "").strip()
        last_name = str(row.last_name or "").strip()
        full_name = " ".join(part for part in [first_name, last_name] if part).strip()
        display_name = f"@{username}" if username else (full_name or f"ID {tg_user_id}")
        users.append(
            {
                "telegram_user_id": tg_user_id,
                "display_name": display_name,
                "username": f"@{username}" if username else None,
                "full_name": full_name or None,
                "is_bot": bool(row.is_bot),
                "is_verified": bool(row.is_verified),
                "is_deleted": bool(row.is_deleted),
                "targets_count": int(row.targets_count or 0),
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                "last_seen_text": _format_kyiv_datetime(row.last_seen_at),
            }
        )
    return {"users": users}


@router.post("/telegram/intel/cross-check")
def telegram_intel_cross_check(payload: dict | None = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    body = payload if isinstance(payload, dict) else {}
    text = str(body.get("text") or "")

    usernames: set[str] = set(_extract_telegram_usernames(text))
    usernames_input = body.get("usernames")
    if isinstance(usernames_input, str):
        raw_items = re.split(r"[\s,;]+", usernames_input)
        usernames.update(item for item in (_normalize_telegram_username(item) for item in raw_items) if item)
    elif isinstance(usernames_input, list):
        usernames.update(item for item in (_normalize_telegram_username(str(v)) for v in usernames_input) if item)

    extracted_usernames = sorted(usernames)
    if not extracted_usernames:
        return {
            "extracted_usernames": [],
            "matched": [],
            "unresolved_usernames": [],
            "mention_only": [],
            "search_unavailable": False,
        }

    targets_stmt = select(Target).where(Target.parser_type == ParserType.telegram)
    if not _is_admin(user):
        targets_stmt = targets_stmt.where(Target.owner_user_id == int(user.id))
    targets = db.execute(targets_stmt).scalars().all()
    target_ids = [int(item.id) for item in targets]
    if not target_ids:
        return {
            "extracted_usernames": extracted_usernames,
            "matched": [],
            "unresolved_usernames": extracted_usernames,
            "mention_only": [],
            "search_unavailable": False,
        }

    target_meta: dict[int, dict] = {}
    for target in targets:
        is_risky, risk_label = _target_risk_meta(target)
        target_meta[int(target.id)] = {
            "target_id": int(target.id),
            "target_name": str(target.name),
            "target_identifier": str(target.identifier),
            "is_risky": bool(is_risky),
            "risk_label": risk_label,
        }

    users = (
        db.execute(
            select(TelegramUser).where(func.lower(func.coalesce(TelegramUser.username, "")).in_(extracted_usernames))
        )
        .scalars()
        .all()
    )
    users_by_username: dict[str, TelegramUser] = {}
    for tg_user in users:
        uname = _normalize_telegram_username(str(tg_user.username or ""))
        if uname:
            users_by_username[uname] = tg_user

    membership_map: dict[int, dict[int, dict]] = {}
    tg_user_ids = [int(item.id) for item in users]
    if tg_user_ids:
        membership_rows = (
            db.execute(
                select(TelegramMembership)
                .where(
                    TelegramMembership.telegram_user_ref_id.in_(tg_user_ids),
                    TelegramMembership.target_id.in_(target_ids),
                )
                .order_by(desc(TelegramMembership.last_seen_at), desc(TelegramMembership.updated_at))
                .limit(50000)
            )
            .scalars()
            .all()
        )
        for membership in membership_rows:
            user_memberships = membership_map.setdefault(int(membership.telegram_user_ref_id), {})
            target_id = int(membership.target_id)
            row = user_memberships.get(target_id)
            seen_at = membership.last_seen_at or membership.updated_at or membership.created_at
            if row is None:
                row = {
                    "status_set": set(),
                    "is_active_any": False,
                    "last_seen_at": seen_at,
                }
                user_memberships[target_id] = row
            status_value = str(membership.membership_status or "").strip()
            if status_value:
                row["status_set"].add(status_value)
            row["is_active_any"] = bool(row["is_active_any"] or bool(membership.is_active))
            if seen_at and (row["last_seen_at"] is None or seen_at > row["last_seen_at"]):
                row["last_seen_at"] = seen_at

    search_unavailable = False
    owner_filter = None if _is_admin(user) else int(user.id)
    mention_by_username: dict[str, dict] = {}
    for username in extracted_usernames:
        mention_targets: dict[int, dict] = {}
        mention_hits_total = 0
        try:
            hits, total = search_index.search_messages(
                query=f"@{username}",
                limit=200,
                owner_user_id=owner_filter,
                parser_type=ParserType.telegram.value,
                target_id=None,
            )
            mention_hits_total = int(total or 0)
            for hit in hits:
                target_id = int(hit.get("target_id") or 0)
                if target_id <= 0:
                    continue
                meta = target_meta.get(target_id)
                if not meta:
                    continue
                row = mention_targets.get(target_id)
                if row is None:
                    row = {
                        "target_id": target_id,
                        "target_name": meta["target_name"],
                        "target_identifier": meta["target_identifier"],
                        "is_risky": bool(meta["is_risky"]),
                        "risk_label": meta.get("risk_label"),
                        "hits": 0,
                    }
                    mention_targets[target_id] = row
                row["hits"] += 1
        except Exception:
            search_unavailable = True

        mention_rows = sorted(mention_targets.values(), key=lambda item: int(item["hits"]), reverse=True)
        mention_by_username[username] = {
            "hits_total": mention_hits_total,
            "targets": mention_rows,
        }

    matched: list[dict] = []
    mention_only: list[dict] = []
    unresolved: list[str] = []

    for username in extracted_usernames:
        tg_user = users_by_username.get(username)
        mention_payload = mention_by_username.get(username) or {"hits_total": 0, "targets": []}
        if tg_user is None:
            unresolved.append(f"@{username}")
            if mention_payload["targets"]:
                mention_only.append(
                    {
                        "username": f"@{username}",
                        "mentions_total": int(mention_payload["hits_total"] or 0),
                        "mention_targets": mention_payload["targets"],
                    }
                )
            continue

        user_target_map = membership_map.get(int(tg_user.id), {})
        target_rows: list[dict] = []
        for target_id, state in user_target_map.items():
            meta = target_meta.get(int(target_id))
            if not meta:
                continue
            statuses = sorted(state["status_set"])
            last_seen_at = state["last_seen_at"]
            target_rows.append(
                {
                    "target_id": int(target_id),
                    "target_name": meta["target_name"],
                    "target_identifier": meta["target_identifier"],
                    "is_risky": bool(meta["is_risky"]),
                    "risk_label": meta.get("risk_label"),
                    "is_active": bool(state["is_active_any"]),
                    "status": ", ".join(statuses[:3]) if statuses else "-",
                    "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
                    "last_seen_text": _format_kyiv_datetime(last_seen_at),
                }
            )
        target_rows.sort(key=lambda item: item["last_seen_at"] or "", reverse=True)
        risky_targets = [item for item in target_rows if bool(item.get("is_risky"))]
        safe_targets = [item for item in target_rows if not bool(item.get("is_risky"))]

        first_name = str(tg_user.first_name or "").strip()
        last_name = str(tg_user.last_name or "").strip()
        full_name = " ".join(part for part in [first_name, last_name] if part).strip() or None
        display_name = f"@{username}" if username else (full_name or f"ID {int(tg_user.telegram_user_id)}")
        matched.append(
            {
                "telegram_user_id": int(tg_user.telegram_user_id),
                "display_name": display_name,
                "username": f"@{username}",
                "full_name": full_name,
                "targets_total": len(target_rows),
                "risky_targets_total": len(risky_targets),
                "safe_targets_total": len(safe_targets),
                "risky_targets": risky_targets,
                "other_targets": safe_targets[:20],
                "mentions_total": int(mention_payload["hits_total"] or 0),
                "mention_targets": mention_payload["targets"],
                "is_verified": bool(tg_user.is_verified),
                "is_bot": bool(tg_user.is_bot),
                "is_deleted": bool(tg_user.is_deleted),
                "last_seen_text": _format_kyiv_datetime(_coerce_utc(tg_user.last_seen_at)),
            }
        )

    return {
        "extracted_usernames": [f"@{item}" for item in extracted_usernames],
        "matched": matched,
        "unresolved_usernames": unresolved,
        "mention_only": mention_only,
        "search_unavailable": bool(search_unavailable),
    }


@router.post("/telegram/targets/{target_id}/intel-extract")
def telegram_target_intel_extract(
    target_id: int,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    target = _ensure_target_access(db.get(Target, target_id), user)
    if target.parser_type != ParserType.telegram:
        raise HTTPException(status_code=404, detail="Telegram ціль не знайдена")

    body = payload if isinstance(payload, dict) else {}
    full_scan = bool(body.get("full_scan", False))
    per_batch_limit = max(200, min(int(body.get("scan_limit") or 5000), 20000))
    max_scan_events = max(per_batch_limit, min(int(body.get("max_scan_events") or 200000), 1_000_000))

    if full_scan:
        events: list[RawEvent] = []
        cursor_id: int | None = None
        while len(events) < max_scan_events:
            remaining = max_scan_events - len(events)
            limit = min(per_batch_limit, remaining)
            stmt = (
                select(RawEvent)
                .where(
                    RawEvent.parser_type == ParserType.telegram,
                    RawEvent.target_id == int(target.id),
                )
            )
            if cursor_id is not None:
                stmt = stmt.where(RawEvent.id < int(cursor_id))
            batch = db.execute(stmt.order_by(RawEvent.id.desc()).limit(limit)).scalars().all()
            if not batch:
                break
            events.extend(batch)
            cursor_id = int(batch[-1].id)
    else:
        events = (
            db.execute(
                select(RawEvent)
                .where(
                    RawEvent.parser_type == ParserType.telegram,
                    RawEvent.target_id == int(target.id),
                )
                .order_by(desc(RawEvent.observed_at), desc(RawEvent.id))
                .limit(per_batch_limit)
            )
            .scalars()
            .all()
        )

    usernames_stats: dict[str, dict] = {}
    scanned_events = 0
    for event in events:
        payload_dict = event.payload
        event_type = str(payload_dict.get("event_type") or "")
        if event_type not in {"telegram_message", "telegram_comment"}:
            continue
        scanned_events += 1
        text = str(payload_dict.get("text") or "").strip()
        if not text:
            continue
        found_usernames = _extract_telegram_usernames(text)
        if not found_usernames:
            continue

        observed_at = event.observed_at or event.created_at
        if observed_at and observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=dt.UTC)
        snippet = text.replace("\n", " ").strip()
        if len(snippet) > 180:
            snippet = f"{snippet[:180]}..."
        for username in found_usernames:
            row = usernames_stats.get(username)
            if row is None:
                row = {
                    "username": f"@{username}",
                    "mentions": 0,
                    "last_seen_at": observed_at,
                    "sample_text": snippet,
                }
                usernames_stats[username] = row
            row["mentions"] += 1
            if observed_at and (row["last_seen_at"] is None or observed_at > row["last_seen_at"]):
                row["last_seen_at"] = observed_at
                row["sample_text"] = snippet

    extracted_usernames = sorted(usernames_stats.keys())
    cross_check = telegram_intel_cross_check(payload={"usernames": [f"@{item}" for item in extracted_usernames]}, db=db, user=user)

    candidates = []
    for username in extracted_usernames:
        row = usernames_stats[username]
        candidates.append(
            {
                "username": row["username"],
                "mentions": int(row["mentions"]),
                "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
                "last_seen_text": _format_kyiv_datetime(row["last_seen_at"]),
                "sample_text": row["sample_text"],
            }
        )
    candidates.sort(key=lambda item: (int(item["mentions"]), str(item["last_seen_at"] or "")), reverse=True)

    return {
        "target": {"id": int(target.id), "name": str(target.name), "identifier": str(target.identifier)},
        "scan_limit": int(per_batch_limit),
        "full_scan": bool(full_scan),
        "max_scan_events": int(max_scan_events),
        "truncated": bool(full_scan and len(events) >= max_scan_events),
        "scanned_events": int(scanned_events),
        "extracted_usernames": [f"@{item}" for item in extracted_usernames],
        "candidates": candidates,
        "cross_check": cross_check,
    }


@router.get("/telegram/users/{telegram_user_id}/profile")
def telegram_user_profile(
    telegram_user_id: int,
    limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    safe_limit = max(20, min(int(limit or 200), 500))
    scan_limit = min(max(safe_limit * 40, 500), 20000)

    tg_user = db.execute(select(TelegramUser).where(TelegramUser.telegram_user_id == int(telegram_user_id))).scalar_one_or_none()
    if tg_user is None:
        raise HTTPException(status_code=404, detail="Telegram користувача не знайдено")

    targets_stmt = select(Target.id, Target.name, Target.identifier).where(Target.parser_type == ParserType.telegram)
    if not _is_admin(user):
        targets_stmt = targets_stmt.where(Target.owner_user_id == int(user.id))
    target_rows = db.execute(targets_stmt).all()
    target_map = {int(item.id): {"name": str(item.name), "identifier": str(item.identifier)} for item in target_rows}
    target_ids = list(target_map.keys())

    memberships: list[dict] = []
    if target_ids:
        membership_rows = (
            db.execute(
                select(TelegramMembership, Target)
                .join(Target, Target.id == TelegramMembership.target_id)
                .where(
                    TelegramMembership.telegram_user_ref_id == tg_user.id,
                    TelegramMembership.target_id.in_(target_ids),
                )
                .order_by(desc(TelegramMembership.last_seen_at), desc(TelegramMembership.updated_at))
                .limit(1000)
            )
            .all()
        )
        for membership, target in membership_rows:
            seen = membership.last_seen_at or membership.updated_at or membership.created_at
            memberships.append(
                {
                    "target_id": int(target.id),
                    "target_name": str(target.name),
                    "target_identifier": str(target.identifier),
                    "status": str(membership.membership_status or "-"),
                    "is_active": bool(membership.is_active),
                    "last_seen_at": seen.isoformat() if seen else None,
                    "last_seen_text": _format_kyiv_datetime(seen),
                }
            )

    messages: list[dict] = []
    if target_ids:
        events = (
            db.execute(
                select(RawEvent)
                .where(
                    RawEvent.parser_type == ParserType.telegram,
                    RawEvent.target_id.in_(target_ids),
                )
                .order_by(desc(RawEvent.observed_at), desc(RawEvent.id))
                .limit(scan_limit)
            )
            .scalars()
            .all()
        )

        for event in events:
            payload = event.payload
            event_type = str(payload.get("event_type") or "")
            if event_type not in {"telegram_message", "telegram_comment"}:
                continue

            sender_raw = payload.get("sender")
            sender = sender_raw if isinstance(sender_raw, dict) else {}
            sender_id_raw = sender.get("id")
            try:
                sender_id = int(sender_id_raw)
            except Exception:
                continue
            if sender_id != int(telegram_user_id):
                continue

            username = str(sender.get("username") or "").strip().removeprefix("@")
            first_name = str(sender.get("first_name") or "").strip()
            last_name = str(sender.get("last_name") or "").strip()
            full_name = " ".join(part for part in [first_name, last_name] if part).strip()
            sender_label = f"@{username}" if username else (full_name or f"ID {sender_id}")
            text = str(payload.get("text") or "").strip() or "(без тексту)"

            observed_at = event.observed_at or event.created_at
            raw_date = str(payload.get("date") or "").strip()
            if raw_date:
                try:
                    parsed_date = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    observed_at = parsed_date if parsed_date.tzinfo else parsed_date.replace(tzinfo=dt.UTC)
                except Exception:
                    pass

            target_info = target_map.get(int(event.target_id), {"name": f"Ціль #{event.target_id}", "identifier": "-"})
            messages.append(
                {
                    "id": int(event.id),
                    "target_id": int(event.target_id),
                    "target_name": str(target_info["name"]),
                    "target_identifier": str(target_info["identifier"]),
                    "external_id": event.external_id,
                    "message_kind": "Коментар" if event_type == "telegram_comment" else "Повідомлення",
                    "is_comment": event_type == "telegram_comment",
                    "sender_label": sender_label,
                    "sender_id": sender_id,
                    "text": text,
                    "root_post_id": payload.get("root_post_id"),
                    "parent_message_id": payload.get("parent_message_id"),
                    "observed_at": observed_at.isoformat() if observed_at else None,
                    "observed_at_text": _format_kyiv_datetime(observed_at),
                }
            )
            if len(messages) >= safe_limit:
                break

    if not memberships and not messages:
        raise HTTPException(status_code=404, detail="Профіль недоступний для ваших цілей")

    username = str(tg_user.username or "").strip()
    first_name = str(tg_user.first_name or "").strip()
    last_name = str(tg_user.last_name or "").strip()
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    display_name = f"@{username}" if username else (full_name or f"ID {tg_user.telegram_user_id}")

    return {
        "profile": {
            "telegram_user_id": int(tg_user.telegram_user_id),
            "display_name": display_name,
            "username": f"@{username}" if username else None,
            "full_name": full_name or None,
            "is_bot": bool(tg_user.is_bot),
            "is_verified": bool(tg_user.is_verified),
            "is_deleted": bool(tg_user.is_deleted),
            "last_seen_text": _format_kyiv_datetime(_coerce_utc(tg_user.last_seen_at)),
        },
        "memberships": memberships,
        "messages": messages,
    }


@router.get("/darknet/users/search")
def darknet_users_search(
    q: str = "",
    limit: int = 50,
    target_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    safe_limit = max(5, min(int(limit or 50), 200))
    query = str(q or "").strip().lower()
    query_no_prefix = query[1:] if query.startswith("@") else query
    like_value = f"%{query_no_prefix}%"
    last_seen_expr = func.max(
        func.coalesce(DarknetUserMembership.last_seen_at, DarknetUserMembership.updated_at, DarknetUserMembership.created_at)
    ).label("last_seen_at")

    stmt = (
        select(
            DarknetUser.id,
            DarknetUser.forum_host,
            DarknetUser.username,
            DarknetUser.username_normalized,
            DarknetUser.display_name,
            func.count(func.distinct(DarknetUserMembership.target_id)).label("targets_count"),
            func.count(DarknetUserMembership.id).label("threads_count"),
            func.sum(func.coalesce(DarknetUserMembership.posts_count, 0)).label("posts_count"),
            last_seen_expr,
        )
        .join(DarknetUserMembership, DarknetUserMembership.darknet_user_ref_id == DarknetUser.id)
        .join(Target, Target.id == DarknetUserMembership.target_id)
        .where(Target.parser_type == ParserType.darknet)
    )

    if target_id is not None:
        target = _ensure_target_access(db.get(Target, int(target_id)), user)
        if target.parser_type != ParserType.darknet:
            raise HTTPException(status_code=400, detail="target_id не відповідає darknet")
        stmt = stmt.where(DarknetUserMembership.target_id == int(target.id))

    if not _is_admin(user):
        stmt = stmt.where(Target.owner_user_id == int(user.id))

    if query_no_prefix:
        search_conditions = [
            func.lower(func.coalesce(DarknetUser.username_normalized, "")).like(like_value),
            func.lower(func.coalesce(DarknetUser.display_name, "")).like(like_value),
            func.lower(func.coalesce(DarknetUser.forum_host, "")).like(like_value),
        ]
        if query_no_prefix.isdigit():
            search_conditions.append(DarknetUser.id == int(query_no_prefix))
        stmt = stmt.where(or_(*search_conditions))

    rows = (
        db.execute(
            stmt.group_by(
                DarknetUser.id,
                DarknetUser.forum_host,
                DarknetUser.username,
                DarknetUser.username_normalized,
                DarknetUser.display_name,
            )
            .order_by(desc(last_seen_expr), DarknetUser.id.desc())
            .limit(safe_limit)
        )
        .all()
    )

    users = []
    for row in rows:
        username = str(row.username or "").strip()
        display_name_raw = str(row.display_name or "").strip()
        if username and (not display_name_raw or display_name_raw == f"@{username}"):
            display_name = username
        else:
            display_name = display_name_raw or f"User #{int(row.id)}"
        users.append(
            {
                "darknet_user_id": int(row.id),
                "display_name": display_name,
                "username": username or None,
                "forum_host": str(row.forum_host or "-"),
                "targets_count": int(row.targets_count or 0),
                "threads_count": int(row.threads_count or 0),
                "posts_count": int(row.posts_count or 0),
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                "last_seen_text": _format_kyiv_datetime(row.last_seen_at),
            }
        )
    return {"users": users}


@router.get("/darknet/users/{darknet_user_id}/profile")
def darknet_user_profile(
    darknet_user_id: int,
    limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    safe_limit = max(20, min(int(limit or 200), 500))
    scan_limit = min(max(safe_limit * 40, 500), 20000)
    profile = db.get(DarknetUser, int(darknet_user_id))
    if profile is None:
        raise HTTPException(status_code=404, detail="Darknet користувача не знайдено")

    memberships_stmt = (
        select(DarknetUserMembership, Target)
        .join(Target, Target.id == DarknetUserMembership.target_id)
        .where(
            DarknetUserMembership.darknet_user_ref_id == int(profile.id),
            Target.parser_type == ParserType.darknet,
        )
    )
    if not _is_admin(user):
        memberships_stmt = memberships_stmt.where(Target.owner_user_id == int(user.id))

    membership_rows = (
        db.execute(memberships_stmt.order_by(desc(DarknetUserMembership.last_seen_at), desc(DarknetUserMembership.updated_at)).limit(2000))
        .all()
    )
    if not membership_rows:
        raise HTTPException(status_code=404, detail="Профіль недоступний для ваших цілей")

    memberships: list[dict] = []
    target_map: dict[int, dict] = {}
    for membership, target in membership_rows:
        seen = membership.last_seen_at or membership.updated_at or membership.created_at
        target_map[int(target.id)] = {"name": str(target.name), "identifier": str(target.identifier)}
        memberships.append(
            {
                "target_id": int(target.id),
                "target_name": str(target.name),
                "target_identifier": str(target.identifier),
                "thread_url": str(membership.thread_url or "-"),
                "thread_title": str(membership.thread_title or "").strip() or None,
                "posts_count": int(membership.posts_count or 0),
                "status": str(membership.last_source or "-"),
                "is_active": bool(membership.is_active),
                "last_seen_at": seen.isoformat() if seen else None,
                "last_seen_text": _format_kyiv_datetime(seen),
            }
        )

    target_ids = list(target_map.keys())
    messages: list[dict] = []
    if target_ids:
        events = (
            db.execute(
                select(RawEvent)
                .where(
                    RawEvent.parser_type == ParserType.darknet,
                    RawEvent.target_id.in_(target_ids),
                )
                .order_by(desc(RawEvent.observed_at), desc(RawEvent.id))
                .limit(scan_limit)
            )
            .scalars()
            .all()
        )

        for event in events:
            payload = event.payload
            if str(payload.get("event_type") or "") != "forum_post":
                continue

            author_norm = _normalize_darknet_username(str(payload.get("author") or ""))
            if not author_norm or author_norm != str(profile.username_normalized or ""):
                continue

            thread_url = str(payload.get("thread_url") or "").strip()
            if thread_url and profile.forum_host:
                parsed_host = str(urlparse(thread_url).netloc or "").strip().lower()
                if parsed_host and parsed_host != str(profile.forum_host).lower():
                    continue

            text = str(payload.get("text") or "").strip() or "(без тексту)"
            observed_at = event.observed_at or event.created_at
            raw_date = str(payload.get("posted_at") or "").strip()
            if raw_date:
                try:
                    parsed_date = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    observed_at = parsed_date if parsed_date.tzinfo else parsed_date.replace(tzinfo=dt.UTC)
                except Exception:
                    pass

            target_info = target_map.get(int(event.target_id), {"name": f"Ціль #{event.target_id}", "identifier": "-"})
            messages.append(
                {
                    "id": int(event.id),
                    "target_id": int(event.target_id),
                    "target_name": str(target_info["name"]),
                    "target_identifier": str(target_info["identifier"]),
                    "thread_url": thread_url or None,
                    "thread_title": str(payload.get("thread_title") or "").strip() or None,
                    "author": str(payload.get("author") or "").strip() or None,
                    "text": text,
                    "external_id": event.external_id,
                    "observed_at": observed_at.isoformat() if observed_at else None,
                    "observed_at_text": _format_kyiv_datetime(observed_at),
                }
            )
            if len(messages) >= safe_limit:
                break

    return {
        "profile": {
            "darknet_user_id": int(profile.id),
            "display_name": (
                profile.username
                if profile.username
                and (
                    not str(profile.display_name or "").strip()
                    or str(profile.display_name or "").strip() == f"@{profile.username}"
                )
                else (str(profile.display_name or "").strip() or f"User #{int(profile.id)}")
            ),
            "username": str(profile.username or "").strip() or None,
            "forum_host": str(profile.forum_host or "-"),
            "last_seen_text": _format_kyiv_datetime(_coerce_utc(profile.last_seen_at)),
        },
        "memberships": memberships,
        "messages": messages,
    }


@router.post("/darknet/profiles/rebuild")
def darknet_profiles_rebuild(payload: dict | None = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    body = payload if isinstance(payload, dict) else {}
    safe_limit = max(100, min(int(body.get("limit") or 10000), 200000))
    target_id_raw = body.get("target_id")
    target_id = int(target_id_raw) if target_id_raw is not None else None

    target_identifier_by_id: dict[int, str] = {}
    if target_id is not None:
        target = _ensure_target_access(db.get(Target, target_id), user)
        if target.parser_type != ParserType.darknet:
            raise HTTPException(status_code=400, detail="target_id не відповідає darknet")
        target_identifier_by_id[int(target.id)] = str(target.identifier or "")

    stmt = select(RawEvent).where(RawEvent.parser_type == ParserType.darknet)
    if target_id is not None:
        stmt = stmt.where(RawEvent.target_id == int(target_id))
    if not _is_admin(user):
        stmt = stmt.where(RawEvent.owner_user_id == int(user.id))

    events = db.execute(stmt.order_by(RawEvent.id.asc()).limit(safe_limit)).scalars().all()
    if not events:
        return {"ok": True, "processed": 0, "upserted": 0}

    if not target_identifier_by_id:
        target_ids = sorted({int(item.target_id) for item in events})
        targets_stmt = select(Target.id, Target.identifier, Target.owner_user_id).where(
            Target.id.in_(target_ids),
            Target.parser_type == ParserType.darknet,
        )
        if not _is_admin(user):
            targets_stmt = targets_stmt.where(Target.owner_user_id == int(user.id))
        for target_row in db.execute(targets_stmt).all():
            target_identifier_by_id[int(target_row.id)] = str(target_row.identifier or "")

    upserted = 0
    processed = 0
    for event in events:
        target_identifier = target_identifier_by_id.get(int(event.target_id))
        if not target_identifier:
            continue
        event_payload = event.payload
        processed += 1
        changed = upsert_darknet_profile_from_event(
            session=db,
            target_id=int(event.target_id),
            target_identifier=target_identifier,
            account_id=int(event.account_id) if event.account_id is not None else None,
            payload=event_payload,
            observed_at=event.observed_at,
        )
        if changed:
            upserted += 1
        if processed % 500 == 0:
            db.commit()
    db.commit()
    return {"ok": True, "processed": int(processed), "upserted": int(upserted)}


@router.get("/jobs")
def list_jobs(limit: int = 100, db: Session = Depends(get_db), user=Depends(get_current_user)):
    jobs = db.execute(_owned_jobs_stmt(user).order_by(desc(ParseJob.created_at)).limit(min(limit, 200))).scalars().all()
    return [
        {
            "id": j.id,
            "parser_type": j.parser_type,
            "target_id": j.target_id,
            "account_id": j.account_id,
            "owner_user_id": j.owner_user_id,
            "status": j.status.value,
            "job_key": j.job_key,
            "attempt": j.attempt,
            "error": j.last_error,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }
        for j in jobs
    ]


@router.get("/events")
def list_events(limit: int = 100, db: Session = Depends(get_db), user=Depends(get_current_user)):
    events = db.execute(_owned_events_stmt(user).order_by(desc(RawEvent.created_at)).limit(min(limit, 200))).scalars().all()
    return [
        {
            "id": e.id,
            "parser_type": e.parser_type,
            "target_id": e.target_id,
            "account_id": e.account_id,
            "owner_user_id": e.owner_user_id,
            "external_id": e.external_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


@router.get("/events/{event_id}/payload")
def get_event_payload(event_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    event = _ensure_event_access(db, db.get(RawEvent, event_id), user)
    return {"id": event.id, "payload": event.payload}
