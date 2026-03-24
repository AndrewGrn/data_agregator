from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    BigInteger,
    Float,
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ParserType(str, enum.Enum):
    telegram = "telegram"
    darknet = "darknet"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    retry = "retry"


class OnboardingStatus(str, enum.Enum):
    ready = "ready"
    needs_account = "needs_account"
    blocked = "blocked"


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.user, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("registration_tokens.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class RegistrationToken(Base):
    __tablename__ = "registration_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    used_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)


class ParserAccount(Base):
    __tablename__ = "parser_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_type: Mapped[ParserType] = mapped_column(Enum(ParserType), index=True)
    label: Mapped[str] = mapped_column(String(128), unique=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    credentials: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    health_score: Mapped[float] = mapped_column(Float, default=100.0)
    hourly_limit: Mapped[int] = mapped_column(Integer, default=120)
    hour_window_start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hour_window_count: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    targets: Mapped[list[TargetAccountLink]] = relationship(back_populates="account")


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_type: Mapped[ParserType] = mapped_column(Enum(ParserType), index=True)
    name: Mapped[str] = mapped_column(String(128))
    identifier: Mapped[str] = mapped_column(String(512), index=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    onboarding_status: Mapped[OnboardingStatus] = mapped_column(Enum(OnboardingStatus), default=OnboardingStatus.ready)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )

    account_links: Mapped[list[TargetAccountLink]] = relationship(back_populates="target", cascade="all, delete-orphan")
    jobs: Mapped[list[ParseJob]] = relationship(back_populates="target", cascade="all, delete-orphan")
    events: Mapped[list[RawEvent]] = relationship(back_populates="target", cascade="all, delete-orphan")


class TargetAccountLink(Base):
    __tablename__ = "target_account_links"
    __table_args__ = (UniqueConstraint("target_id", "account_id", name="uq_target_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("parser_accounts.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    target: Mapped[Target] = relationship(back_populates="account_links")
    account: Mapped[ParserAccount] = relationship(back_populates="targets")


class ParseJob(Base):
    __tablename__ = "parse_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_type: Mapped[ParserType] = mapped_column(Enum(ParserType), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    job_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.pending, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lock_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    target: Mapped[Target] = relationship(back_populates="jobs")


class RawEvent(Base):
    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_type: Mapped[ParserType] = mapped_column(Enum(ParserType), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    storage_type: Mapped[str] = mapped_column(String(32), default="s3")
    payload_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_preview: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)

    target: Mapped[Target] = relationship(back_populates="events")


class TelegramUser(Base):
    __tablename__ = "telegram_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_scam: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fake: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )


class TelegramMembership(Base):
    __tablename__ = "telegram_memberships"
    __table_args__ = (UniqueConstraint("target_id", "account_id", "telegram_user_ref_id", name="uq_tg_membership"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    telegram_user_ref_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    membership_status: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )


class TelegramMembershipHistory(Base):
    __tablename__ = "telegram_membership_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    membership_id: Mapped[int] = mapped_column(ForeignKey("telegram_memberships.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)
    membership_status: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)


class TelegramAuthSession(Base):
    __tablename__ = "telegram_auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    hourly_limit: Mapped[int] = mapped_column(Integer, default=120)
    api_id: Mapped[str] = mapped_column(String(64))
    api_hash: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(64))
    temp_session_string: Mapped[str] = mapped_column(Text)
    phone_code_hash: Mapped[str] = mapped_column(String(255))
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)


class TelegramOffset(Base):
    __tablename__ = "telegram_offsets"
    __table_args__ = (UniqueConstraint("target_id", "account_id", name="uq_tg_offset_target_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("parser_accounts.id", ondelete="CASCADE"), index=True)
    max_message_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    last_event_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_gapfill_batch_count: Mapped[int] = mapped_column(Integer, default=0)
    last_gapfill_limit: Mapped[int] = mapped_column(Integer, default=0)
    is_caught_up: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC), index=True
    )


Index("ix_jobs_target_status", ParseJob.target_id, ParseJob.status)
Index("ix_jobs_status_priority_run_after", ParseJob.status, ParseJob.priority, ParseJob.run_after)
Index("uq_raw_events_parser_target_external", RawEvent.parser_type, RawEvent.target_id, RawEvent.external_id, unique=True)
