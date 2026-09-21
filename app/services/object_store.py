from __future__ import annotations

import boto3
from botocore.client import Config

from app.config import get_settings

settings = get_settings()


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


object_store = ObjectStore()
