from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.whatsapp import map_wa_event

FIXTURE = Path(__file__).parent / "fixtures" / "wa_event_v1.json"


@pytest.fixture
def wa_message() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_maps_core_fields(wa_message):
    event = map_wa_event(wa_message)

    assert event.external_id == wa_message["message_id"]
    assert event.text == "привет всем"
    assert event.author_id == "380671234567@c.us"
    assert event.author_label == "Andrii"
    assert event.thread_id == "120363012345678901@g.us"
    assert event.event_kind == "message"


def test_payload_is_the_raw_object(wa_message):
    event = map_wa_event(wa_message)

    assert event.payload == wa_message["raw"]


def test_timestamp_becomes_utc_datetime(wa_message):
    event = map_wa_event(wa_message)

    assert event.observed_at is not None
    assert event.observed_at.tzinfo is not None
    assert event.observed_at.year == 2025


def test_quoted_message_becomes_reply(wa_message):
    wa_message["has_quoted"] = True
    wa_message["quoted_message_id"] = "true_120363@g.us_PREV"

    event = map_wa_event(wa_message)

    assert event.event_kind == "reply"
    assert event.reply_to == "true_120363@g.us_PREV"


def test_media_becomes_a_file_ref(wa_message):
    wa_message["media"] = {
        "sha256": "ab" * 32, "mime": "image/jpeg", "size": 2048, "filename": "photo.jpg",
    }

    event = map_wa_event(wa_message)

    assert len(event.files) == 1
    assert event.files[0].sha256 == "ab" * 32
    assert event.files[0].mime == "image/jpeg"


def test_author_falls_back_to_from_in_direct_chats(wa_message):
    wa_message["author"] = None
    wa_message["author_name"] = None

    event = map_wa_event(wa_message)

    assert event.author_id == "380671234567@c.us"
    assert event.author_label == "380671234567"


def test_empty_body_gives_no_text(wa_message):
    wa_message["body"] = ""

    event = map_wa_event(wa_message)

    assert event.text is None
