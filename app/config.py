from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Data Aggregator"
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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
