from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Data Aggregator"
    secret_key: str = "change-me"
    database_url: str = "postgresql+psycopg2://postgres:postgres@db:5432/aggregator"
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"
    worker_poll_interval: int = 3
    telegram_parallel_jobs_per_account: int = 3
    telegram_backfill_parallel_jobs_per_account: int = 1
    telegram_fetch_limit: int = 200
    telegram_fetch_timeout_seconds: int = 180
    telegram_listener_refresh_seconds: int = 30
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
