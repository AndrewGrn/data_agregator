"""Add service_state table for OpenSearch autosync cursor

Revision ID: 0009_service_state_autosync
Revises: 0008_user_profile_sessions
Create Date: 2026-03-26 17:15:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_service_state_autosync"
down_revision: Union[str, Sequence[str], None] = "0008_user_profile_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_table(insp, "service_state"):
        op.create_table(
            "service_state",
            sa.Column("key", sa.String(length=128), primary_key=True, nullable=False),
            sa.Column("value", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        if bind.dialect.name != "sqlite":
            op.alter_column("service_state", "value", server_default=None)
            op.alter_column("service_state", "updated_at", server_default=None)
        return

    if not _has_column(insp, "service_state", "value"):
        op.add_column("service_state", sa.Column("value", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        if bind.dialect.name != "sqlite":
            op.alter_column("service_state", "value", server_default=None)

    if not _has_column(insp, "service_state", "updated_at"):
        op.add_column(
            "service_state",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        if bind.dialect.name != "sqlite":
            op.alter_column("service_state", "updated_at", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "service_state"):
        op.drop_table("service_state")
