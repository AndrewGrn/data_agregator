"""normalized events: jsonb payload, string parser_type, file tables

Revision ID: 0014_normalized_events
Revises: 0013_parse_job_dispatch_audit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_normalized_events"
down_revision = "0013_parse_job_dispatch_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # parser_type: enum -> varchar on tables whose data we keep
    for table in ("targets", "parser_accounts", "parse_jobs"):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN parser_type TYPE varchar(32) "
            f"USING parser_type::text"
        )

    # raw_events is rebuilt from scratch: no data migration (clean start).
    op.drop_table("raw_events")

    op.create_table(
        "raw_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parser_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("author_id", sa.String(128), nullable=True),
        sa.Column("author_label", sa.String(255), nullable=True),
        sa.Column("event_kind", sa.String(32), nullable=False, server_default="message"),
        sa.Column("thread_id", sa.String(128), nullable=True),
        sa.Column("reply_to", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_raw_events_parser_type", "raw_events", ["parser_type"])
    op.create_index("ix_raw_events_target_id", "raw_events", ["target_id"])
    op.create_index("ix_raw_events_owner_user_id", "raw_events", ["owner_user_id"])
    op.create_index("ix_raw_events_external_id", "raw_events", ["external_id"])
    op.create_index("ix_raw_events_author_id", "raw_events", ["author_id"])
    op.create_index("ix_raw_events_event_kind", "raw_events", ["event_kind"])
    op.create_index("ix_raw_events_thread_id", "raw_events", ["thread_id"])
    op.create_index("ix_raw_events_created_at", "raw_events", ["created_at"])
    op.create_index(
        "uq_raw_events_parser_target_external",
        "raw_events",
        ["parser_type", "target_id", "external_id"],
        unique=True,
    )

    # Maintained by Postgres; absent from the ORM model on purpose.
    op.execute(
        "ALTER TABLE raw_events ADD COLUMN text_search tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
    )
    op.execute("CREATE INDEX ix_raw_events_text_search ON raw_events USING GIN (text_search)")

    op.create_table(
        "event_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("mime", sa.String(128), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_files_sha256", "event_files", ["sha256"])

    op.create_table(
        "raw_event_files",
        sa.Column("raw_event_id", sa.Integer(), sa.ForeignKey("raw_events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("event_files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_ref", sa.String(512), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )

    # The enum type is now unused by every table.
    op.execute("DROP TYPE IF EXISTS parsertype")

    # OpenSearch autosync state is obsolete; migration 0009 stays in the
    # revision chain because 0010 references it.
    op.execute("DELETE FROM service_state WHERE key = 'opensearch_autosync_v1'")


def downgrade() -> None:
    raise NotImplementedError("0014 is a one-way migration: raw_events is rebuilt")
