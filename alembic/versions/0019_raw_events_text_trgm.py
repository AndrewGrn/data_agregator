"""raw_events.text trigram index: substring search (wallet fragments, a nickname
mentioned inside a message) is not what to_tsvector does — it indexes whole
words, so 'курьер' missed two thirds of the rows containing 'курьеры'.

Revision ID: 0019_raw_events_text_trgm
Revises: 0018_target_deleted_at
"""

from __future__ import annotations

from alembic import op

revision = "0019_raw_events_text_trgm"
down_revision = "0018_target_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_raw_events_text_trgm "
        "ON raw_events USING gin (text gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_raw_events_text_trgm")
