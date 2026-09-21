from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import EventFile, RawEvent, RawEventFile, Target, User
from app.services.object_store import object_store

pytestmark = pytest.mark.postgres


def _user(session, username: str) -> User:
    user = User(username=username, password_hash="x", is_admin=False)
    session.add(user)
    session.commit()
    return user


def _seed(session):
    owner = _user(session, "owner")
    other = _user(session, "other")

    target = Target(parser_type="telegram", name="chan", identifier="@chan")
    session.add(target)
    session.commit()

    event = RawEvent(
        parser_type="telegram",
        target_id=target.id,
        owner_user_id=owner.id,
        external_id="1",
        observed_at=dt.datetime.now(dt.UTC),
        payload={"text": "hi"},
    )
    session.add(event)
    session.commit()

    file = EventFile(sha256="a" * 64, storage_key="media/aaaa", mime="image/jpeg", size=123, filename="a.jpg")
    session.add(file)
    session.commit()
    session.add(RawEventFile(raw_event_id=event.id, file_id=file.id, source_ref="1", position=0))
    session.commit()

    return owner, other, event


def test_event_files_returns_rows_for_owner(pg_session, monkeypatch):
    owner, _other, event = _seed(pg_session)
    monkeypatch.setattr(object_store, "presigned_url", lambda key, ttl=3600: f"https://minio.local/{key}")

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        client = TestClient(app)
        response = client.get(f"/api/events/{event.id}/files")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "id": body[0]["id"],
            "filename": "a.jpg",
            "mime": "image/jpeg",
            "size": 123,
            "url": "https://minio.local/media/aaaa",
        }
    ]


def test_event_files_forbidden_for_other_user(pg_session):
    _owner, other, event = _seed(pg_session)

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        client = TestClient(app)
        response = client.get(f"/api/events/{event.id}/files")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
