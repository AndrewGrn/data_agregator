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
        idx_at, idx_key = list(res.column_names).index("observed_at"), list(res.column_names).index("event_key")
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


def _group_by_month(rows: list[dict]) -> dict[str, list[dict]]:
    """Split rows by the table's partition key so no insert block spans >100 partitions."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["observed_at"].strftime("%Y%m"), []).append(row)
    return groups


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
        # One insert per month, because the table is PARTITION BY toYYYYMM(observed_at) and
        # ClickHouse refuses a block touching more than max_partitions_per_insert_block (100)
        # partitions. Rows arrive in id order, so a single batch of a channel with a decade of
        # history spans well over 100 months — @durov alone reaches back to 2015.
        for _, group in sorted(_group_by_month(rows).items()):
            client.insert(EVENTS_TABLE, [[row[c] for c in _COLUMNS] for row in group], column_names=_COLUMNS)
        total += len(rows); last_id = int(batch[-1].id)
        logger.info("ch-backfill: %d rows so far (last id %d)", total, last_id)
    return total
