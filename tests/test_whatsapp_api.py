from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import ParserAccount, ServiceState, User

pytestmark = pytest.mark.postgres


def _user(session, username: str) -> User:
    user = User(username=username, password_hash="x", is_admin=False)
    session.add(user)
    session.commit()
    return user


def test_create_account_then_read_status(pg_session):
    owner = _user(pg_session, "owner")

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        client = TestClient(app)
        create_response = client.post("/api/whatsapp/accounts", json={"label": "wa-main"})
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["label"] == "wa-main"
        assert created["status"] == "pending"
        assert "перезапустіть" in created["message"].lower()

        account_id = created["id"]
        account = pg_session.get(ParserAccount, account_id)
        assert account.parser_type == "whatsapp"
        assert account.credentials.get("session_id")

        status_response = client.get(f"/api/whatsapp/accounts/{account_id}/status")
        assert status_response.status_code == 200
        assert status_response.json() == {"status": "pending", "qr_data_url": None, "updated_at": None}

        pg_session.add(
            ServiceState(
                key=f"wa_session_{account_id}",
                value={"status": "qr", "qr_data_url": "data:image/png;base64,xyz", "updated_at": "2026-09-21T00:00:00+00:00"},
            )
        )
        pg_session.commit()

        status_response = client.get(f"/api/whatsapp/accounts/{account_id}/status")
        assert status_response.status_code == 200
        body = status_response.json()
        assert body["status"] == "qr"
        assert body["qr_data_url"] == "data:image/png;base64,xyz"
    finally:
        app.dependency_overrides.clear()


def test_status_forbidden_for_other_user(pg_session):
    owner = _user(pg_session, "owner")
    other = _user(pg_session, "other")

    account = ParserAccount(
        parser_type="whatsapp",
        label="wa-owner",
        owner_user_id=owner.id,
        credentials={"session_id": "abc"},
    )
    pg_session.add(account)
    pg_session.commit()

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        client = TestClient(app)
        response = client.get(f"/api/whatsapp/accounts/{account.id}/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
