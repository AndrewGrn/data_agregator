from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, UserRole


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    # A bearer token authenticates as its owner without a session, so an
    # integration does not have to hold a cookie obtained with a one-time 2FA
    # code. Checked first: when a caller sends a token, a stale cookie must not
    # silently win.
    raw_token = _bearer_token(request)
    if raw_token is not None:
        from app.services.api_tokens import resolve_token

        user = resolve_token(db, raw_token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недійсний токен")
        return user

    user_id = request.session.get("user_id")
    session_version = request.session.get("session_version")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Потрібна авторизація")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недійсна сесія")
    if not bool(user.is_active):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Користувач деактивований")
    try:
        current_session_version = int(session_version)
    except (TypeError, ValueError):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сесію відкликано, увійдіть повторно")
    if int(user.session_version or 1) != current_session_version:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сесію відкликано, увійдіть повторно")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not bool(user.is_admin or user.role == UserRole.admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Потрібні права адміністратора")
    return user
