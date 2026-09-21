from __future__ import annotations

from app.plugins.telegram import normalize_telegram_payload


def _message(**overrides) -> dict:
    item = {
        "event_type": "telegram_message",
        "message_id": 42,
        "date": "2026-01-01T10:00:00+00:00",
        "text": "привет",
        "chat_id": 12345,
        "sender": {"id": 777, "username": "alice", "first_name": "Alice", "last_name": None},
    }
    item.update(overrides)
    return item


def test_message_maps_to_normalized_fields():
    fields = normalize_telegram_payload(_message())

    assert fields["text"] == "привет"
    assert fields["author_id"] == "777"
    assert fields["author_label"] == "@alice"
    assert fields["event_kind"] == "message"
    assert fields["thread_id"] == "12345"


def test_comment_is_a_comment_kind_and_keeps_reply_target():
    fields = normalize_telegram_payload(
        _message(event_type="telegram_comment", root_post_id=10, parent_message_id=11)
    )

    assert fields["event_kind"] == "comment"
    assert fields["reply_to"] == "11"
    assert fields["thread_id"] == "10"


def test_sender_without_username_falls_back_to_full_name():
    fields = normalize_telegram_payload(
        _message(sender={"id": 777, "username": None, "first_name": "Alice", "last_name": "Smith"})
    )

    assert fields["author_label"] == "Alice Smith"


def test_sender_without_any_name_falls_back_to_id():
    fields = normalize_telegram_payload(
        _message(sender={"id": 777, "username": None, "first_name": None, "last_name": None})
    )

    assert fields["author_label"] == "ID 777"


def test_missing_sender_does_not_raise():
    fields = normalize_telegram_payload(_message(sender=None))

    assert fields["author_id"] is None
    assert fields["author_label"] is None


from app.plugins.darknet import normalize_darknet_payload


def test_forum_post_maps_title_and_body():
    fields = normalize_darknet_payload(
        {
            "event_type": "forum_post",
            "thread_url": "https://f.onion/threads/1/",
            "thread_title": "Заголовок",
            "post_id": "p1",
            "author": "alice",
            "content": "тело поста",
        }
    )

    assert fields["event_kind"] == "post"
    assert fields["author_id"] == "alice"
    assert fields["author_label"] == "alice"
    assert fields["thread_id"] == "https://f.onion/threads/1/"
    assert "Заголовок" in fields["text"]
    assert "тело поста" in fields["text"]


def test_discovery_event_has_no_text():
    fields = normalize_darknet_payload({"event_type": "darknet_discovery", "target_id": 1})

    assert fields["text"] is None
    assert fields["event_kind"] == "darknet_discovery"


def test_forum_user_event_keeps_username_as_author():
    fields = normalize_darknet_payload(
        {"event_type": "forum_user", "thread_url": "https://f.onion/t/2/", "user": {"username": "bob"}}
    )

    assert fields["author_id"] == "bob"
    assert fields["event_kind"] == "forum_user"
