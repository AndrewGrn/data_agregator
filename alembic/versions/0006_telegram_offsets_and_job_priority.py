"""Add telegram offsets table and parse_jobs priority

Revision ID: 0006_tg_offsets_priority
Revises: 0005_telegram_profiles
Create Date: 2026-03-24 00:05:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_tg_offsets_priority"
down_revision: Union[str, Sequence[str], None] = "0005_telegram_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "parse_jobs" in tables and not _has_column(insp, "parse_jobs", "priority"):
        op.add_column("parse_jobs", sa.Column("priority", sa.Integer(), nullable=False, server_default="100"))
        if bind.dialect.name != "sqlite":
            op.alter_column("parse_jobs", "priority", server_default=None)

    if "parse_jobs" in tables and not _has_index(insp, "parse_jobs", "ix_jobs_status_priority_run_after"):
        op.create_index(
            "ix_jobs_status_priority_run_after",
            "parse_jobs",
            ["status", "priority", "run_after"],
            unique=False,
        )

    if "telegram_offsets" not in tables:
        op.create_table(
            "telegram_offsets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("max_message_id", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_source", sa.String(length=64), nullable=True),
            sa.Column("last_gapfill_batch_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_gapfill_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_caught_up", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("target_id", "account_id", name="uq_tg_offset_target_account"),
        )
        op.create_index("ix_telegram_offsets_target_id", "telegram_offsets", ["target_id"], unique=False)
        op.create_index("ix_telegram_offsets_account_id", "telegram_offsets", ["account_id"], unique=False)
        op.create_index("ix_telegram_offsets_max_message_id", "telegram_offsets", ["max_message_id"], unique=False)
        op.create_index("ix_telegram_offsets_is_caught_up", "telegram_offsets", ["is_caught_up"], unique=False)
        op.create_index("ix_telegram_offsets_created_at", "telegram_offsets", ["created_at"], unique=False)
        op.create_index("ix_telegram_offsets_updated_at", "telegram_offsets", ["updated_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "telegram_offsets" in tables:
        if _has_index(insp, "telegram_offsets", "ix_telegram_offsets_updated_at"):
            op.drop_index("ix_telegram_offsets_updated_at", table_name="telegram_offsets")
        if _has_index(insp, "telegram_offsets", "ix_telegram_offsets_created_at"):
            op.drop_index("ix_telegram_offsets_created_at", table_name="telegram_offsets")
        if _has_index(insp, "telegram_offsets", "ix_telegram_offsets_is_caught_up"):
            op.drop_index("ix_telegram_offsets_is_caught_up", table_name="telegram_offsets")
        if _has_index(insp, "telegram_offsets", "ix_telegram_offsets_max_message_id"):
            op.drop_index("ix_telegram_offsets_max_message_id", table_name="telegram_offsets")
        if _has_index(insp, "telegram_offsets", "ix_telegram_offsets_account_id"):
            op.drop_index("ix_telegram_offsets_account_id", table_name="telegram_offsets")
        if _has_index(insp, "telegram_offsets", "ix_telegram_offsets_target_id"):
            op.drop_index("ix_telegram_offsets_target_id", table_name="telegram_offsets")
        op.drop_table("telegram_offsets")

    if "parse_jobs" in tables and _has_index(insp, "parse_jobs", "ix_jobs_status_priority_run_after"):
        op.drop_index("ix_jobs_status_priority_run_after", table_name="parse_jobs")

    if "parse_jobs" in tables and _has_column(insp, "parse_jobs", "priority"):
        op.drop_column("parse_jobs", "priority")
