from __future__ import annotations

import datetime as dt

from app.plugins.base import FileRef, ParsedEvent
from app.plugins.registry import plugin_registry


def test_parsed_event_defaults():
    event = ParsedEvent(external_id="x1", observed_at=None, payload={"a": 1})

    assert event.event_kind == "message"
    assert event.text is None
    assert event.files == []


def test_parsed_event_files_are_not_shared_between_instances():
    first = ParsedEvent(external_id="a", observed_at=None, payload={})
    second = ParsedEvent(external_id="b", observed_at=None, payload={})
    first.files.append(FileRef(source_ref="f1"))

    assert second.files == []


def test_parsed_event_accepts_normalized_fields():
    now = dt.datetime.now(dt.UTC)
    event = ParsedEvent(
        external_id="m1",
        observed_at=now,
        payload={"raw": True},
        text="привет",
        author_id="380671234567@c.us",
        author_label="Andrii",
        event_kind="reply",
        thread_id="120363@g.us",
        reply_to="m0",
        files=[FileRef(source_ref="m1", mime="image/jpeg", size=100, sha256="ab")],
    )

    assert event.author_id == "380671234567@c.us"
    assert event.files[0].sha256 == "ab"


def test_registry_knows_registered_types():
    assert plugin_registry.is_known("telegram") is True
    assert plugin_registry.is_known("nonexistent") is False
