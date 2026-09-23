"""api_tokens: bearer tokens for machine access, so an integration does not need
to hold a browser session with a one-time 2FA code.

Only the SHA-256 of a token is stored; the raw value exists once, in the
response that creates it.

Revision ID: 0022_api_tokens
Revises: 0021_target_groups_table
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_api_tokens"
down_revision = "0021_target_groups_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)
    op.create_index("ix_api_tokens_prefix", "api_tokens", ["prefix"])
    op.create_index("ix_api_tokens_owner_user_id", "api_tokens", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_owner_user_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_prefix", table_name="api_tokens")
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_table("api_tokens")
