from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Target, User

pytestmark = pytest.mark.postgres


def test_dashboard_ok_with_whatsapp_target(pg_session):
    """GET /api/dashboard must not 500 once a whatsapp target exists.

    plugin_registry.list_types() includes "whatsapp", and the dashboard
    casts every registered type through ParserType(parser_name). Without
    a whatsapp member on that enum, this raises ValueError for every
    caller (telegram and darknet users too), regardless of whether they
    have any whatsapp data themselves.
    """
    admin = User(username="admin", password_hash="x", is_admin=True)
    pg_session.add(admin)
    pg_session.commit()

    pg_session.add(Target(parser_type="whatsapp", name="grp", identifier="120@g.us", is_active=True))
    pg_session.commit()

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        client = TestClient(app)
        response = client.get("/api/dashboard")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    modules = response.json()["modules"]
    assert {m["parser_type"] for m in modules} >= {"whatsapp"}

    whatsapp_module = next(m for m in modules if m["parser_type"] == "whatsapp")
    assert "Darknet" not in whatsapp_module["title"]
    assert "WhatsApp" in whatsapp_module["title"]
