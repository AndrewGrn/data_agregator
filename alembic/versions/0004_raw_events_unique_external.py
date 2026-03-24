"""Add unique index for raw_events external ids

Revision ID: 0004_raw_events_unique_external
Revises: 0003_telegram_auth_sessions
Create Date: 2026-03-23 21:20:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_raw_events_unique_external"
down_revision: Union[str, Sequence[str], None] = "0003_telegram_auth_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in insp.get_indexes("raw_events")}

    # Keep the newest row for each external id within parser+target.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY parser_type, target_id, external_id
                    ORDER BY id DESC
                ) AS rn
            FROM raw_events
            WHERE external_id IS NOT NULL
        )
        DELETE FROM raw_events
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
        """
    )

    if "uq_raw_events_parser_target_external" not in existing_indexes:
        op.create_index(
            "uq_raw_events_parser_target_external",
            "raw_events",
            ["parser_type", "target_id", "external_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in insp.get_indexes("raw_events")}
    if "uq_raw_events_parser_target_external" in existing_indexes:
        op.drop_index("uq_raw_events_parser_target_external", table_name="raw_events")

