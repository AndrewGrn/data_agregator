from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, UserRole
from app.security import generate_totp_secret, hash_password


settings = get_settings()


def run_migrations() -> None:
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    try:
        command.upgrade(alembic_cfg, "head")
    except Exception as exc:
        message = str(exc).lower()
        # Compatibility path for pre-Alembic databases created via create_all.
        if "already exists" in message:
            command.stamp(alembic_cfg, "head")
            return
        raise


def ensure_default_admin(session: Session) -> User:
    user = session.execute(select(User).where(User.username == settings.default_admin_username)).scalar_one_or_none()
    if user:
        updated = False
        if not user.is_admin:
            user.is_admin = True
            updated = True
        if user.role != UserRole.admin:
            user.role = UserRole.admin
            updated = True
        if not user.totp_secret:
            user.totp_secret = generate_totp_secret()
            updated = True
        if not user.totp_enabled:
            user.totp_enabled = True
            updated = True
        if updated:
            session.add(user)
        return user

    user = User(
        username=settings.default_admin_username,
        password_hash=hash_password(settings.default_admin_password),
        is_admin=True,
        role=UserRole.admin,
        totp_secret=generate_totp_secret(),
        totp_enabled=True,
        totp_confirmed=False,
    )
    session.add(user)
    session.flush()
    return user
