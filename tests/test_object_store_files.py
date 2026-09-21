from __future__ import annotations

import hashlib

from app.services.object_store import ObjectStore


class _FakeClient:
    """Minimal stand-in for the boto3 S3 client."""

    def __init__(self, existing: set[str] | None = None):
        self.existing = existing or set()
        self.put_calls: list[tuple[str, bytes]] = []

    def head_bucket(self, Bucket):
        return {}

    def head_object(self, Bucket, Key):
        if Key not in self.existing:
            raise Exception("404 not found")
        return {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.put_calls.append((Key, Body))
        self.existing.add(Key)
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://minio.local/{Params['Key']}?exp={ExpiresIn}"


def _store(client) -> ObjectStore:
    store = ObjectStore()
    store._client = client
    store._bucket_ready = True
    return store


def test_put_bytes_uses_sha256_key():
    client = _FakeClient()
    store = _store(client)
    data = b"hello world"

    stored = store.put_bytes(data, mime="text/plain", filename="a.txt")

    digest = hashlib.sha256(data).hexdigest()
    assert stored.sha256 == digest
    assert stored.storage_key == f"media/{digest}"
    assert stored.size == 11
    assert client.put_calls[0][0] == f"media/{digest}"


def test_existing_object_is_not_uploaded_again():
    data = b"hello world"
    digest = hashlib.sha256(data).hexdigest()
    client = _FakeClient(existing={f"media/{digest}"})
    store = _store(client)

    stored = store.put_bytes(data)

    assert stored.storage_key == f"media/{digest}"
    assert client.put_calls == []


def test_put_bytes_returns_none_without_client():
    store = ObjectStore()
    store._client = None

    assert store.put_bytes(b"data") is None


def test_presigned_url_is_built():
    client = _FakeClient()
    store = _store(client)

    url = store.presigned_url("media/abc", ttl=60)

    assert url == "https://minio.local/media/abc?exp=60"
