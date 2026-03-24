from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import verify_password, verify_totp_code

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    otp_code: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невірний логін або пароль")
    if not bool(user.is_active):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Користувач деактивований")
    if not bool(user.totp_enabled) or not bool(user.totp_confirmed):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Потрібно завершити налаштування 2FA")
    if not verify_totp_code(user.totp_secret, otp_code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невірний 2FA код")

    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
