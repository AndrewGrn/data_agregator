"""targets.deleted_at: soft delete that hides a target from every listing
while raw_events/parse_jobs history stays queryable.

Revision ID: 0018_target_deleted_at
Revises: 0017_tg_onboarding_liveness
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_target_deleted_at"
down_revision = "0017_tg_onboarding_liveness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("targets", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_targets_deleted_at", "targets", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_targets_deleted_at", table_name="targets")
    op.drop_column("targets", "deleted_at")
