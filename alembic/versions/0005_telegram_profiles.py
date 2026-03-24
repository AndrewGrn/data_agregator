"""Add telegram profiles and memberships tables

Revision ID: 0005_telegram_profiles
Revises: 0004_raw_events_unique_external
Create Date: 2026-03-23 22:05:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_telegram_profiles"
down_revision: Union[str, Sequence[str], None] = "0004_raw_events_unique_external"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "telegram_users" not in tables:
        op.create_table(
            "telegram_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.String(length=64), nullable=True),
            sa.Column("first_name", sa.String(length=128), nullable=True),
            sa.Column("last_name", sa.String(length=128), nullable=True),
            sa.Column("phone", sa.String(length=64), nullable=True),
            sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_scam", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_fake", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("telegram_user_id", name="uq_telegram_users_tg_id"),
        )
        op.create_index("ix_telegram_users_telegram_user_id", "telegram_users", ["telegram_user_id"], unique=True)
        op.create_index("ix_telegram_users_created_at", "telegram_users", ["created_at"], unique=False)

    if "telegram_memberships" not in tables:
        op.create_table(
            "telegram_memberships",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True),
            sa.Column("telegram_user_ref_id", sa.Integer(), sa.ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("membership_status", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_source", sa.String(length=64), nullable=True),
            sa.Column("last_raw", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("target_id", "account_id", "telegram_user_ref_id", name="uq_tg_membership"),
        )
        op.create_index("ix_telegram_memberships_target_id", "telegram_memberships", ["target_id"], unique=False)
        op.create_index("ix_telegram_memberships_account_id", "telegram_memberships", ["account_id"], unique=False)
        op.create_index("ix_telegram_memberships_telegram_user_ref_id", "telegram_memberships", ["telegram_user_ref_id"], unique=False)
        op.create_index("ix_telegram_memberships_membership_status", "telegram_memberships", ["membership_status"], unique=False)
        op.create_index("ix_telegram_memberships_is_active", "telegram_memberships", ["is_active"], unique=False)
        op.create_index("ix_telegram_memberships_created_at", "telegram_memberships", ["created_at"], unique=False)

    if "telegram_membership_history" not in tables:
        op.create_table(
            "telegram_membership_history",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("membership_id", sa.Integer(), sa.ForeignKey("telegram_memberships.id", ondelete="CASCADE"), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("membership_status", sa.String(length=64), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("source", sa.String(length=64), nullable=True),
            sa.Column("snapshot", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_telegram_membership_history_membership_id", "telegram_membership_history", ["membership_id"], unique=False)
        op.create_index("ix_telegram_membership_history_observed_at", "telegram_membership_history", ["observed_at"], unique=False)
        op.create_index("ix_telegram_membership_history_created_at", "telegram_membership_history", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "telegram_membership_history" in tables:
        op.drop_index("ix_telegram_membership_history_created_at", table_name="telegram_membership_history")
        op.drop_index("ix_telegram_membership_history_observed_at", table_name="telegram_membership_history")
        op.drop_index("ix_telegram_membership_history_membership_id", table_name="telegram_membership_history")
        op.drop_table("telegram_membership_history")

    if "telegram_memberships" in tables:
        op.drop_index("ix_telegram_memberships_created_at", table_name="telegram_memberships")
        op.drop_index("ix_telegram_memberships_is_active", table_name="telegram_memberships")
        op.drop_index("ix_telegram_memberships_membership_status", table_name="telegram_memberships")
        op.drop_index("ix_telegram_memberships_telegram_user_ref_id", table_name="telegram_memberships")
        op.drop_index("ix_telegram_memberships_account_id", table_name="telegram_memberships")
        op.drop_index("ix_telegram_memberships_target_id", table_name="telegram_memberships")
        op.drop_table("telegram_memberships")

    if "telegram_users" in tables:
        op.drop_index("ix_telegram_users_created_at", table_name="telegram_users")
        op.drop_index("ix_telegram_users_telegram_user_id", table_name="telegram_users")
        op.drop_table("telegram_users")

