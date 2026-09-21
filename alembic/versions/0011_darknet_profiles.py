"""Add darknet users and memberships

Revision ID: 0011_darknet_profiles
Revises: 0010_darknet_auth_tokens
Create Date: 2026-03-27 13:10:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011_darknet_profiles"
down_revision: Union[str, Sequence[str], None] = "0010_darknet_auth_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_table(insp, "darknet_users"):
        op.create_table(
            "darknet_users",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("forum_host", sa.String(length=255), nullable=False),
            sa.Column("username", sa.String(length=255), nullable=False),
            sa.Column("username_normalized", sa.String(length=255), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=True),
            sa.Column("raw", sa.JSON(), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("forum_host", "username_normalized", name="uq_darknet_user_host_username"),
        )
        if bind.dialect.name != "sqlite":
            op.alter_column("darknet_users", "first_seen_at", server_default=None)
            op.alter_column("darknet_users", "created_at", server_default=None)
            op.alter_column("darknet_users", "updated_at", server_default=None)

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "darknet_users"):
        if not _has_index(insp, "darknet_users", "ix_darknet_users_forum_host"):
            op.create_index("ix_darknet_users_forum_host", "darknet_users", ["forum_host"], unique=False)
        if not _has_index(insp, "darknet_users", "ix_darknet_users_username_normalized"):
            op.create_index("ix_darknet_users_username_normalized", "darknet_users", ["username_normalized"], unique=False)
        if not _has_index(insp, "darknet_users", "ix_darknet_users_first_seen_at"):
            op.create_index("ix_darknet_users_first_seen_at", "darknet_users", ["first_seen_at"], unique=False)
        if not _has_index(insp, "darknet_users", "ix_darknet_users_last_seen_at"):
            op.create_index("ix_darknet_users_last_seen_at", "darknet_users", ["last_seen_at"], unique=False)
        if not _has_index(insp, "darknet_users", "ix_darknet_users_created_at"):
            op.create_index("ix_darknet_users_created_at", "darknet_users", ["created_at"], unique=False)
        if not _has_index(insp, "darknet_users", "ix_darknet_users_updated_at"):
            op.create_index("ix_darknet_users_updated_at", "darknet_users", ["updated_at"], unique=False)
        if not _has_index(insp, "darknet_users", "ix_darknet_users_host_last_seen"):
            op.create_index("ix_darknet_users_host_last_seen", "darknet_users", ["forum_host", "last_seen_at"], unique=False)

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "darknet_user_memberships"):
        op.create_table(
            "darknet_user_memberships",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True),
            sa.Column("darknet_user_ref_id", sa.Integer(), sa.ForeignKey("darknet_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("thread_url", sa.String(length=1024), nullable=False),
            sa.Column("thread_title", sa.String(length=512), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("posts_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_source", sa.String(length=64), nullable=True),
            sa.Column("last_raw", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("target_id", "darknet_user_ref_id", "thread_url", name="uq_darknet_membership_target_user_thread"),
        )
        if bind.dialect.name != "sqlite":
            op.alter_column("darknet_user_memberships", "is_active", server_default=None)
            op.alter_column("darknet_user_memberships", "posts_count", server_default=None)
            op.alter_column("darknet_user_memberships", "first_seen_at", server_default=None)
            op.alter_column("darknet_user_memberships", "created_at", server_default=None)
            op.alter_column("darknet_user_memberships", "updated_at", server_default=None)

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "darknet_user_memberships"):
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_target_id"):
            op.create_index("ix_darknet_user_memberships_target_id", "darknet_user_memberships", ["target_id"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_account_id"):
            op.create_index("ix_darknet_user_memberships_account_id", "darknet_user_memberships", ["account_id"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_darknet_user_ref_id"):
            op.create_index("ix_darknet_user_memberships_darknet_user_ref_id", "darknet_user_memberships", ["darknet_user_ref_id"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_thread_url"):
            op.create_index("ix_darknet_user_memberships_thread_url", "darknet_user_memberships", ["thread_url"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_is_active"):
            op.create_index("ix_darknet_user_memberships_is_active", "darknet_user_memberships", ["is_active"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_first_seen_at"):
            op.create_index("ix_darknet_user_memberships_first_seen_at", "darknet_user_memberships", ["first_seen_at"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_last_seen_at"):
            op.create_index("ix_darknet_user_memberships_last_seen_at", "darknet_user_memberships", ["last_seen_at"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_created_at"):
            op.create_index("ix_darknet_user_memberships_created_at", "darknet_user_memberships", ["created_at"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_user_memberships_updated_at"):
            op.create_index("ix_darknet_user_memberships_updated_at", "darknet_user_memberships", ["updated_at"], unique=False)
        if not _has_index(insp, "darknet_user_memberships", "ix_darknet_memberships_target_last_seen"):
            op.create_index(
                "ix_darknet_memberships_target_last_seen",
                "darknet_user_memberships",
                ["target_id", "last_seen_at"],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "darknet_user_memberships"):
        op.drop_table("darknet_user_memberships")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "darknet_users"):
        op.drop_table("darknet_users")
