from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon.errors import ChatAdminRequiredError

from app.db import Base
from app.models import ParseJob, ParserAccount, Target, TargetAccountLink
from app.plugins import telegram as tg


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _linked_target(session, **config):
    acc = ParserAccount(parser_type="telegram", label="a", credentials={"api_id": 1, "api_hash": "h", "session_string": "s"})
    target = Target(parser_type="telegram", name="c", identifier="@c", config={"live_enabled": True, **config})
    session.add_all([acc, target])
    session.commit()
    session.add(TargetAccountLink(target_id=target.id, account_id=acc.id, is_active=True))
    session.commit()
    return target, acc


# --- Gap 1: forum topics -----------------------------------------------------


def test_message_topic_id_from_reply_header():
    msg = SimpleNamespace(id=99, reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=55))
    assert tg._message_topic_id(msg) == 55


def test_message_topic_id_root_message_has_no_top_id():
    # The topic's own root message is marked forum_topic=True but carries no
    # reply_to_top_id (it isn't a reply to anything) — it is its own topic id.
    msg = SimpleNamespace(id=99, reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=None))
    assert tg._message_topic_id(msg) == 99


def test_message_topic_id_no_reply_to():
    msg = SimpleNamespace(id=99, reply_to=None)
    assert tg._message_topic_id(msg) is None


def test_normalize_topic_message_sets_thread_id():
    item = {
        "event_type": "telegram_message",
        "chat_id": 123,
        "topic_id": 55,
        "sender": {"id": 1, "username": "u"},
    }
    normalized = tg.normalize_telegram_payload(item)
    assert normalized["thread_id"] == "55"


def test_normalize_plain_message_unaffected():
    item = {
        "event_type": "telegram_message",
        "chat_id": 123,
        "sender": {"id": 1, "username": "u"},
    }
    normalized = tg.normalize_telegram_payload(item)
    assert normalized["thread_id"] == "123"


def test_normalize_comment_still_uses_root_post_id_even_with_topic_id():
    item = {
        "event_type": "telegram_comment",
        "chat_id": 123,
        "root_post_id": 10,
        "topic_id": 55,
        "sender": {"id": 1, "username": "u"},
    }
    normalized = tg.normalize_telegram_payload(item)
    assert normalized["thread_id"] == "10"


# --- Gap 2: participants ------------------------------------------------------


def test_aggressive_chosen_above_threshold():
    assert tg._use_aggressive_participants(limit=5000, threshold=2000, enabled=True) is True


def test_aggressive_not_chosen_below_threshold():
    assert tg._use_aggressive_participants(limit=500, threshold=2000, enabled=True) is False


def test_aggressive_not_chosen_when_disabled():
    assert tg._use_aggressive_participants(limit=5000, threshold=2000, enabled=False) is False


def test_chat_admin_required_marks_target_and_does_not_raise(monkeypatch):
    session = _session()
    target, account = _linked_target(session)
    job = ParseJob(
        parser_type="telegram",
        target_id=target.id,
        account_id=account.id,
        payload={"mode": "participants_sync", "identifier": "@c", "participants_limit": 100},
        queue="telegram_backfill",
    )

    async def _raise(self, account_, identifier, limit, aggressive_enabled=True, aggressive_threshold=2000):
        raise ChatAdminRequiredError(request=None)

    monkeypatch.setattr(tg.TelegramPlugin, "_fetch_participants", _raise)

    events = tg.TelegramPlugin().run(session, job, target, account)

    assert events == []
    assert target.config.get("participants_unavailable", {}).get("reason") == "chat_admin_required"


def test_participants_unavailable_stops_rescheduling():
    session = _session()
    target, _ = _linked_target(
        session,
        participants_unavailable={"reason": "chat_admin_required", "detected_at": "2026-01-01T00:00:00+00:00"},
    )

    jobs = tg.TelegramPlugin().generate_jobs(session, target)

    assert all(j.payload.get("mode") != "participants_sync" for j in jobs)
