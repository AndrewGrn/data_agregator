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
    config.addinivalue_line("markers", "clickhouse: requires a real ClickHouse server")


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


@pytest.fixture
def ch_client():
    """Sync clickhouse-connect client against a throwaway `_test` database.

    Skipped when TEST_CLICKHOUSE_URL is unset (e.g. http://default:@127.0.0.1:8123/aggregator_test).
    Drops and recreates the events table on every use.
    """
    url = os.environ.get("TEST_CLICKHOUSE_URL")
    if not url:
        pytest.skip("TEST_CLICKHOUSE_URL not set")
    from urllib.parse import urlparse

    parsed = urlparse(url)
    database = parsed.path.strip("/") or "aggregator_test"
    if not database.endswith("_test"):
        pytest.fail(f"refusing to wipe {database!r}: TEST_CLICKHOUSE_URL database must end in '_test'")

    import clickhouse_connect
    from app.services import clickhouse_store as store

    admin = clickhouse_connect.get_client(
        host=parsed.hostname or "127.0.0.1", port=parsed.port or 8123,
        username=parsed.username or "default", password=parsed.password or "",
    )
    admin.command(f"CREATE DATABASE IF NOT EXISTS {database}")
    admin.command(f"DROP TABLE IF EXISTS {database}.{store.EVENTS_TABLE}")
    client = clickhouse_connect.get_client(
        host=parsed.hostname or "127.0.0.1", port=parsed.port or 8123,
        username=parsed.username or "default", password=parsed.password or "", database=database,
    )
    store.ensure_schema_sync(client)
    yield client
    client.close()
    admin.close()
