"""Add darknet auth tokens for local browser helper

Revision ID: 0010_darknet_auth_tokens
Revises: 0009_service_state_autosync
Create Date: 2026-03-27 10:30:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010_darknet_auth_tokens"
down_revision: Union[str, Sequence[str], None] = "0009_service_state_autosync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_table(insp, "darknet_auth_tokens"):
        op.create_table(
            "darknet_auth_tokens",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_from_ip", sa.String(length=128), nullable=True),
            sa.Column("used_from_ip", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("token_hash", name="uq_darknet_auth_tokens_token_hash"),
        )
        if bind.dialect.name != "sqlite":
            op.alter_column("darknet_auth_tokens", "created_at", server_default=None)

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "darknet_auth_tokens"):
        if not _has_index(insp, "darknet_auth_tokens", "ix_darknet_auth_tokens_account_id"):
            op.create_index("ix_darknet_auth_tokens_account_id", "darknet_auth_tokens", ["account_id"], unique=False)
        if not _has_index(insp, "darknet_auth_tokens", "ix_darknet_auth_tokens_owner_user_id"):
            op.create_index("ix_darknet_auth_tokens_owner_user_id", "darknet_auth_tokens", ["owner_user_id"], unique=False)
        if not _has_index(insp, "darknet_auth_tokens", "ix_darknet_auth_tokens_token_hash"):
            op.create_index("ix_darknet_auth_tokens_token_hash", "darknet_auth_tokens", ["token_hash"], unique=True)
        if not _has_index(insp, "darknet_auth_tokens", "ix_darknet_auth_tokens_expires_at"):
            op.create_index("ix_darknet_auth_tokens_expires_at", "darknet_auth_tokens", ["expires_at"], unique=False)
        if not _has_index(insp, "darknet_auth_tokens", "ix_darknet_auth_tokens_used_at"):
            op.create_index("ix_darknet_auth_tokens_used_at", "darknet_auth_tokens", ["used_at"], unique=False)
        if not _has_index(insp, "darknet_auth_tokens", "ix_darknet_auth_tokens_created_at"):
            op.create_index("ix_darknet_auth_tokens_created_at", "darknet_auth_tokens", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "darknet_auth_tokens"):
        op.drop_table("darknet_auth_tokens")
