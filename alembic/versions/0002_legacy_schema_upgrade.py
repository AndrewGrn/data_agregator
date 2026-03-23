"""Legacy schema upgrade for new fields

Revision ID: 0002_legacy_schema_upgrade
Revises: 0001_initial
Create Date: 2026-03-20 18:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_legacy_schema_upgrade"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return {col["name"] for col in insp.get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return {idx["name"] for idx in insp.get_indexes(table_name)}


def upgrade() -> None:
    cols = _column_names("parser_accounts")
    if "health_score" not in cols:
        op.add_column("parser_accounts", sa.Column("health_score", sa.Float(), nullable=False, server_default="100"))
    if "hourly_limit" not in cols:
        op.add_column("parser_accounts", sa.Column("hourly_limit", sa.Integer(), nullable=False, server_default="120"))
    if "hour_window_start" not in cols:
        op.add_column("parser_accounts", sa.Column("hour_window_start", sa.DateTime(timezone=True), nullable=True))
    if "hour_window_count" not in cols:
        op.add_column("parser_accounts", sa.Column("hour_window_count", sa.Integer(), nullable=False, server_default="0"))
    if "cooldown_until" not in cols:
        op.add_column("parser_accounts", sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True))
    if "fail_count" not in cols:
        op.add_column("parser_accounts", sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"))
    if "success_count" not in cols:
        op.add_column("parser_accounts", sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"))
    if "last_error" not in cols:
        op.add_column("parser_accounts", sa.Column("last_error", sa.Text(), nullable=True))
    if "last_success_at" not in cols:
        op.add_column("parser_accounts", sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True))

    cols = _column_names("parse_jobs")
    if "job_key" not in cols:
        op.add_column("parse_jobs", sa.Column("job_key", sa.String(length=255), nullable=True))
    idx = _index_names("parse_jobs")
    if "ix_parse_jobs_job_key" not in idx:
        op.create_index("ix_parse_jobs_job_key", "parse_jobs", ["job_key"], unique=False)

    cols = _column_names("raw_events")
    if "storage_type" not in cols:
        op.add_column("raw_events", sa.Column("storage_type", sa.String(length=32), nullable=False, server_default="s3"))
    if "payload_ref" not in cols:
        op.add_column("raw_events", sa.Column("payload_ref", sa.String(length=1024), nullable=True))
    if "payload_sha256" not in cols:
        op.add_column("raw_events", sa.Column("payload_sha256", sa.String(length=64), nullable=True))
    if "payload_size" not in cols:
        op.add_column("raw_events", sa.Column("payload_size", sa.Integer(), nullable=True))
    if "payload_preview" not in cols:
        op.add_column("raw_events", sa.Column("payload_preview", sa.JSON(), nullable=True))

    bind = op.get_bind()
    dialect = bind.dialect.name
    raw_cols = {col["name"]: col for col in sa.inspect(bind).get_columns("raw_events")}
    if "payload" in raw_cols and raw_cols["payload"].get("nullable") is False and dialect != "sqlite":
        op.alter_column("raw_events", "payload", existing_type=sa.JSON(), nullable=True)


def downgrade() -> None:
    pass
