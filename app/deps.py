from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, UserRole


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Потрібна авторизація")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недійсна сесія")
    if not bool(user.is_active):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Користувач деактивований")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not bool(user.is_admin or user.role == UserRole.admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Потрібні права адміністратора")
    return user
