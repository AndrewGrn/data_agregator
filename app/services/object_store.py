from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import boto3
from botocore.client import Config

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


@dataclass(slots=True)
class StoredFile:
    sha256: str
    storage_key: str
    size: int
    mime: str | None
    filename: str | None


class ObjectStore:
    def __init__(self) -> None:
        self.settings = settings
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

    def put_bytes(self, data: bytes, mime: str | None = None, filename: str | None = None) -> StoredFile | None:
        """Store bytes under media/<sha256>, skipping the upload if present.

        Returns None when the object store is unavailable: the caller keeps
        the event and drops the attachment. There is no local-disk fallback.
        """
        if not self._client:
            return None

        digest = hashlib.sha256(data).hexdigest()
        key = f"media/{digest}"
        try:
            self._ensure_bucket()
            try:
                self._client.head_object(Bucket=self.settings.s3_bucket, Key=key)
            except Exception:
                self._client.put_object(
                    Bucket=self.settings.s3_bucket,
                    Key=key,
                    Body=data,
                    ContentType=mime or "application/octet-stream",
                )
        except Exception:
            # ponytail: no retry queue; add one if drops become noticeable
            logger.exception("put_bytes %s failed", key)
            return None

        return StoredFile(
            sha256=digest,
            storage_key=key,
            size=len(data),
            mime=mime,
            filename=filename,
        )

    def presigned_url(self, storage_key: str, ttl: int = 3600) -> str | None:
        if not self._client:
            return None
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.settings.s3_bucket, "Key": storage_key},
                ExpiresIn=int(ttl),
            )
        except Exception:
            logger.exception("presigned_url %s failed", storage_key)
            return None


object_store = ObjectStore()
