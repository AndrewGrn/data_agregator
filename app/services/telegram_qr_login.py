from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

import segno
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ponytail: in-process registry; one uvicorn worker (as in compose). Move to
# Redis/DB if the API ever runs with several workers.
_SESSIONS: dict[str, "QrSession"] = {}
_PRUNE_AFTER = dt.timedelta(minutes=15)

ClientFactory = Callable[[int, str], Any]


@dataclass
class QrSession:
    token: str
    api_id: int
    api_hash: str
    owner_user_id: int
    label: str
    hourly_limit: int
    status: str = "pending"
    url: str | None = None
    qr_svg: str | None = None
    error: str | None = None
    session_string: str | None = None
    me: dict[str, Any] = field(default_factory=dict)
    account_id: int | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    expires_at: dt.datetime | None = None
    _client: Any = None
    _qr: Any = None
    _password: asyncio.Future | None = None
    _task: asyncio.Task | None = None


def _default_client_factory(api_id: int, api_hash: str) -> Any:
    return TelegramClient(StringSession(), api_id, api_hash)


def _svg(url: str) -> str:
    return segno.make(url, error="m").svg_data_uri(scale=6, border=1)


def _prune(now: dt.datetime) -> None:
    for token in [t for t, s in _SESSIONS.items() if now - s.created_at > _PRUNE_AFTER]:
        _SESSIONS.pop(token, None)


async def start_qr_login(
    *,
    api_id: int,
    api_hash: str,
    owner_user_id: int,
    label: str,
    hourly_limit: int,
    client_factory: ClientFactory | None = None,
) -> QrSession:
    # ponytail: resolved lazily (not a bound default) so tests can monkeypatch
    # `_default_client_factory` on the module and have callers that omit
    # client_factory pick up the patched version.
    factory = client_factory or _default_client_factory
    now = dt.datetime.now(dt.UTC)
    _prune(now)
    s = QrSession(
        token=secrets.token_urlsafe(24),
        api_id=int(api_id),
        api_hash=str(api_hash),
        owner_user_id=int(owner_user_id),
        label=label,
        hourly_limit=int(hourly_limit),
        expires_at=now + dt.timedelta(seconds=int(settings.telegram_qr_login_ttl_seconds)),
    )
    s._client = factory(s.api_id, s.api_hash)
    await s._client.connect()
    s._qr = await s._client.qr_login()
    s.url = s._qr.url
    s.qr_svg = _svg(s.url)
    s._task = asyncio.create_task(_run(s))
    _SESSIONS[s.token] = s
    logger.info("qr-login started token=%s owner=%s label=%s", s.token[:8], owner_user_id, label)
    return s


async def _run(s: QrSession) -> None:
    try:
        while True:
            now = dt.datetime.now(dt.UTC)
            remaining = (s.expires_at - now).total_seconds()
            if remaining <= 0:
                s.status = "expired"
                return
            token_left = max((s._qr.expires - now).total_seconds(), 0.05)
            try:
                await s._qr.wait(timeout=min(token_left, remaining))
            except asyncio.TimeoutError:
                await s._qr.recreate()
                s.url = s._qr.url
                s.qr_svg = _svg(s.url)
                continue
            except SessionPasswordNeededError:
                s.status = "password_needed"
                s._password = asyncio.get_running_loop().create_future()
                try:
                    password = await asyncio.wait_for(s._password, timeout=max(remaining, 30))
                except asyncio.TimeoutError:
                    s.status = "expired"
                    return
                await s._client.sign_in(password=password)
            break

        me = await s._client.get_me()
        s.me = {
            "username": (str(getattr(me, "username", "") or "").strip().lower() or None),
            "phone": (str(getattr(me, "phone", "") or "").strip() or None),
            "user_id": int(getattr(me, "id", 0) or 0) or None,
        }
        s.session_string = s._client.session.save()
        s.status = "done"
        logger.info("qr-login done token=%s user=%s", s.token[:8], s.me.get("username"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        s.status = "error"
        s.error = str(exc)[:500]
        logger.warning("qr-login error token=%s: %s", s.token[:8], exc)
    finally:
        try:
            await s._client.disconnect()
        except Exception:
            pass


def get_qr_session(token: str) -> QrSession | None:
    return _SESSIONS.get(str(token or ""))


def submit_password(token: str, password: str) -> bool:
    s = get_qr_session(token)
    if s is None or s.status != "password_needed" or s._password is None or s._password.done():
        return False
    s._password.set_result(password)
    return True


async def cancel_qr_login(token: str) -> None:
    s = _SESSIONS.pop(str(token or ""), None)
    if s is None:
        return
    if s._task and not s._task.done():
        s._task.cancel()
        try:
            await s._task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        await s._client.disconnect()
    except Exception:
        pass


def public_view(s: QrSession) -> dict[str, Any]:
    return {
        "token": s.token,
        "status": s.status,
        "url": s.url,
        "qr_svg": s.qr_svg,
        "error": s.error,
        "account_id": s.account_id,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
    }
