"""target_groups: promote the grouping from a name carried by each target to a
first-class row, so a group survives its last member leaving and can own
settings and bulk actions for a whole category.

Migrates the values written by 0020: one group row per (owner, name) pair that
any target currently uses, then repoints the targets and drops the column.

Revision ID: 0021_target_groups_table
Revises: 0020_target_group_name
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_target_groups_table"
down_revision = "0020_target_group_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "target_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_target_group_owner_name"),
    )
    op.create_index("ix_target_groups_owner_user_id", "target_groups", ["owner_user_id"])

    op.add_column("targets", sa.Column("group_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_targets_group_id", "targets", "target_groups", ["group_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_targets_group_id", "targets", ["group_id"])

    # Carry over whatever 0020 recorded. NULL owner is a distinct bucket, so it is
    # compared with IS NOT DISTINCT FROM rather than =.
    op.execute(
        """
        INSERT INTO target_groups (name, owner_user_id, position)
        SELECT DISTINCT group_name, owner_user_id, 0
        FROM targets
        WHERE group_name IS NOT NULL AND group_name <> ''
        """
    )
    op.execute(
        """
        UPDATE targets t
        SET group_id = g.id
        FROM target_groups g
        WHERE t.group_name = g.name
          AND t.owner_user_id IS NOT DISTINCT FROM g.owner_user_id
        """
    )

    op.drop_index("ix_targets_group_name", table_name="targets")
    op.drop_column("targets", "group_name")


def downgrade() -> None:
    op.add_column("targets", sa.Column("group_name", sa.String(64), nullable=True))
    op.create_index("ix_targets_group_name", "targets", ["group_name"])
    op.execute(
        """
        UPDATE targets t SET group_name = g.name
        FROM target_groups g WHERE t.group_id = g.id
        """
    )
    op.drop_index("ix_targets_group_id", table_name="targets")
    op.drop_constraint("fk_targets_group_id", "targets", type_="foreignkey")
    op.drop_column("targets", "group_id")
    op.drop_index("ix_target_groups_owner_user_id", table_name="target_groups")
    op.drop_table("target_groups")
