import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: requires a real PostgreSQL database")


@pytest.fixture
def pg_session():
    """Session against a throwaway Postgres schema.

    Skipped when TEST_DATABASE_URL is unset. Refuses any database whose name
    does not end in `_test`: this fixture drops the whole public schema, and
    the development database holds real targets, accounts and jobs.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")

    db_name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not db_name.endswith("_test"):
        pytest.fail(
            f"refusing to wipe {db_name!r}: TEST_DATABASE_URL must point at a "
            f"database whose name ends in '_test'"
        )

    from app.db import Base

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE raw_events ADD COLUMN text_search tsvector "
                "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
            )
        )
        conn.execute(text("CREATE INDEX ix_raw_events_text_search ON raw_events USING GIN (text_search)"))

    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()
