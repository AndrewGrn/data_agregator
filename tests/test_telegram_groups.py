from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import Target, TargetGroup, User

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(pg_session):
    admin = User(username="adm", password_hash="x", role="admin", is_admin=True, is_active=True,
                 totp_enabled=True, totp_confirmed=True)
    pg_session.add(admin)
    pg_session.commit()
    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: admin
    yield TestClient(app), pg_session, admin
    app.dependency_overrides.clear()


def _target(session, name="c1", identifier="@c1", group_id=None, owner=None):
    t = Target(parser_type="telegram", name=name, identifier=identifier, config={},
               is_active=True, group_id=group_id, owner_user_id=owner)
    session.add(t)
    session.commit()
    return t


def test_group_survives_having_no_members(client):
    """The whole point of a table: an empty category still exists and is listed."""
    c, session, _ = client

    created = c.post("/api/modules/telegram/groups", json={"name": "  Дропи  "}).json()
    assert created["name"] == "Дропи" and created["targets"] == 0

    listed = c.get("/api/modules/telegram/groups").json()["groups"]
    assert [g["name"] for g in listed] == ["Дропи"]
    assert session.get(TargetGroup, created["id"]) is not None


def test_duplicate_name_is_rejected(client):
    c, _session, _ = client
    c.post("/api/modules/telegram/groups", json={"name": "Крипта"})
    assert c.post("/api/modules/telegram/groups", json={"name": "Крипта"}).status_code == 409
    assert c.post("/api/modules/telegram/groups", json={"name": "   "}).status_code == 400


def test_rename_keeps_members_attached(client):
    """Renaming is one row now, not an UPDATE over every member."""
    c, session, _ = client
    g = c.post("/api/modules/telegram/groups", json={"name": "Крипта"}).json()
    t = _target(session, group_id=g["id"])

    renamed = c.post(f"/api/modules/telegram/groups/{g['id']}", json={"name": "Дропи"}).json()

    assert renamed["name"] == "Дропи" and renamed["targets"] == 1
    session.refresh(t)
    assert t.group_id == g["id"], "the object never moved"
    row = next(r for r in c.get("/api/modules/telegram").json()["targets"] if r["id"] == t.id)
    assert row["group_name"] == "Дропи" and row["group_id"] == g["id"]


def test_deleting_a_group_keeps_its_objects(client):
    c, session, _ = client
    g = c.post("/api/modules/telegram/groups", json={"name": "Тимчасова"}).json()
    t = _target(session, group_id=g["id"])

    resp = c.post(f"/api/modules/telegram/groups/{g['id']}/delete")

    assert resp.json() == {"ok": True, "freed": 1}
    session.expire_all()
    t = session.get(Target, t.id)
    assert t is not None and t.group_id is None and t.is_active is True
    assert session.get(TargetGroup, g["id"]) is None


def test_target_moves_between_groups_and_out(client):
    c, session, _ = client
    a = c.post("/api/modules/telegram/groups", json={"name": "A"}).json()
    b = c.post("/api/modules/telegram/groups", json={"name": "B"}).json()
    t = _target(session, group_id=a["id"])

    assert c.post(f"/api/modules/telegram/targets/{t.id}/group", json={"group_id": b["id"]}).json()["group_name"] == "B"
    assert c.post(f"/api/modules/telegram/targets/{t.id}/group", json={"group_id": None}).json()["group_name"] is None


def test_mode_applies_to_the_whole_category(client):
    """This is what a category is for: manage every channel in it at once."""
    c, session, _ = client
    g = c.post("/api/modules/telegram/groups", json={"name": "Шум"}).json()
    members = [_target(session, name=f"c{i}", identifier=f"@c{i}", group_id=g["id"]) for i in range(3)]
    outsider = _target(session, name="out", identifier="@out")

    resp = c.post(f"/api/modules/telegram/groups/{g['id']}/mode",
                  json={"backfill_enabled": False, "live_enabled": True})

    assert resp.json() == {"ok": True, "updated": 3}
    for t in members:
        session.refresh(t)
        assert t.config["backfill"]["enabled"] is False and t.config["live_enabled"] is True
    session.refresh(outsider)
    assert outsider.config == {}, "objects outside the category are untouched"


def test_onboard_can_place_the_object_in_a_group(client):
    c, session, _ = client
    g = c.post("/api/modules/telegram/groups", json={"name": "Крипта"}).json()

    row = c.post("/api/modules/telegram/onboard", json={"input": "@durov", "group_id": g["id"]}).json()
    assert row["group_id"] == g["id"] and row["group_name"] == "Крипта"

    again = c.post("/api/modules/telegram/onboard", json={"input": "@durov"}).json()
    assert again["group_id"] == g["id"], "a re-onboard must not drop the grouping"


def test_another_users_group_is_off_limits(client):
    c, session, admin = client
    other = User(username="other", password_hash="x", role="user", is_active=True)
    session.add(other)
    session.commit()
    theirs = TargetGroup(name="Чужа", owner_user_id=int(other.id))
    session.add(theirs)
    session.commit()

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=int(admin.id), is_admin=False, role="user", is_active=True
    )
    assert c.post(f"/api/modules/telegram/groups/{theirs.id}", json={"name": "Моя"}).status_code == 403
    assert c.post(f"/api/modules/telegram/groups/{theirs.id}/delete").status_code == 403
    assert c.get("/api/modules/telegram/groups").json()["groups"] == []
