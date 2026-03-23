from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import Config

from app.config import get_settings

settings = get_settings()


@dataclass(slots=True)
class StoredObject:
    storage_type: str
    payload_ref: str | None
    payload_sha256: str | None
    payload_size: int | None
    payload_preview: dict | None
    payload_inline: dict | None


def _build_preview(payload: dict) -> dict:
    keys = list(payload.keys())[:10]
    preview = {"keys": keys}
    if "message_id" in payload:
        preview["message_id"] = payload.get("message_id")
    if "date" in payload:
        preview["date"] = payload.get("date")
    if "text" in payload and payload.get("text"):
        text = str(payload.get("text"))
        preview["text"] = text[:200]
    if "url" in payload:
        preview["url"] = payload.get("url")
    return preview


class ObjectStore:
    def __init__(self) -> None:
        self.settings = settings
        self.local_dir = Path("data/raw")
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._client = None
        self._bucket_ready = False

        if self.settings.s3_enabled:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.s3_endpoint_url,
                aws_access_key_id=self.settings.s3_access_key,
                aws_secret_access_key=self.settings.s3_secret_key,
                region_name=self.settings.s3_region,
                use_ssl=self.settings.s3_use_ssl,
                config=Config(s3={"addressing_style": "path"}),
            )

    def _ensure_bucket(self) -> None:
        if not self._client or self._bucket_ready:
            return

        bucket = self.settings.s3_bucket
        try:
            self._client.head_bucket(Bucket=bucket)
        except Exception:
            self._client.create_bucket(Bucket=bucket)
        self._bucket_ready = True

    def put_json(self, parser_type: str, target_id: int, payload: dict, external_id: str | None = None) -> StoredObject:
        payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(payload_bytes).hexdigest()
        size = len(payload_bytes)
        preview = _build_preview(payload)

        if self._client:
            try:
                self._ensure_bucket()
                key = f"raw/{parser_type}/target_{target_id}/{external_id or uuid.uuid4().hex}.json"
                self._client.put_object(
                    Bucket=self.settings.s3_bucket,
                    Key=key,
                    Body=payload_bytes,
                    ContentType="application/json",
                )
                return StoredObject(
                    storage_type="s3",
                    payload_ref=f"s3://{self.settings.s3_bucket}/{key}",
                    payload_sha256=digest,
                    payload_size=size,
                    payload_preview=preview,
                    payload_inline=None,
                )
            except Exception:
                # Fallback for local/dev mode when MinIO is temporarily unavailable.
                pass

        local_path = self.local_dir / f"{parser_type}_target_{target_id}_{external_id or uuid.uuid4().hex}.json"
        local_path.write_bytes(payload_bytes)
        return StoredObject(
            storage_type="local",
            payload_ref=str(local_path),
            payload_sha256=digest,
            payload_size=size,
            payload_preview=preview,
            payload_inline=None,
        )

    def get_json(self, raw_event) -> dict | None:
        if raw_event.payload:
            return raw_event.payload

        if not raw_event.payload_ref:
            return None

        if raw_event.storage_type == "s3" and self._client:
            try:
                ref = raw_event.payload_ref.removeprefix("s3://")
                bucket, key = ref.split("/", 1)
                response = self._client.get_object(Bucket=bucket, Key=key)
                return json.loads(response["Body"].read())
            except Exception:
                return None

        if raw_event.storage_type == "local":
            path = Path(raw_event.payload_ref)
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))

        return None


object_store = ObjectStore()
