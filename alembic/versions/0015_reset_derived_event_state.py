"""reset state derived from the raw_events wipe in 0014

0014 rebuilt raw_events from scratch, but two tables fed by it kept their
pre-wipe values: telegram_offsets (whose max_message_id becomes min_id for
poll jobs, so the wiped history could never be re-collected) and
darknet_user_memberships.posts_count (which would be double-counted, since
darknet's dedup source is now empty and every post re-parses as new).

Revision ID: 0015_reset_derived_event_state
Revises: 0014_normalized_events
"""

from __future__ import annotations

from alembic import op

revision = "0015_reset_derived_event_state"
down_revision = "0014_normalized_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM telegram_offsets")
    op.execute("UPDATE darknet_user_memberships SET posts_count = 0")
    # Leftover verification rows from the 0014 rollout: a clean start means empty.
    op.execute("DELETE FROM raw_events")


def downgrade() -> None:
    raise NotImplementedError("0015 is a one-way migration: the reset state is gone")
