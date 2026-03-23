from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class ParserAccount(Base):
    __tablename__ = "parser_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_type: Mapped[ParserType] = mapped_column(Enum(ParserType), index=True)
    label: Mapped[str] = mapped_column(String(128), unique=True)
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
    job_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.pending, index=True)
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


Index("ix_jobs_target_status", ParseJob.target_id, ParseJob.status)
