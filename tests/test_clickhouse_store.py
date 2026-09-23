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


def test_backfill_splits_inserts_by_month(ch_client):
    """Regression: the table is PARTITION BY toYYYYMM(observed_at) and ClickHouse rejects an
    insert block touching more than 100 partitions. An id-ordered batch of a channel with a
    decade of history (e.g. @durov, back to 2015) blows straight through that."""
    from app.db import Base
    from app.models import RawEvent, Target
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    target = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(target); session.commit()

    base = dt.datetime(2015, 1, 15, tzinfo=dt.UTC)
    session.add_all([
        RawEvent(parser_type="telegram", target_id=target.id, external_id=str(i), payload={},
                 text=f"msg {i}", observed_at=base + dt.timedelta(days=31 * i), event_kind="message")
        for i in range(130)  # 130 distinct months, well over the 100-partition limit
    ])
    session.commit()

    assert store.backfill_from_postgres(session, batch_size=5000, client=ch_client) == 130
    assert int(ch_client.command(f"SELECT count() FROM {store.EVENTS_TABLE} FINAL")) == 130
    assert int(ch_client.command(f"SELECT uniq(toYYYYMM(observed_at)) FROM {store.EVENTS_TABLE}")) == 130
    session.close()
