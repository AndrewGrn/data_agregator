from __future__ import annotations

import asyncio

import pytest

from app.plugins import telegram as telegram_plugin
from app.plugins.base import FileRef
from app.services.object_store import StoredFile


class _FakeFile:
    def __init__(self, size=None, mime_type=None, name=None):
        self.size = size
        self.mime_type = mime_type
        self.name = name


class _FakeMsg:
    def __init__(self, media=None, file=None, data=b"", raises=False, msg_id=42):
        self.media = media
        self.file = file
        self.id = msg_id
        self._data = data
        self._raises = raises

    async def download_media(self, file=bytes):
        if self._raises:
            raise RuntimeError("boom")
        return self._data


def _run(coro):
    return asyncio.run(coro)


def test_no_media_returns_none():
    msg = _FakeMsg(media=None)
    assert _run(telegram_plugin._download_media(msg, max_bytes=100)) is None


def test_advertised_size_over_limit_skips_download(monkeypatch):
    calls = []
    msg = _FakeMsg(media=object(), file=_FakeFile(size=1000), data=b"x")

    async def _fake_download(self, file=bytes):
        calls.append(1)
        return b"x"

    monkeypatch.setattr(_FakeMsg, "download_media", _fake_download)
    assert _run(telegram_plugin._download_media(msg, max_bytes=100)) is None
    assert calls == []


def test_download_media_raises_returns_none():
    msg = _FakeMsg(media=object(), file=_FakeFile(size=10), raises=True)
    assert _run(telegram_plugin._download_media(msg, max_bytes=100)) is None


def test_downloaded_data_over_limit_without_advertised_size_returns_none():
    msg = _FakeMsg(media=object(), file=_FakeFile(size=None), data=b"x" * 200)
    assert _run(telegram_plugin._download_media(msg, max_bytes=100)) is None


def test_store_unavailable_returns_none(monkeypatch):
    msg = _FakeMsg(media=object(), file=_FakeFile(size=5, mime_type="text/plain", name="a.txt"), data=b"hello")
    monkeypatch.setattr(telegram_plugin.object_store, "put_bytes", lambda *a, **k: None)
    assert _run(telegram_plugin._download_media(msg, max_bytes=100)) is None


def test_successful_download_returns_file_ref(monkeypatch):
    msg = _FakeMsg(media=object(), file=_FakeFile(size=5, mime_type="text/plain", name="a.txt"), data=b"hello", msg_id=7)
    stored = StoredFile(sha256="abc123", storage_key="media/abc123", size=5, mime="text/plain", filename="a.txt")
    monkeypatch.setattr(telegram_plugin.object_store, "put_bytes", lambda *a, **k: stored)

    result = _run(telegram_plugin._download_media(msg, max_bytes=100))

    assert result == FileRef(source_ref="7", filename="a.txt", mime="text/plain", size=5, sha256="abc123")


def test_file_entries_round_trip():
    ref = FileRef(source_ref="1", filename="a.jpg", mime="image/jpeg", size=3, sha256="deadbeef")
    assert telegram_plugin._file_entries(ref) == [
        {"source_ref": "1", "filename": "a.jpg", "mime": "image/jpeg", "size": 3, "sha256": "deadbeef"}
    ]
    assert telegram_plugin._file_entries(None) == []


def test_media_disabled_skips_download_entirely(monkeypatch):
    """Media is opt-in per channel: max_bytes=0 means never touch the network."""
    called = []

    async def _fake_download(self, file=bytes):
        called.append(True)
        return b"x"

    monkeypatch.setattr(_FakeMsg, "download_media", _fake_download)
    msg = _FakeMsg(media=object(), file=_FakeFile(size=10), data=b"x")
    assert _run(telegram_plugin._download_media(msg, max_bytes=0)) is None
    assert called == []
