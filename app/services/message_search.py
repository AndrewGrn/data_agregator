from __future__ import annotations

from typing import Any

from sqlalchemy import func, literal, or_, select, text
from sqlalchemy.orm import Session

from app.models import RawEvent, Target

FTS_CONFIG = "simple"
# pg_trgm builds no trigrams for shorter input, so its index cannot serve it.
TRIGRAM_MIN_CHARS = 3


def _tsquery(query: str):
    return func.websearch_to_tsquery(literal(FTS_CONFIG), literal(query))


def _ilike_pattern(text_query: str) -> str:
    """Wrap in wildcards, escaping the ones the user typed."""
    escaped = text_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_messages(
    session: Session,
    *,
    query: str,
    limit: int,
    owner_user_id: int | None,
    parser_type: str | None = None,
    target_id: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Full-text search over raw_events.

    Matching is on the generated text_search column; author label and target
    name are matched separately so a search for "@alice" still works.
    """
    safe_limit = max(1, min(int(limit or 100), 200))
    text_query = str(query or "").strip()

    filters = []
    if owner_user_id is not None:
        filters.append(RawEvent.owner_user_id == int(owner_user_id))
    if parser_type:
        filters.append(RawEvent.parser_type == str(parser_type))
    if target_id is not None:
        filters.append(RawEvent.target_id == int(target_id))

    if text_query:
        pattern = _ilike_pattern(text_query)
        # Matching targets are resolved first, as ids: an OR arm on the joined
        # table (or an IN-subquery arm) is not indexable, so the planner would
        # fall back to a seq scan over all of raw_events. With plain ids every
        # arm is a local predicate and the arms can be combined with a BitmapOr.
        # targets is a small curated table, so the list stays short.
        target_ids = (
            session.execute(
                select(Target.id).where(
                    or_(
                        Target.name.ilike(pattern, escape="\\"),
                        Target.identifier.ilike(pattern, escape="\\"),
                    )
                )
            )
            .scalars()
            .all()
        )
        arms = [
            text(
                "raw_events.text_search @@ websearch_to_tsquery(:cfg, :q)"
            ).bindparams(cfg=FTS_CONFIG, q=text_query),
            RawEvent.author_label.ilike(pattern, escape="\\"),
            RawEvent.author_id.ilike(pattern, escape="\\"),
            RawEvent.external_id.ilike(pattern, escape="\\"),
        ]
        # to_tsvector indexes whole words, so a fragment ("курьер" inside
        # "курьеры", a slice of a wallet address, an @nick quoted in a message)
        # never matches the FTS arm. The trigram index on text covers exactly
        # that. GIN trigram needs 3 characters to produce a trigram; below that
        # the index cannot be used and the arm would force a seq scan.
        if len(text_query) >= TRIGRAM_MIN_CHARS:
            arms.append(RawEvent.text.ilike(pattern, escape="\\"))
        if target_ids:
            arms.append(RawEvent.target_id.in_(target_ids))
        filters.append(or_(*arms))

    # No join here: the filters are all predicates on raw_events now.
    total = session.execute(
        select(func.count()).select_from(RawEvent).where(*filters)
    ).scalar_one()

    if text_query:
        rank = func.ts_rank(
            text("raw_events.text_search"),
            _tsquery(text_query),
        )
        order_by = [rank.desc(), RawEvent.observed_at.desc().nullslast(), RawEvent.id.desc()]
    else:
        order_by = [RawEvent.observed_at.desc().nullslast(), RawEvent.id.desc()]

    rows = session.execute(
        select(RawEvent, Target.name, Target.identifier)
        .join(Target, Target.id == RawEvent.target_id)
        .where(*filters)
        .order_by(*order_by)
        .limit(safe_limit)
    ).all()

    # ts_headline is expensive, so it runs only over the rows we return.
    snippets: dict[int, str] = {}
    if text_query and rows:
        event_ids = [row[0].id for row in rows]
        headline_rows = session.execute(
            select(
                RawEvent.id,
                func.ts_headline(
                    literal(FTS_CONFIG),
                    func.coalesce(RawEvent.text, ""),
                    _tsquery(text_query),
                    literal("MaxFragments=1, MaxWords=30, MinWords=5"),
                ),
            ).where(RawEvent.id.in_(event_ids))
        ).all()
        snippets = {int(row[0]): str(row[1] or "") for row in headline_rows}

    hits: list[dict[str, Any]] = []
    for event, target_name, target_identifier in rows:
        observed = event.observed_at or event.created_at
        hits.append(
            {
                "event_id": int(event.id),
                "parser_type": str(event.parser_type),
                "target_id": int(event.target_id),
                "target_name": str(target_name or "-"),
                "target_identifier": str(target_identifier or "-"),
                "account_id": int(event.account_id) if event.account_id is not None else None,
                "external_id": event.external_id,
                "event_type": str(event.event_kind or ""),
                "is_comment": str(event.event_kind or "") == "comment",
                "sender_id": event.author_id,
                "sender_label": str(event.author_label or "-"),
                "sender_username": event.author_id,
                "text": str(event.text or ""),
                "snippet": snippets.get(int(event.id)) or None,
                "observed_at": observed.isoformat() if observed else None,
            }
        )
    return hits, int(total)
