from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Generator
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings

settings = get_settings()


def _json_default(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, set):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


# Raw payloads (notably Telethon's msg.to_dict()) carry datetime/bytes values
# that bare json.dumps cannot encode; without this every JSONB bind raises.
json_serializer = partial(json.dumps, default=_json_default)


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Build an engine that can serialize our payloads. Tests use it too."""
    return create_engine(url, json_serializer=json_serializer, **kwargs)


engine = make_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
