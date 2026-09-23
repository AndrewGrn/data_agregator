"""targets.group_name: a flat, user-defined grouping for the Telegram list.

A plain column rather than a groups table: a group here has no attributes of its
own (no colour, no order, no owner), it exists exactly as long as some target
names it, and renaming one is an UPDATE over its members.

Revision ID: 0020_target_group_name
Revises: 0019_raw_events_text_trgm
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_target_group_name"
down_revision = "0019_raw_events_text_trgm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("targets", sa.Column("group_name", sa.String(64), nullable=True))
    op.create_index("ix_targets_group_name", "targets", ["group_name"])


def downgrade() -> None:
    op.drop_index("ix_targets_group_name", table_name="targets")
    op.drop_column("targets", "group_name")
