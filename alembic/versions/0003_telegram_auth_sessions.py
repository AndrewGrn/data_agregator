"""Add telegram auth sessions table

Revision ID: 0003_telegram_auth_sessions
Revises: 0002_legacy_schema_upgrade
Create Date: 2026-03-23 11:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_telegram_auth_sessions"
down_revision: Union[str, Sequence[str], None] = "0002_legacy_schema_upgrade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "telegram_auth_sessions" in insp.get_table_names():
        return

    op.create_table(
        "telegram_auth_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("hourly_limit", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("api_id", sa.String(length=64), nullable=False),
        sa.Column("api_hash", sa.String(length=128), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=False),
        sa.Column("temp_session_string", sa.Text(), nullable=False),
        sa.Column("phone_code_hash", sa.String(length=255), nullable=False),
        sa.Column("is_completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_telegram_auth_sessions_created_at", "telegram_auth_sessions", ["created_at"], unique=False)
    op.create_index("ix_telegram_auth_sessions_expires_at", "telegram_auth_sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "telegram_auth_sessions" not in insp.get_table_names():
        return

    op.drop_index("ix_telegram_auth_sessions_expires_at", table_name="telegram_auth_sessions")
    op.drop_index("ix_telegram_auth_sessions_created_at", table_name="telegram_auth_sessions")
    op.drop_table("telegram_auth_sessions")
