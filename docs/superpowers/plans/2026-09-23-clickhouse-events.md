# ClickHouse Events + Async Search API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every collected event is written to ClickHouse (alongside Postgres), existing events are backfilled, and an async FastAPI endpoint searches ClickHouse by nickname, tg id, phone, words in text, channel and period with cursor pagination.

**Architecture:** One module `app/services/clickhouse_store.py` owns the ClickHouse schema, row mapping, sync insert (called from the existing sync `persist_events`), backfill and the async search query builder. One router `app/routers/events_v2.py` exposes it. `persist_events` gains a single call to the store; nothing else in the app changes.

**Tech Stack:** ClickHouse (`clickhouse/clickhouse-server:latest`), `clickhouse-connect[async]==1.9.0`, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-23-clickhouse-events-design.md`

## Global Constraints

- Table `aggregator.events`, engine `ReplacingMergeTree(ingested_at) PARTITION BY toYYYYMM(observed_at) ORDER BY (parser_type, target_id, event_key)` — copied verbatim from the spec.
- `event_key` = `external_id` when non-empty, else `uuid4().hex`.
- Word/substring search runs against `text_lower` (a `MATERIALIZED lowerUTF8(text)` column) with the needle wrapped in `lowerUTF8(...)`. Verified on ClickHouse 26.9.1: `hasAllTokens` is case-sensitive and `hasTokenCaseInsensitive` folds ASCII only, so querying `text` directly silently returns nothing for Cyrillic.
- ClickHouse is reachable in this environment on `127.0.0.1:8123` (HTTP); native port 9000 is taken by MinIO and is not exposed.
- ClickHouse write failure must never raise out of `persist_events`; log at ERROR.
- All ClickHouse SQL uses server-side parameter binding `{name:Type}`; never f-string user input into SQL.
- No exact `total` count anywhere in the API.
- Tests needing ClickHouse: `pytestmark = pytest.mark.clickhouse`, skip without `TEST_CLICKHOUSE_URL`, refuse a database not ending in `_test`.
- Settings names exactly: `clickhouse_enabled`, `clickhouse_host`, `clickhouse_port`, `clickhouse_user`, `clickhouse_password`, `clickhouse_database` (already added to `app/config.py` by the controller).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Run tests as: `TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" TEST_CLICKHOUSE_URL="http://default:@127.0.0.1:8123/aggregator_test" python -m pytest -q -p no:warnings`.

---

### Task 1: ClickHouse store — schema, row mapping, dual-write, backfill, search query

**Files:**
- Create: `app/services/clickhouse_store.py`
- Modify: `app/services/event_sink.py` (inside `persist_events`, after the loop, before `return written`)
- Modify: `app/cli.py` (add `ch-backfill` command next to existing commands)
- Modify: `tests/conftest.py` (add `clickhouse` marker + `ch_client` fixture)
- Create: `tests/test_clickhouse_store.py`

**Interfaces:**
- Consumes: `app.config.get_settings().clickhouse_*`; `app.db.json_serializer`; `app.plugins.base.ParsedEvent` (fields: `external_id`, `observed_at`, `payload`, `text`, `author_id`, `author_label`, `event_kind`, `thread_id`, `reply_to`); `app.models.RawEvent`.
- Produces (Task 2 relies on these exact names):

```python
EVENTS_TABLE = "events"
TEXT_INDEX_AVAILABLE: bool          # set by ensure_schema_sync / ensure_schema

def get_sync_client(): ...          # cached clickhouse_connect Client from settings
async def get_async_client(): ...   # NEW AsyncClient each call; caller closes

def ensure_schema_sync(client=None) -> None
async def ensure_schema(client) -> None

def event_rows(*, target_id: int, parser_type: str, account_id: int | None,
               owner_user_id: int | None, events: list[ParsedEvent]) -> list[dict]
def insert_events_sync(rows: list[dict], client=None) -> int     # never raises
async def insert_events(client, rows: list[dict]) -> int

@dataclass(slots=True)
class EventFilters:
    q: str | None = None; contains: str | None = None; author_id: str | None = None
    nickname: str | None = None; phone: str | None = None; target_id: int | None = None
    parser_type: str | None = None; owner_user_id: int | None = None
    since: dt.datetime | None = None; until: dt.datetime | None = None

def encode_cursor(observed_at: dt.datetime, event_key: str) -> str
def decode_cursor(cursor: str) -> tuple[dt.datetime, str]      # raises ValueError on garbage
def build_search_sql(filters: EventFilters, *, limit: int, cursor: str | None) -> tuple[str, dict]
async def search_events(client, filters: EventFilters, *, limit: int, cursor: str | None) -> tuple[list[dict], str | None]
async def get_event(client, *, parser_type: str, target_id: int, event_key: str, owner_user_id: int | None) -> dict | None
def backfill_from_postgres(session, *, batch_size: int = 5000, client=None) -> int
```

- [ ] **Step 1: Add the `clickhouse` marker and `ch_client` fixture to `tests/conftest.py`**

Append to `pytest_configure`: `config.addinivalue_line("markers", "clickhouse: requires a real ClickHouse server")`. Append fixture:

```python
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
```

- [ ] **Step 2: Write the failing tests `tests/test_clickhouse_store.py`**

```python
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.plugins.base import ParsedEvent
from app.services import clickhouse_store as store

pytestmark = pytest.mark.clickhouse


def _ev(external_id, text, *, author_id="1", author_label="@alice", phone=None, minutes_ago=0, kind="message"):
    payload = {"raw": {"x": 1}, "sender": {"id": author_id, "username": author_label.lstrip("@"), "phone": phone}}
    return ParsedEvent(
        external_id=external_id, observed_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago),
        payload=payload, text=text, author_id=author_id, author_label=author_label, event_kind=kind,
    )


def _run(coro):
    return asyncio.run(coro)


async def _aclient():
    import os
    from urllib.parse import urlparse
    import clickhouse_connect
    p = urlparse(os.environ["TEST_CLICKHOUSE_URL"])
    return await clickhouse_connect.get_async_client(
        host=p.hostname, port=p.port or 8123, username=p.username or "default",
        password=p.password or "", database=p.path.strip("/"),
    )


def test_event_rows_maps_fields_and_derives_key_and_phone():
    rows = store.event_rows(target_id=7, parser_type="telegram", account_id=None, owner_user_id=3,
                            events=[_ev("42", "hi", phone="+380 67 123-45-67"), _ev(None, "no id")])
    assert rows[0]["event_key"] == "42" and rows[0]["external_id"] == "42"
    assert rows[0]["sender_phone"] == "380671234567"
    assert rows[0]["owner_user_id"] == 3 and rows[0]["account_id"] is None
    assert isinstance(rows[0]["payload"], str) and '"raw"' in rows[0]["payload"]
    assert rows[1]["external_id"] == "" and len(rows[1]["event_key"]) == 32


def test_insert_is_idempotent_by_key(ch_client):
    rows = store.event_rows(target_id=1, parser_type="telegram", account_id=None, owner_user_id=None,
                            events=[_ev("1", "first"), _ev("2", "second")])
    assert store.insert_events_sync(rows, ch_client) == 2
    assert store.insert_events_sync(rows, ch_client) == 2  # duplicates collapse on read
    n = ch_client.command(f"SELECT count() FROM {store.EVENTS_TABLE} FINAL")
    assert int(n) == 2


def test_hot_path_insert_uses_server_side_async_batching():
    """One row per listener message must not become one part per message."""
    seen = {}

    class _Spy:
        def insert(self, table, data, column_names=None, settings=None):
            seen["settings"] = settings
    rows = store.event_rows(target_id=1, parser_type="telegram", account_id=None, owner_user_id=None, events=[_ev("1", "x")])
    assert store.insert_events_sync(rows, _Spy()) == 1
    assert seen["settings"] == {"async_insert": 1, "wait_for_async_insert": 1}


def test_insert_never_raises_when_server_is_down():
    import clickhouse_connect
    bad = clickhouse_connect.get_client(host="127.0.0.1", port=1, connect_timeout=1, send_receive_timeout=1) if False else None
    # A None client with a broken settings host must not raise either.
    rows = store.event_rows(target_id=1, parser_type="telegram", account_id=None, owner_user_id=None, events=[_ev("1", "x")])
    class _Broken:
        def insert(self, *a, **k):
            raise ConnectionError("down")
    assert store.insert_events_sync(rows, _Broken()) == 0


def test_search_by_words_nickname_author_phone_and_cursor(ch_client):
    rows = store.event_rows(target_id=1, parser_type="telegram", account_id=None, owner_user_id=None, events=[
        _ev("1", "Курьеры наличных средств, писать в лс", author_id="111", author_label="@alice", phone="+380671111111", minutes_ago=3),
        _ev("2", "кошелёк bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh оплата", author_id="222", author_label="Bob Smith", minutes_ago=2),
        _ev("3", "совершенно другой текст", author_id="333", author_label="@carol", minutes_ago=1),
    ])
    store.insert_events_sync(rows, ch_client)

    async def go():
        c = await _aclient()
        try:
            items, nxt = await store.search_events(c, store.EventFilters(q="курьеры наличных"), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["1"]
            # Cyrillic word search must ignore case in BOTH directions (row 1 text is "Курьеры ...")
            items, _ = await store.search_events(c, store.EventFilters(q="КуРьЕрЫ"), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["1"]
            items, _ = await store.search_events(c, store.EventFilters(contains="УРЬЕР"), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["1"]
            items, _ = await store.search_events(c, store.EventFilters(contains="2n0yrf2493"), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["2"]
            items, _ = await store.search_events(c, store.EventFilters(nickname="bob"), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["2"]
            items, _ = await store.search_events(c, store.EventFilters(author_id="333"), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["3"]
            items, _ = await store.search_events(c, store.EventFilters(phone="+380 (67) 111-11-11"), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["1"]
            # newest first, cursor walks the rest
            page1, cur = await store.search_events(c, store.EventFilters(), limit=2, cursor=None)
            assert [i["external_id"] for i in page1] == ["3", "2"] and cur
            page2, cur2 = await store.search_events(c, store.EventFilters(), limit=2, cursor=cur)
            assert [i["external_id"] for i in page2] == ["1"] and cur2 is None
            assert "payload" not in page1[0]
            one = await store.get_event(c, parser_type="telegram", target_id=1, event_key="2", owner_user_id=None)
            assert one["payload"]["sender"]["id"] == "222"
            assert await store.get_event(c, parser_type="telegram", target_id=1, event_key="2", owner_user_id=99) is None
        finally:
            await c.close()
    _run(go())


def test_owner_filter_scopes_results(ch_client):
    rows = store.event_rows(target_id=1, parser_type="telegram", account_id=None, owner_user_id=5, events=[_ev("1", "mine")])
    rows += store.event_rows(target_id=2, parser_type="telegram", account_id=None, owner_user_id=6, events=[_ev("2", "theirs")])
    store.insert_events_sync(rows, ch_client)

    async def go():
        c = await _aclient()
        try:
            items, _ = await store.search_events(c, store.EventFilters(owner_user_id=5), limit=10, cursor=None)
            assert [i["external_id"] for i in items] == ["1"]
        finally:
            await c.close()
    _run(go())


def test_cursor_roundtrip_and_garbage():
    t = dt.datetime(2026, 9, 23, 12, 0, 0, 123000, tzinfo=dt.UTC)
    assert store.decode_cursor(store.encode_cursor(t, "abc")) == (t, "abc")
    with pytest.raises(ValueError):
        store.decode_cursor("not base64!!")


def test_build_search_sql_binds_parameters_never_interpolates():
    sql, params = store.build_search_sql(store.EventFilters(q="a' OR 1=1 --", nickname="x"), limit=10, cursor=None)
    assert "OR 1=1" not in sql and params["q"] == "a' OR 1=1 --"
    assert "{q:String}" in sql and "{limit_plus:UInt32}" in sql
    assert "text_lower" in sql, "word search must run against the lowercased column"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `TEST_CLICKHOUSE_URL="http://default:@127.0.0.1:8123/aggregator_test" python -m pytest tests/test_clickhouse_store.py -q -p no:warnings`
Expected: FAIL with `ModuleNotFoundError: app.services.clickhouse_store` (or `ch_client` fixture not found before Step 1).

- [ ] **Step 4: Implement `app/services/clickhouse_store.py`**

```python
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import clickhouse_connect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import json_serializer
from app.models import RawEvent
from app.plugins.base import ParsedEvent

logger = logging.getLogger(__name__)
settings = get_settings()

EVENTS_TABLE = "events"
TEXT_INDEX_AVAILABLE = True  # flipped to False by ensure_schema* if the server lacks TYPE text

_COLUMNS = [
    "parser_type", "target_id", "account_id", "owner_user_id", "external_id", "event_key",
    "observed_at", "event_kind", "thread_id", "reply_to", "author_id", "author_label",
    "sender_phone", "text", "payload",
]
_LIST_COLUMNS = [c for c in _COLUMNS if c != "payload"] + ["ingested_at"]

_DDL_BASE = f"""
CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
    parser_type LowCardinality(String),
    target_id UInt32,
    account_id Nullable(UInt32),
    owner_user_id Nullable(UInt32),
    external_id String,
    event_key String,
    observed_at DateTime64(3, 'UTC'),
    event_kind LowCardinality(String),
    thread_id String,
    reply_to String,
    author_id String,
    author_label String,
    sender_phone String,
    text String,
    -- hasAllTokens is case-sensitive and hasTokenCaseInsensitive folds ASCII only,
    -- so Cyrillic word search needs a lowercased copy to index and query against.
    text_lower String MATERIALIZED lowerUTF8(text),
    payload String CODEC(ZSTD(3)),
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3),
    INDEX idx_text_ngram text_lower TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 4,
    INDEX idx_author_id author_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_phone sender_phone TYPE bloom_filter GRANULARITY 4{{text_index}}
) ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(observed_at)
ORDER BY (parser_type, target_id, event_key)
"""
_TEXT_INDEX = ",\n    INDEX idx_text text_lower TYPE text(tokenizer = splitByNonAlpha) GRANULARITY 1"


def _client_kwargs() -> dict[str, Any]:
    return {
        "host": settings.clickhouse_host, "port": int(settings.clickhouse_port),
        "username": settings.clickhouse_user, "password": settings.clickhouse_password,
        "database": settings.clickhouse_database,
    }


@lru_cache(maxsize=1)
def get_sync_client():
    return clickhouse_connect.get_client(**_client_kwargs())


async def get_async_client():
    return await clickhouse_connect.get_async_client(**_client_kwargs())


def _is_unknown_text_index(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "unknown index type" in msg or "text" in msg and "index" in msg and "unknown" in msg


def ensure_schema_sync(client=None) -> None:
    """Idempotent DDL. Falls back to a table without the text index on servers that lack it."""
    global TEXT_INDEX_AVAILABLE
    client = client or get_sync_client()
    client.command(f"CREATE DATABASE IF NOT EXISTS {client.database}")
    try:
        client.command(_DDL_BASE.format(text_index=_TEXT_INDEX))
        TEXT_INDEX_AVAILABLE = True
    except Exception as exc:  # noqa: BLE001
        if not _is_unknown_text_index(exc):
            raise
        logger.warning("clickhouse: TYPE text index unsupported, creating events without it: %s", exc)
        client.command(_DDL_BASE.format(text_index=""))
        TEXT_INDEX_AVAILABLE = False


async def ensure_schema(client) -> None:
    global TEXT_INDEX_AVAILABLE
    await client.command(f"CREATE DATABASE IF NOT EXISTS {client.database}")
    try:
        await client.command(_DDL_BASE.format(text_index=_TEXT_INDEX))
        TEXT_INDEX_AVAILABLE = True
    except Exception as exc:  # noqa: BLE001
        if not _is_unknown_text_index(exc):
            raise
        logger.warning("clickhouse: TYPE text index unsupported, creating events without it: %s", exc)
        await client.command(_DDL_BASE.format(text_index=""))
        TEXT_INDEX_AVAILABLE = False


_DIGITS = re.compile(r"\D+")


def normalize_phone(value: Any) -> str:
    return _DIGITS.sub("", str(value or ""))


def _sender_phone(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    sender = payload.get("sender")
    if isinstance(sender, dict):
        return normalize_phone(sender.get("phone"))
    return ""


def _utc(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.UTC)
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def event_rows(*, target_id: int, parser_type: str, account_id: int | None, owner_user_id: int | None,
               events: list[ParsedEvent]) -> list[dict]:
    rows: list[dict] = []
    for ev in events:
        external_id = str(ev.external_id or "")
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        rows.append({
            "parser_type": str(parser_type),
            "target_id": int(target_id),
            "account_id": int(account_id) if account_id is not None else None,
            "owner_user_id": int(owner_user_id) if owner_user_id is not None else None,
            "external_id": external_id,
            "event_key": external_id or uuid.uuid4().hex,
            "observed_at": _utc(ev.observed_at),
            "event_kind": str(ev.event_kind or "message"),
            "thread_id": str(ev.thread_id or ""),
            "reply_to": str(ev.reply_to or ""),
            "author_id": str(ev.author_id or ""),
            "author_label": str(ev.author_label or ""),
            "sender_phone": _sender_phone(payload),
            "text": str(ev.text or ""),
            "payload": json_serializer(payload),
        })
    return rows


def rows_from_raw_events(raw_events: list[RawEvent]) -> list[dict]:
    """Same mapping as event_rows, for the Postgres backfill."""
    out: list[dict] = []
    for r in raw_events:
        external_id = str(r.external_id or "")
        payload = r.payload if isinstance(r.payload, dict) else {}
        out.append({
            "parser_type": str(r.parser_type), "target_id": int(r.target_id),
            "account_id": int(r.account_id) if r.account_id is not None else None,
            "owner_user_id": int(r.owner_user_id) if r.owner_user_id is not None else None,
            "external_id": external_id, "event_key": external_id or f"pg{int(r.id):028x}",
            "observed_at": _utc(r.observed_at or r.created_at), "event_kind": str(r.event_kind or "message"),
            "thread_id": str(r.thread_id or ""), "reply_to": str(r.reply_to or ""),
            "author_id": str(r.author_id or ""), "author_label": str(r.author_label or ""),
            "sender_phone": _sender_phone(payload), "text": str(r.text or ""),
            "payload": json_serializer(payload),
        })
    return out


# The live listener persists ONE message per call. A plain INSERT per call
# would create one on-disk part per message and hit "Too many parts" at scale.
# async_insert makes the server buffer small inserts and flush them in bulk;
# wait_for_async_insert=1 still acks only after the flush, so nothing is lost.
ASYNC_INSERT_SETTINGS = {"async_insert": 1, "wait_for_async_insert": 1}


def insert_events_sync(rows: list[dict], client=None) -> int:
    """Dual-write hook. Never raises: collection must not fail because the analytics store is down;
    `ch-backfill` re-syncs idempotently (ReplacingMergeTree)."""
    if not rows or not settings.clickhouse_enabled and client is None:
        return 0
    try:
        client = client or get_sync_client()
        client.insert(EVENTS_TABLE, [[row[c] for c in _COLUMNS] for row in rows],
                      column_names=_COLUMNS, settings=ASYNC_INSERT_SETTINGS)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("clickhouse insert failed for %d rows: %s", len(rows), exc)
        return 0


async def insert_events(client, rows: list[dict]) -> int:
    if not rows:
        return 0
    await client.insert(EVENTS_TABLE, [[row[c] for c in _COLUMNS] for row in rows],
                        column_names=_COLUMNS, settings=ASYNC_INSERT_SETTINGS)
    return len(rows)


@dataclass(slots=True)
class EventFilters:
    q: str | None = None
    contains: str | None = None
    author_id: str | None = None
    nickname: str | None = None
    phone: str | None = None
    target_id: int | None = None
    parser_type: str | None = None
    owner_user_id: int | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None


def encode_cursor(observed_at: dt.datetime, event_key: str) -> str:
    ms = int(_utc(observed_at).timestamp() * 1000)
    return base64.urlsafe_b64encode(f"{ms}:{event_key}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[dt.datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ms, key = raw.split(":", 1)
        return dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.UTC), key
    except Exception as exc:  # noqa: BLE001
        raise ValueError("bad cursor") from exc


_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _tokens(q: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(q or "")]


def build_search_sql(filters: EventFilters, *, limit: int, cursor: str | None) -> tuple[str, dict]:
    where: list[str] = []
    params: dict[str, Any] = {"limit_plus": int(limit) + 1}
    if filters.owner_user_id is not None:
        where.append("owner_user_id = {owner:UInt32}"); params["owner"] = int(filters.owner_user_id)
    if filters.parser_type:
        where.append("parser_type = {ptype:String}"); params["ptype"] = filters.parser_type
    if filters.target_id is not None:
        where.append("target_id = {tid:UInt32}"); params["tid"] = int(filters.target_id)
    if filters.author_id:
        where.append("author_id = {aid:String}"); params["aid"] = str(filters.author_id)
    if filters.phone:
        where.append("sender_phone = {phone:String}"); params["phone"] = normalize_phone(filters.phone)
    if filters.nickname:
        where.append("positionCaseInsensitiveUTF8(author_label, {nick:String}) > 0"); params["nick"] = filters.nickname
    if filters.contains:
        # both sides lowered by ClickHouse itself, so Python-side casing cannot drift
        where.append("position(text_lower, lowerUTF8({contains:String})) > 0"); params["contains"] = filters.contains
    if filters.q:
        params["q"] = filters.q
        if TEXT_INDEX_AVAILABLE:
            where.append("hasAllTokens(text_lower, lowerUTF8({q:String}))")
        else:
            for i, tok in enumerate(_tokens(filters.q)):
                where.append(f"hasToken(text_lower, lowerUTF8({{tok{i}:String}}))"); params[f"tok{i}"] = tok
    if filters.since is not None:
        where.append("observed_at >= {since:DateTime64(3)}"); params["since"] = _utc(filters.since)
    if filters.until is not None:
        where.append("observed_at < {until:DateTime64(3)}"); params["until"] = _utc(filters.until)
    if cursor:
        c_at, c_key = decode_cursor(cursor)
        where.append("(observed_at, event_key) < ({c_at:DateTime64(3)}, {c_key:String})")
        params["c_at"] = c_at; params["c_key"] = c_key
    sql = (
        f"SELECT {', '.join(_LIST_COLUMNS)} FROM {EVENTS_TABLE} FINAL"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY observed_at DESC, event_key DESC LIMIT {limit_plus:UInt32}"
    )
    return sql, params


def _row_dict(columns: list[str], row: tuple) -> dict:
    d = dict(zip(columns, row))
    for k in ("observed_at", "ingested_at"):
        if isinstance(d.get(k), dt.datetime):
            d[k] = _utc(d[k]).isoformat(timespec="milliseconds")
    return d


async def search_events(client, filters: EventFilters, *, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    sql, params = build_search_sql(filters, limit=limit, cursor=cursor)
    res = await client.query(sql, parameters=params)
    rows = [_row_dict(list(res.column_names), r) for r in res.result_rows]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = res.result_rows[limit - 1]
        idx_at, idx_key = res.column_names.index("observed_at"), res.column_names.index("event_key")
        next_cursor = encode_cursor(last[idx_at], last[idx_key])
    return rows, next_cursor


async def get_event(client, *, parser_type: str, target_id: int, event_key: str, owner_user_id: int | None) -> dict | None:
    where = "parser_type = {p:String} AND target_id = {t:UInt32} AND event_key = {k:String}"
    params: dict[str, Any] = {"p": parser_type, "t": int(target_id), "k": event_key}
    if owner_user_id is not None:
        where += " AND owner_user_id = {o:UInt32}"; params["o"] = int(owner_user_id)
    res = await client.query(
        f"SELECT {', '.join(_COLUMNS + ['ingested_at'])} FROM {EVENTS_TABLE} FINAL WHERE {where} LIMIT 1",
        parameters=params,
    )
    if not res.result_rows:
        return None
    d = _row_dict(list(res.column_names), res.result_rows[0])
    try:
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
    except ValueError:
        pass
    return d


def backfill_from_postgres(session: Session, *, batch_size: int = 5000, client=None) -> int:
    """Stream raw_events into ClickHouse in id order. Idempotent."""
    client = client or get_sync_client()
    ensure_schema_sync(client)
    total, last_id = 0, 0
    while True:
        batch = session.execute(
            select(RawEvent).where(RawEvent.id > last_id).order_by(RawEvent.id).limit(batch_size)
        ).scalars().all()
        if not batch:
            break
        rows = rows_from_raw_events(batch)
        # Bulk path: 5000-row batches are already the right part size, no async_insert needed.
        client.insert(EVENTS_TABLE, [[row[c] for c in _COLUMNS] for row in rows], column_names=_COLUMNS)
        total += len(rows); last_id = int(batch[-1].id)
        logger.info("ch-backfill: %d rows so far (last id %d)", total, last_id)
    return total
```

- [ ] **Step 5: Wire the dual-write into `persist_events`**

In `app/services/event_sink.py`, add `from app.services import clickhouse_store` at the top and, immediately before `return written` at the end of `persist_events`:

```python
    if written and settings_clickhouse_enabled():
        clickhouse_store.insert_events_sync(
            clickhouse_store.event_rows(
                target_id=target.id, parser_type=parser_type, account_id=account_id,
                owner_user_id=resolved_owner,
                events=[ev for ev in events if not (ev.external_id and str(ev.external_id) in known)],
            )
        )
```

and add near the imports:

```python
from app.config import get_settings


def settings_clickhouse_enabled() -> bool:
    return bool(get_settings().clickhouse_enabled)
```

Note: `known` and `resolved_owner` already exist in that function. Only events actually written to Postgres are mirrored (the filter reproduces the loop's skip condition).

- [ ] **Step 6: Add the CLI command in `app/cli.py`**

Next to the other `@cli.command()` definitions:

```python
@cli.command("ch-backfill")
@click.option("--batch-size", default=5000, type=int, show_default=True)
def ch_backfill_cmd(batch_size: int) -> None:
    """Copy every raw_events row into ClickHouse (idempotent)."""
    from app.db import SessionLocal
    from app.services.clickhouse_store import backfill_from_postgres

    with SessionLocal() as session:
        total = backfill_from_postgres(session, batch_size=batch_size)
    click.echo(f"ClickHouse backfill: {total} rows")
```

(Check how existing commands in `app/cli.py` are registered — use the same decorator object name.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" TEST_CLICKHOUSE_URL="http://default:@127.0.0.1:8123/aggregator_test" python -m pytest tests/test_clickhouse_store.py tests/test_event_sink.py -q -p no:warnings`
Expected: all PASS. If `hasAllTokens` errors on this server version, `ensure_schema_sync` must have set `TEXT_INDEX_AVAILABLE=False` — check the warning in the log; the token fallback must make `test_search_by_words_...` pass either way.

- [ ] **Step 8: Run the whole suite**

Run the full command from Global Constraints. Expected: everything green (existing 200 + new).

- [ ] **Step 9: Commit**

```bash
git add app/services/clickhouse_store.py app/services/event_sink.py app/cli.py tests/conftest.py tests/test_clickhouse_store.py
git commit -m "feat(clickhouse): events store with dual-write, backfill and search query

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Async search API `/api/v2/events`

**Files:**
- Create: `app/routers/events_v2.py`
- Modify: `app/main.py` (include router; create/close the async client in startup/shutdown; call `ensure_schema`)
- Create: `tests/test_events_v2_api.py`

**Interfaces:**
- Consumes from Task 1 (exact names): `clickhouse_store.EventFilters`, `clickhouse_store.search_events(client, filters, *, limit, cursor)`, `clickhouse_store.get_event(client, *, parser_type, target_id, event_key, owner_user_id)`, `clickhouse_store.get_async_client()`, `clickhouse_store.ensure_schema(client)`, `clickhouse_store.decode_cursor` (raises `ValueError`).
- Consumes existing: `app.deps.get_current_user`, `app.routers.api._is_admin(user)` (import it), `app.plugins.registry.plugin_registry.is_known(parser_type)`.
- Produces: `router` with `GET /api/v2/events` and `GET /api/v2/events/{parser_type}/{target_id}/{event_key}`; `get_ch_client` dependency reading `request.app.state.ch_client`.

- [ ] **Step 1: Write the failing tests `tests/test_events_v2_api.py`**

These unit-test the router with the store monkeypatched, so they run without ClickHouse; one integration test is marked `clickhouse`.

```python
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user
from app.main import app
from app.services import clickhouse_store as store


class _User:
    def __init__(self, id=7, admin=False):
        self.id, self.is_admin, self.role = id, admin, ("admin" if admin else "user")
        self.is_active = True


@pytest.fixture
def client(monkeypatch):
    calls = {}

    async def fake_search(ch, filters, *, limit, cursor):
        calls["filters"], calls["limit"], calls["cursor"] = filters, limit, cursor
        return ([{"parser_type": "telegram", "target_id": 1, "external_id": "9", "event_key": "9",
                  "observed_at": "2026-09-23T10:00:00.000+00:00", "event_kind": "message",
                  "author_id": "1", "author_label": "@a", "sender_phone": "", "text": "hi",
                  "thread_id": "", "reply_to": "", "ingested_at": "2026-09-23T10:00:01.000+00:00"}], "CURSOR")

    async def fake_get(ch, *, parser_type, target_id, event_key, owner_user_id):
        calls["get"] = (parser_type, target_id, event_key, owner_user_id)
        return {"event_key": event_key, "payload": {"x": 1}} if event_key == "9" else None

    monkeypatch.setattr(store, "search_events", fake_search)
    monkeypatch.setattr(store, "get_event", fake_get)
    app.state.ch_client = object()
    app.dependency_overrides[get_current_user] = lambda: _User()
    yield TestClient(app), calls
    app.dependency_overrides.clear()


def test_search_passes_filters_and_scopes_to_owner(client):
    c, calls = client
    r = c.get("/api/v2/events", params={"q": "курьеры", "nickname": "bob", "author_id": "1", "phone": "+38 (067)",
                                        "target_id": 3, "parser_type": "telegram", "limit": 50,
                                        "since": "2026-09-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_cursor"] == "CURSOR" and body["limit"] == 50 and body["items"][0]["external_id"] == "9"
    f = calls["filters"]
    assert f.q == "курьеры" and f.nickname == "bob" and f.author_id == "1" and f.phone == "+38 (067)"
    assert f.target_id == 3 and f.parser_type == "telegram" and f.owner_user_id == 7
    assert f.since == dt.datetime(2026, 9, 1, tzinfo=dt.UTC) and calls["limit"] == 50


def test_admin_is_not_scoped(client, monkeypatch):
    c, calls = client
    app.dependency_overrides[get_current_user] = lambda: _User(admin=True)
    assert c.get("/api/v2/events").status_code == 200
    assert calls["filters"].owner_user_id is None


def test_limit_is_clamped_and_bad_inputs_are_400(client):
    c, calls = client
    assert c.get("/api/v2/events", params={"limit": 5000}).status_code == 200 and calls["limit"] == 500
    assert c.get("/api/v2/events", params={"limit": 0}).status_code == 422
    assert c.get("/api/v2/events", params={"parser_type": "nope"}).status_code == 400
    assert c.get("/api/v2/events", params={"since": "yesterday"}).status_code == 422


def test_bad_cursor_is_400(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(store, "decode_cursor", lambda s: (_ for _ in ()).throw(ValueError("bad")))
    assert c.get("/api/v2/events", params={"cursor": "zzz"}).status_code == 400


def test_get_one_event_returns_payload_and_404(client):
    c, calls = client
    r = c.get("/api/v2/events/telegram/1/9")
    assert r.status_code == 200 and r.json()["payload"] == {"x": 1}
    assert calls["get"] == ("telegram", 1, "9", 7)
    assert c.get("/api/v2/events/telegram/1/nope").status_code == 404


def test_requires_auth():
    app.dependency_overrides.clear()
    r = TestClient(app).get("/api/v2/events")
    assert r.status_code == 401


def test_endpoint_is_async():
    import inspect
    from app.routers import events_v2
    assert inspect.iscoroutinefunction(events_v2.search_events_v2)
    assert inspect.iscoroutinefunction(events_v2.get_event_v2)


@pytest.mark.clickhouse
def test_integration_roundtrip(ch_client, monkeypatch):
    """Real ClickHouse through the real router (only the auth dependency is overridden)."""
    import asyncio
    from app.plugins.base import ParsedEvent

    rows = store.event_rows(target_id=1, parser_type="telegram", account_id=None, owner_user_id=7, events=[
        ParsedEvent(external_id="1", observed_at=dt.datetime.now(dt.UTC), payload={"sender": {"phone": "+380671234567"}},
                    text="курьеры наличных", author_id="111", author_label="@alice", event_kind="message"),
    ])
    store.insert_events_sync(rows, ch_client)

    async def make():
        import os
        from urllib.parse import urlparse
        import clickhouse_connect
        p = urlparse(os.environ["TEST_CLICKHOUSE_URL"])
        return await clickhouse_connect.get_async_client(host=p.hostname, port=p.port or 8123,
                                                          username=p.username or "default",
                                                          password=p.password or "", database=p.path.strip("/"))
    app.state.ch_client = asyncio.run(make())
    app.dependency_overrides[get_current_user] = lambda: _User(id=7)
    try:
        c = TestClient(app)
        assert [i["external_id"] for i in c.get("/api/v2/events", params={"q": "курьеры"}).json()["items"]] == ["1"]
        assert c.get("/api/v2/events", params={"phone": "380671234567"}).json()["items"][0]["author_label"] == "@alice"
        one = c.get("/api/v2/events/telegram/1/1").json()
        assert one["payload"]["sender"]["phone"] == "+380671234567"
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_events_v2_api.py -q -p no:warnings`
Expected: FAIL — 404 on `/api/v2/events` (router not mounted) / ImportError for `app.routers.events_v2`.

- [ ] **Step 3: Implement `app/routers/events_v2.py`**

```python
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.deps import get_current_user
from app.plugins.registry import plugin_registry
from app.routers.api import _is_admin
from app.services import clickhouse_store as store

router = APIRouter(prefix="/api/v2", tags=["events-v2"])

MAX_LIMIT = 500


def get_ch_client(request: Request):
    client = getattr(request.app.state, "ch_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="ClickHouse недоступний")
    return client


def _owner_scope(user) -> int | None:
    return None if _is_admin(user) else int(user.id)


@router.get("/events")
async def search_events_v2(
    q: str | None = Query(None, description="слова в тексті (усі мають бути присутні)"),
    contains: str | None = Query(None, description="підрядок у тексті без урахування регістру"),
    author_id: str | None = None,
    nickname: str | None = None,
    phone: str | None = None,
    target_id: int | None = None,
    parser_type: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(100, ge=1),
    cursor: str | None = None,
    user=Depends(get_current_user),
    ch=Depends(get_ch_client),
):
    if parser_type and not plugin_registry.is_known(parser_type):
        raise HTTPException(status_code=400, detail="Некоректний parser_type")
    if cursor:
        try:
            store.decode_cursor(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некоректний cursor")
    limit = min(int(limit), MAX_LIMIT)
    filters = store.EventFilters(
        q=q or None, contains=contains or None, author_id=author_id or None, nickname=nickname or None,
        phone=phone or None, target_id=target_id, parser_type=parser_type or None,
        owner_user_id=_owner_scope(user), since=since, until=until,
    )
    items, next_cursor = await store.search_events(ch, filters, limit=limit, cursor=cursor)
    return {"items": items, "next_cursor": next_cursor, "limit": limit}


@router.get("/events/{parser_type}/{target_id}/{event_key}")
async def get_event_v2(
    parser_type: str, target_id: int, event_key: str,
    user=Depends(get_current_user), ch=Depends(get_ch_client),
):
    row = await store.get_event(ch, parser_type=parser_type, target_id=target_id, event_key=event_key,
                                owner_user_id=_owner_scope(user))
    if row is None:
        raise HTTPException(status_code=404, detail="Подію не знайдено")
    return row
```

- [ ] **Step 4: Wire the router and the client lifecycle in `app/main.py`**

Add `from app.routers import events_v2` and `from app.services import clickhouse_store` to imports; after `app.include_router(api.router)` add `app.include_router(events_v2.router)`. Extend startup and add shutdown (keep the existing sync startup body):

```python
@app.on_event("startup")
async def startup_clickhouse() -> None:
    app.state.ch_client = None
    if not settings.clickhouse_enabled:
        return
    try:
        client = await clickhouse_store.get_async_client()
        await clickhouse_store.ensure_schema(client)
        app.state.ch_client = client
    except Exception as exc:  # noqa: BLE001 - API must still serve everything else
        import logging
        logging.getLogger(__name__).error("clickhouse unavailable at startup: %s", exc)


@app.on_event("shutdown")
async def shutdown_clickhouse() -> None:
    client = getattr(app.state, "ch_client", None)
    if client is not None:
        await client.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TEST_CLICKHOUSE_URL="http://default:@127.0.0.1:8123/aggregator_test" python -m pytest tests/test_events_v2_api.py -q -p no:warnings`
Expected: all PASS (integration test included when the env var is set).

- [ ] **Step 6: Run the whole suite**

Run the full command from Global Constraints. Expected: green.

- [ ] **Step 7: Commit**

```bash
git add app/routers/events_v2.py app/main.py tests/test_events_v2_api.py
git commit -m "feat(api): async /api/v2/events search over ClickHouse

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

- Spec coverage: schema → T1 Step 4; dual-write → T1 Step 5; backfill → T1 Step 4/6; search params, cursor, owner scoping, no total → T1 `build_search_sql` + T2; async → T2 `async def` + `test_endpoint_is_async`; text-index fallback → `ensure_schema*`; infra (compose/settings/env/requirements) → done by the controller before dispatch.
- Placeholders: none; every step has code.
- Type consistency: `EventFilters`, `search_events`, `get_event`, `decode_cursor`, `get_async_client`, `ensure_schema` names match between T1 "Produces" and T2 "Consumes".
