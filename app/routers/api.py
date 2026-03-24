import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_current_admin, get_current_user
from app.db import get_db
from app.models import (
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
from app.services.object_store import object_store
from app.services.scheduler import schedule_once, schedule_target_once, sync_telegram_memberships
from app.services.telegram_accounts import account_parallel_limits

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


def _job_kind_from_key(job_key: str | None) -> str:
    key = str(job_key or "")
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
        "is_admin": bool(user.is_admin),
        "role": user.role.value if user.role else ("admin" if user.is_admin else "user"),
        "is_active": bool(user.is_active),
        "totp_confirmed": bool(user.totp_confirmed),
    }


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
    return {
        "ok": True,
        "user": {
            "id": user.id,
            "username": user.username,
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
            "role": item.role.value if item.role else ("admin" if item.is_admin else "user"),
            "is_admin": bool(item.is_admin),
            "is_active": bool(item.is_active),
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

    db.add(item)
    db.commit()
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
                "parser_type": item.parser_type.value,
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
                "parser_type": item.parser_type.value,
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
            "parser_type": item.parser_type.value,
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
            "parser_type": a.parser_type.value,
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
def telegram_module_data(db: Session = Depends(get_db), user=Depends(get_current_user)):
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

    target_ids = [target.id for target in targets]
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
            process_text = "Працює за розкладом" if last_success else "Готово до першого запуску"

        target_rows.append(
            {
                "id": target.id,
                "name": target.name,
                "identifier": target.identifier,
                "is_active": bool(target.is_active),
                "running": int(running),
                "queued": int(queued),
                "failed": int(failed),
                "events_count": int(events_count),
                "last_success_at": last_success.isoformat() if last_success else None,
                "last_success_text": _format_kyiv_datetime(last_success),
                "process_text": process_text,
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
                "error": job.last_error,
                "wait_reason": reason,
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            }
        )

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
        payload = event.payload if isinstance(event.payload, dict) else object_store.get_json(event)
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


@router.get("/jobs")
def list_jobs(limit: int = 100, db: Session = Depends(get_db), user=Depends(get_current_user)):
    jobs = db.execute(_owned_jobs_stmt(user).order_by(desc(ParseJob.created_at)).limit(min(limit, 200))).scalars().all()
    return [
        {
            "id": j.id,
            "parser_type": j.parser_type.value,
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
            "parser_type": e.parser_type.value,
            "target_id": e.target_id,
            "account_id": e.account_id,
            "owner_user_id": e.owner_user_id,
            "external_id": e.external_id,
            "storage_type": e.storage_type,
            "payload_ref": e.payload_ref,
            "payload_size": e.payload_size,
            "payload_sha256": e.payload_sha256,
            "payload_preview": e.payload_preview,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


@router.get("/events/{event_id}/payload")
def get_event_payload(event_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    event = _ensure_event_access(db, db.get(RawEvent, event_id), user)
    payload = object_store.get_json(event)
    if payload is None:
        raise HTTPException(status_code=404, detail="payload not available")
    return {"id": event.id, "payload": payload}
