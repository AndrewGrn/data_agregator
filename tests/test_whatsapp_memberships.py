from __future__ import annotations

import pytest

from app.models import ParserAccount, Target, TargetAccountLink
from app.services import whatsapp_bus

pytestmark = pytest.mark.postgres


class _FakeConnection:
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.closed = False

    async def request(self, subject, data, timeout):
        if self._error is not None:
            raise self._error
        import json

        class _Msg:
            pass

        msg = _Msg()
        msg.data = json.dumps(self._response).encode("utf-8")
        return msg

    async def close(self):
        self.closed = True


# --- app.services.whatsapp_bus.request_participants (stubbed NATS connection) ---


def test_request_participants_returns_list_on_success(monkeypatch):
    fake = _FakeConnection(response={"participants": [{"id": "a@c.us", "is_admin": False}]})

    async def fake_connect():
        return fake

    monkeypatch.setattr(whatsapp_bus, "_connect", fake_connect)

    members = whatsapp_bus.request_participants(account_id=1, chat_id="g@g.us")

    assert members == [{"id": "a@c.us", "is_admin": False}]
    assert fake.closed


def test_request_participants_times_out_to_empty_list(monkeypatch):
    fake = _FakeConnection(error=TimeoutError("no reply"))

    async def fake_connect():
        return fake

    monkeypatch.setattr(whatsapp_bus, "_connect", fake_connect)

    members = whatsapp_bus.request_participants(account_id=1, chat_id="g@g.us")

    assert members == []


def test_request_participants_without_account_skips_the_call(monkeypatch):
    def fail_connect():
        raise AssertionError("should not connect without an account")

    monkeypatch.setattr(whatsapp_bus, "_connect", fail_connect)

    assert whatsapp_bus.request_participants(account_id=None, chat_id="g@g.us") == []


# --- app.plugins.whatsapp.WhatsAppPlugin.sync_memberships (stubbed bus) ---


def _account(session) -> ParserAccount:
    account = ParserAccount(parser_type="whatsapp", label="wa-main", credentials={"session_id": "s1"})
    session.add(account)
    session.commit()
    return account


def test_sync_memberships_counts_a_group_target(pg_session, monkeypatch):
    from app.plugins.whatsapp import WhatsAppPlugin

    account = _account(pg_session)
    target = Target(parser_type="whatsapp", name="grp", identifier="120@g.us", is_active=True)
    pg_session.add(target)
    pg_session.commit()
    pg_session.add(TargetAccountLink(target_id=target.id, account_id=account.id))
    pg_session.commit()

    monkeypatch.setattr(
        "app.services.whatsapp_bus.request_participants",
        lambda account_id, chat_id: [{"id": "a@c.us", "is_admin": False}, {"id": "b@c.us", "is_admin": True}],
    )

    result = WhatsAppPlugin().sync_memberships(pg_session)

    assert result == {"checked": 1, "linked": 2}


def test_sync_memberships_skips_direct_chats(pg_session, monkeypatch):
    from app.plugins.whatsapp import WhatsAppPlugin

    target = Target(parser_type="whatsapp", name="dm", identifier="380671234567@c.us", is_active=True)
    pg_session.add(target)
    pg_session.commit()

    def fail_request(**kwargs):
        raise AssertionError("direct chats must never be asked for participants")

    monkeypatch.setattr("app.services.whatsapp_bus.request_participants", fail_request)

    result = WhatsAppPlugin().sync_memberships(pg_session)

    assert result == {"checked": 0, "linked": 0}


def test_sync_memberships_skips_group_without_linked_account(pg_session, monkeypatch):
    from app.plugins.whatsapp import WhatsAppPlugin

    target = Target(parser_type="whatsapp", name="grp", identifier="120@g.us", is_active=True)
    pg_session.add(target)
    pg_session.commit()

    def fail_request(**kwargs):
        raise AssertionError("must not request participants without a linked account")

    monkeypatch.setattr("app.services.whatsapp_bus.request_participants", fail_request)

    result = WhatsAppPlugin().sync_memberships(pg_session)

    assert result == {"checked": 0, "linked": 0}
