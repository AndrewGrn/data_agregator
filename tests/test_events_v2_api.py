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
    import anyio.from_thread

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

    # clickhouse-connect binds the async client's aiohttp session to the loop that created it,
    # and a bare TestClient starts a fresh event loop per request. One pinned portal gives the
    # client and every request the same loop, like the single uvicorn loop in production.
    app.dependency_overrides[get_current_user] = lambda: _User(id=7)
    with anyio.from_thread.start_blocking_portal("asyncio") as portal:
        app.state.ch_client = portal.call(make)
        try:
            c = TestClient(app)
            c.portal = portal
            assert [i["external_id"] for i in c.get("/api/v2/events", params={"q": "курьеры"}).json()["items"]] == ["1"]
            assert c.get("/api/v2/events", params={"phone": "380671234567"}).json()["items"][0]["author_label"] == "@alice"
            one = c.get("/api/v2/events/telegram/1/1").json()
            assert one["payload"]["sender"]["phone"] == "+380671234567"
        finally:
            app.dependency_overrides.clear()
            portal.call(app.state.ch_client.close)
            app.state.ch_client = None
