"""Add dispatch audit fields for parse_jobs

Revision ID: 0013_parse_job_dispatch_audit
Revises: 0012_parse_job_queue_routing
Create Date: 2026-03-27 15:05:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0013_parse_job_dispatch_audit"
down_revision: Union[str, Sequence[str], None] = "0012_parse_job_queue_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "parse_jobs"):
        return

    if not _has_column(insp, "parse_jobs", "dispatch_attempts"):
        op.add_column("parse_jobs", sa.Column("dispatch_attempts", sa.Integer(), nullable=True))
        op.execute(sa.text("UPDATE parse_jobs SET dispatch_attempts = 0 WHERE dispatch_attempts IS NULL"))
        if bind.dialect.name != "sqlite":
            op.alter_column("parse_jobs", "dispatch_attempts", nullable=False, server_default="0")
            op.alter_column("parse_jobs", "dispatch_attempts", server_default=None)

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_column(insp, "parse_jobs", "last_dispatched_at"):
        op.add_column("parse_jobs", sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column(insp, "parse_jobs", "last_dispatch_error"):
        op.add_column("parse_jobs", sa.Column("last_dispatch_error", sa.Text(), nullable=True))

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_index(insp, "parse_jobs", "ix_parse_jobs_last_dispatched_at"):
        op.create_index("ix_parse_jobs_last_dispatched_at", "parse_jobs", ["last_dispatched_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "parse_jobs"):
        return

    if _has_index(insp, "parse_jobs", "ix_parse_jobs_last_dispatched_at"):
        op.drop_index("ix_parse_jobs_last_dispatched_at", table_name="parse_jobs")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_column(insp, "parse_jobs", "last_dispatch_error"):
        op.drop_column("parse_jobs", "last_dispatch_error")
    if _has_column(insp, "parse_jobs", "last_dispatched_at"):
        op.drop_column("parse_jobs", "last_dispatched_at")
    if _has_column(insp, "parse_jobs", "dispatch_attempts"):
        op.drop_column("parse_jobs", "dispatch_attempts")
