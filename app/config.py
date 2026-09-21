from __future__ import annotations

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Values that ship as defaults / in .env.example. Any of these still being in
# effect in production means the corresponding secret was never set.
_PLACEHOLDER_SECRET_KEY = "change-me"
_PLACEHOLDER_ADMIN_PASSWORD = "admin123"
_PLACEHOLDER_S3_ACCESS_KEY = "minio"
_PLACEHOLDER_S3_SECRET_KEY = "miniosecret"


class Settings(BaseSettings):
    app_name: str = "Data Aggregator"
    environment: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me"
    database_url: str = "postgresql+psycopg2://postgres:postgres@db:5432/aggregator"
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"
    worker_poll_interval: int = 3
    worker_default_max_attempts: int = 5
    worker_telegram_live_max_attempts: int = 5
    worker_telegram_backfill_max_attempts: int = 7
    worker_darknet_max_attempts: int = 5
    worker_web_max_attempts: int = 4
    worker_telegram_live_lock_minutes: int = 8
    worker_telegram_backfill_lock_minutes: int = 20
    worker_darknet_lock_minutes: int = 20
    worker_web_lock_minutes: int = 15
    worker_telegram_live_backoff_max_seconds: int = 120
    worker_telegram_backfill_backoff_max_seconds: int = 300
    worker_darknet_backoff_max_seconds: int = 900
    worker_web_backoff_max_seconds: int = 600
    telegram_parallel_jobs_per_account: int = 3
    telegram_backfill_parallel_jobs_per_account: int = 1
    darknet_parallel_jobs_per_account: int = 1
    web_parallel_jobs_per_account: int = 2
    telegram_fetch_limit: int = 200
    telegram_fetch_timeout_seconds: int = 180
    telegram_listener_refresh_seconds: int = 30
    darknet_http_timeout_seconds: int = 90
    nats_enabled: bool = True
    nats_servers: str = "nats://nats:4222"
    nats_stream_name: str = "AGG_JOBS"
    nats_subject_prefix: str = "jobs"
    nats_publish_timeout_seconds: int = 2
    nats_fetch_batch_size: int = 16
    nats_fetch_timeout_seconds: int = 2
    nats_consumer_ack_wait_seconds: int = 120
    nats_consumer_max_deliver: int = 25
    nats_dispatch_batch_size: int = 500
    nats_dispatch_min_interval_seconds: int = 10
    tor_proxy: str = "socks5://127.0.0.1:9050"
    s3_enabled: bool = False
    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "miniosecret"
    s3_bucket: str = "raw-events"
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False
    media_max_bytes: int = 52428800  # 50 MB

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


def _check_placeholder_secrets(settings: Settings) -> None:
    """Refuse to start in production with a shipped-default secret.

    In development this only warns, so the existing workflow and tests are
    unaffected; the .env.example placeholders are chosen to match these
    exact strings so a copy-pasted-but-unedited .env fails loudly instead of
    looking like it works.
    """
    offenders: list[tuple[str, str]] = []
    if settings.secret_key == _PLACEHOLDER_SECRET_KEY:
        offenders.append(("SECRET_KEY", "a long random value (see scripts/generate_secrets.py)"))
    if settings.default_admin_password == _PLACEHOLDER_ADMIN_PASSWORD:
        offenders.append(("DEFAULT_ADMIN_PASSWORD", "a strong password (see scripts/generate_secrets.py)"))
    if settings.s3_enabled and settings.s3_access_key == _PLACEHOLDER_S3_ACCESS_KEY:
        offenders.append(("S3_ACCESS_KEY", "your real MinIO/S3 access key"))
    if settings.s3_enabled and settings.s3_secret_key == _PLACEHOLDER_S3_SECRET_KEY:
        offenders.append(("S3_SECRET_KEY", "your real MinIO/S3 secret key (see scripts/generate_secrets.py)"))

    if not offenders:
        return

    names = ", ".join(name for name, _ in offenders)
    if settings.environment == "production":
        details = "\n".join(f"  - {name}: set it to {hint}" for name, hint in offenders)
        raise RuntimeError(
            "Refusing to start in production with placeholder secret(s) still set: "
            f"{names}.\n{details}"
        )
    logger.warning(
        "Using placeholder secret(s) %s — fine for development, but this must be "
        "changed before deploying to production (see scripts/generate_secrets.py).",
        names,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_settings(settings: Settings | None = None) -> Settings:
    """Entry-point hook: build (if needed) and validate settings.

    Called once from app/__init__.py so every process that imports anything
    under `app` — the API, the CLI, workers, the listener — gets the same
    check, not just the ones that happen to call it explicitly.
    """
    settings = settings or get_settings()
    _check_placeholder_secrets(settings)
    return settings
