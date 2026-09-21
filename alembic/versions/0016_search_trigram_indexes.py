"""trigram indexes so message search can use an index instead of a seq scan

app/services/message_search.py ORs the full-text `@@` predicate with ILIKE
predicates on author label / author id / external id. An OR is only index-served
when *every* arm is indexable, so the unindexed ILIKE arms were dragging the
whole query down to a parallel seq scan over raw_events. pg_trgm GIN indexes
make those arms indexable, and the planner can then BitmapOr them together.

targets.name / targets.identifier get the same treatment: the search resolves
matching target ids in a separate small query (an OR arm across the join is not
indexable at all), and that query is index-served too.

Revision ID: 0016_search_trigram_indexes
Revises: 0015_reset_derived_event_state
"""

from __future__ import annotations

from alembic import op

revision = "0016_search_trigram_indexes"
down_revision = "0015_reset_derived_event_state"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_raw_events_author_label_trgm", "raw_events", "author_label"),
    ("ix_raw_events_author_id_trgm", "raw_events", "author_id"),
    ("ix_raw_events_external_id_trgm", "raw_events", "external_id"),
    ("ix_targets_name_trgm", "targets", "name"),
    ("ix_targets_identifier_trgm", "targets", "identifier"),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING GIN ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    # Unlike 0014/0015 this one is genuinely reversible: indexes carry no data.
    # The extension is left in place -- other objects may depend on it.
    for name, _table, _column in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
