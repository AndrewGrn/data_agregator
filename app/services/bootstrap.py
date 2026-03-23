from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User
from app.security import hash_password


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
        return user

    user = User(username=settings.default_admin_username, password_hash=hash_password(settings.default_admin_password), is_admin=True)
    session.add(user)
    session.flush()
    return user
