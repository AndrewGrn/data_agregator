"""telegram onboarding + liveness: account pool/alive/join-window columns,
target onboarding step, one active link per target

Revision ID: 0017_tg_onboarding_liveness
Revises: 0016_search_trigram_indexes

Note: the revision id must fit alembic_version.version_num (VARCHAR(32));
the brief's proposed id "0017_telegram_onboarding_and_liveness" is 37 chars
and overflows that column (confirmed against a scratch DB: the upgrade
failed with psycopg2.errors.StringDataRightTruncation), so it is shortened
here to "0017_tg_onboarding_liveness" (27 chars), matching this codebase's
existing "tg_" abbreviation convention (see uq_tg_membership, TelegramOffset's
uq_tg_offset_target_account).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_tg_onboarding_liveness"
down_revision = "0016_search_trigram_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("parser_accounts", sa.Column("pool_mode", sa.String(16), nullable=False, server_default="shared"))
    op.add_column("parser_accounts", sa.Column("alive", sa.Boolean(), nullable=True))
    op.add_column("parser_accounts", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("parser_accounts", sa.Column("last_alive_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("parser_accounts", sa.Column("dead_reason", sa.Text(), nullable=True))
    op.add_column("parser_accounts", sa.Column("join_window_start", sa.DateTime(timezone=True), nullable=True))
    op.add_column("parser_accounts", sa.Column("join_window_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("parser_accounts", sa.Column("last_join_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_parser_accounts_pool_mode", "parser_accounts", ["pool_mode"])

    op.add_column("targets", sa.Column("onboarding_step", sa.String(32), nullable=False, server_default="idle"))
    op.add_column("targets", sa.Column("onboarding_error", sa.Text(), nullable=True))
    op.create_index("ix_targets_onboarding_step", "targets", ["onboarding_step"])

    # One active link per target. Keep the link whose account is healthiest,
    # deactivate the rest, then enforce with a partial unique index.
    op.execute(
        """
        WITH ranked AS (
            SELECT l.id,
                   ROW_NUMBER() OVER (
                       PARTITION BY l.target_id
                       ORDER BY a.health_score DESC, l.id ASC
                   ) AS rn
            FROM target_account_links l
            JOIN parser_accounts a ON a.id = l.account_id
            WHERE l.is_active
        )
        UPDATE target_account_links SET is_active = false
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tal_one_active ON target_account_links (target_id) WHERE is_active"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tal_one_active")
    op.drop_index("ix_targets_onboarding_step", table_name="targets")
    op.drop_column("targets", "onboarding_error")
    op.drop_column("targets", "onboarding_step")
    op.drop_index("ix_parser_accounts_pool_mode", table_name="parser_accounts")
    for name in (
        "last_join_at", "join_window_count", "join_window_start",
        "dead_reason", "last_alive_at", "last_checked_at", "alive", "pool_mode",
    ):
        op.drop_column("parser_accounts", name)
