"""Add user profile fields and session version

Revision ID: 0008_user_profile_sessions
Revises: 0007_auth_rbac_ownership
Create Date: 2026-03-25 12:25:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_user_profile_sessions"
down_revision: Union[str, Sequence[str], None] = "0007_auth_rbac_ownership"
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
    if not _has_table(insp, "users"):
        return

    if not _has_column(insp, "users", "full_name"):
        op.add_column("users", sa.Column("full_name", sa.String(length=128), nullable=True))

    if not _has_column(insp, "users", "email"):
        op.add_column("users", sa.Column("email", sa.String(length=255), nullable=True))

    if not _has_column(insp, "users", "session_version"):
        op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"))
        if bind.dialect.name != "sqlite":
            op.alter_column("users", "session_version", server_default=None)

    bind.execute(sa.text("UPDATE users SET session_version = 1 WHERE session_version IS NULL"))

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_index(insp, "users", "ix_users_email"):
        op.create_index("ix_users_email", "users", ["email"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "users"):
        return

    if _has_index(insp, "users", "ix_users_email"):
        op.drop_index("ix_users_email", table_name="users")

    if _has_column(insp, "users", "session_version"):
        op.drop_column("users", "session_version")

    if _has_column(insp, "users", "email"):
        op.drop_column("users", "email")

    if _has_column(insp, "users", "full_name"):
        op.drop_column("users", "full_name")
