"""Initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-03-20 00:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# create_type=False: the types are created once, explicitly, at the top of
# upgrade(). Without it every op.create_table() referencing an enum emits its
# own CREATE TYPE and the second table to use it fails with "already exists".
# It also suppresses the mirror-image DROP TYPE on every op.drop_table().
parser_type_enum = postgresql.ENUM(
    "telegram", "darknet", name="parsertype", create_type=False
)
job_status_enum = postgresql.ENUM(
    "pending", "running", "succeeded", "failed", "retry", name="jobstatus", create_type=False
)
onboarding_status_enum = postgresql.ENUM(
    "ready", "needs_account", "blocked", name="onboardingstatus", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    parser_type_enum.create(bind, checkfirst=True)
    job_status_enum.create(bind, checkfirst=True)
    onboarding_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "parser_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parser_type", parser_type_enum, nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("credentials", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("health_score", sa.Float(), nullable=False, server_default="100"),
        sa.Column("hourly_limit", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("hour_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hour_window_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_parser_accounts_parser_type", "parser_accounts", ["parser_type"], unique=False)
    op.create_index("ix_parser_accounts_label", "parser_accounts", ["label"], unique=True)

    op.create_table(
        "targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parser_type", parser_type_enum, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("identifier", sa.String(length=512), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("onboarding_status", onboarding_status_enum, nullable=False, server_default="ready"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_targets_parser_type", "targets", ["parser_type"], unique=False)
    op.create_index("ix_targets_identifier", "targets", ["identifier"], unique=False)

    op.create_table(
        "target_account_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auto_detected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("target_id", "account_id", name="uq_target_account"),
    )
    op.create_index("ix_target_account_links_target_id", "target_account_links", ["target_id"], unique=False)
    op.create_index("ix_target_account_links_account_id", "target_account_links", ["account_id"], unique=False)

    op.create_table(
        "parse_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parser_type", parser_type_enum, nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("job_key", sa.String(length=255), nullable=True),
        sa.Column("status", job_status_enum, nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_parse_jobs_parser_type", "parse_jobs", ["parser_type"], unique=False)
    op.create_index("ix_parse_jobs_target_id", "parse_jobs", ["target_id"], unique=False)
    op.create_index("ix_parse_jobs_status", "parse_jobs", ["status"], unique=False)
    op.create_index("ix_parse_jobs_run_after", "parse_jobs", ["run_after"], unique=False)
    op.create_index("ix_parse_jobs_job_key", "parse_jobs", ["job_key"], unique=False)
    op.create_index("ix_jobs_target_status", "parse_jobs", ["target_id", "status"], unique=False)

    op.create_table(
        "raw_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parser_type", parser_type_enum, nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("storage_type", sa.String(length=32), nullable=False, server_default="s3"),
        sa.Column("payload_ref", sa.String(length=1024), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        sa.Column("payload_size", sa.Integer(), nullable=True),
        sa.Column("payload_preview", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_raw_events_parser_type", "raw_events", ["parser_type"], unique=False)
    op.create_index("ix_raw_events_target_id", "raw_events", ["target_id"], unique=False)
    op.create_index("ix_raw_events_external_id", "raw_events", ["external_id"], unique=False)
    op.create_index("ix_raw_events_created_at", "raw_events", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_raw_events_created_at", table_name="raw_events")
    op.drop_index("ix_raw_events_external_id", table_name="raw_events")
    op.drop_index("ix_raw_events_target_id", table_name="raw_events")
    op.drop_index("ix_raw_events_parser_type", table_name="raw_events")
    op.drop_table("raw_events")

    op.drop_index("ix_jobs_target_status", table_name="parse_jobs")
    op.drop_index("ix_parse_jobs_job_key", table_name="parse_jobs")
    op.drop_index("ix_parse_jobs_run_after", table_name="parse_jobs")
    op.drop_index("ix_parse_jobs_status", table_name="parse_jobs")
    op.drop_index("ix_parse_jobs_target_id", table_name="parse_jobs")
    op.drop_index("ix_parse_jobs_parser_type", table_name="parse_jobs")
    op.drop_table("parse_jobs")

    op.drop_index("ix_target_account_links_account_id", table_name="target_account_links")
    op.drop_index("ix_target_account_links_target_id", table_name="target_account_links")
    op.drop_table("target_account_links")

    op.drop_index("ix_targets_identifier", table_name="targets")
    op.drop_index("ix_targets_parser_type", table_name="targets")
    op.drop_table("targets")

    op.drop_index("ix_parser_accounts_label", table_name="parser_accounts")
    op.drop_index("ix_parser_accounts_parser_type", table_name="parser_accounts")
    op.drop_table("parser_accounts")

    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")

    bind = op.get_bind()
    onboarding_status_enum.drop(bind, checkfirst=True)
    job_status_enum.drop(bind, checkfirst=True)
    parser_type_enum.drop(bind, checkfirst=True)
