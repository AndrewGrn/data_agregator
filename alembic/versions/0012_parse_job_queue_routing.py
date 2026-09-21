"""Add parse_jobs queue routing column

Revision ID: 0012_parse_job_queue_routing
Revises: 0011_darknet_profiles
Create Date: 2026-03-27 14:30:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012_parse_job_queue_routing"
down_revision: Union[str, Sequence[str], None] = "0011_darknet_profiles"
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

    if not _has_column(insp, "parse_jobs", "queue"):
        op.add_column("parse_jobs", sa.Column("queue", sa.String(length=64), nullable=True))

    # Backfill route for old jobs.
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE parse_jobs
                SET queue = CASE
                    WHEN parser_type::text = 'telegram'
                         AND (job_key LIKE 'backfill:%' OR job_key LIKE 'backfill-full:%') THEN 'telegram_backfill'
                    WHEN parser_type::text = 'telegram' THEN 'telegram_live'
                    WHEN parser_type::text = 'darknet' THEN 'darknet'
                    WHEN parser_type::text = 'web' THEN 'web'
                    ELSE COALESCE(NULLIF(TRIM(parser_type::text), ''), 'web')
                END
                WHERE queue IS NULL OR TRIM(queue) = ''
                """
            )
        )
    else:
        op.execute(
            sa.text(
                """
                UPDATE parse_jobs
                SET queue = CASE
                    WHEN parser_type = 'telegram'
                         AND (job_key LIKE 'backfill:%' OR job_key LIKE 'backfill-full:%') THEN 'telegram_backfill'
                    WHEN parser_type = 'telegram' THEN 'telegram_live'
                    WHEN parser_type = 'darknet' THEN 'darknet'
                    WHEN parser_type = 'web' THEN 'web'
                    ELSE COALESCE(NULLIF(TRIM(parser_type), ''), 'web')
                END
                WHERE queue IS NULL OR TRIM(queue) = ''
                """
            )
        )

    # New rows fallback.
    op.execute(sa.text("UPDATE parse_jobs SET queue = 'web' WHERE queue IS NULL OR TRIM(queue) = ''"))

    if bind.dialect.name != "sqlite":
        op.alter_column("parse_jobs", "queue", existing_type=sa.String(length=64), nullable=False, server_default="web")
        op.alter_column("parse_jobs", "queue", existing_type=sa.String(length=64), server_default=None)

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_index(insp, "parse_jobs", "ix_parse_jobs_queue"):
        op.create_index("ix_parse_jobs_queue", "parse_jobs", ["queue"], unique=False)
    if not _has_index(insp, "parse_jobs", "ix_jobs_queue_status_run_after"):
        op.create_index("ix_jobs_queue_status_run_after", "parse_jobs", ["queue", "status", "run_after"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "parse_jobs"):
        return

    if _has_index(insp, "parse_jobs", "ix_jobs_queue_status_run_after"):
        op.drop_index("ix_jobs_queue_status_run_after", table_name="parse_jobs")
    if _has_index(insp, "parse_jobs", "ix_parse_jobs_queue"):
        op.drop_index("ix_parse_jobs_queue", table_name="parse_jobs")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_column(insp, "parse_jobs", "queue"):
        op.drop_column("parse_jobs", "queue")
