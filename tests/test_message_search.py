from __future__ import annotations

import datetime as dt

import pytest

from app.models import RawEvent, Target, User
from app.services.message_search import search_messages

pytestmark = pytest.mark.postgres


def _seed(session, *, owner_user_id: int | None = None) -> Target:
    if owner_user_id is not None:
        # owner_user_id is a real FK on Postgres; a matching row must exist.
        session.add(User(id=owner_user_id, username=f"owner{owner_user_id}", password_hash="x"))
        session.commit()

    target = Target(parser_type="telegram", name="Канал", identifier="@chan", owner_user_id=owner_user_id)
    session.add(target)
    session.commit()

    now = dt.datetime.now(dt.UTC)
    session.add_all(
        [
            RawEvent(
                parser_type="telegram", target_id=target.id, external_id="1",
                observed_at=now, payload={}, text="продаю велосипед в Киеве",
                author_id="777", author_label="@alice", event_kind="message",
                owner_user_id=owner_user_id,
            ),
            RawEvent(
                parser_type="telegram", target_id=target.id, external_id="2",
                observed_at=now - dt.timedelta(hours=1), payload={}, text="куплю велосипед",
                author_id="888", author_label="@bob", event_kind="comment",
                owner_user_id=owner_user_id,
            ),
            RawEvent(
                parser_type="telegram", target_id=target.id, external_id="3",
                observed_at=now, payload={}, text="совершенно другой текст",
                author_id="999", author_label="@carol", event_kind="message",
                owner_user_id=owner_user_id,
            ),
        ]
    )
    session.commit()
    return target


def test_finds_matching_messages(pg_session):
    _seed(pg_session)

    hits, total = search_messages(pg_session, query="велосипед", limit=10, owner_user_id=None)

    assert total == 2
    assert {hit["text"] for hit in hits} == {"продаю велосипед в Киеве", "куплю велосипед"}


def test_returns_highlighted_snippet(pg_session):
    _seed(pg_session)

    hits, _ = search_messages(pg_session, query="велосипед", limit=10, owner_user_id=None)

    assert any("<b>" in str(hit["snippet"]) for hit in hits)


def test_empty_query_returns_everything_newest_first(pg_session):
    _seed(pg_session)

    hits, total = search_messages(pg_session, query="", limit=10, owner_user_id=None)

    assert total == 3
    assert hits[0]["observed_at"] >= hits[-1]["observed_at"]


def test_owner_filter_hides_other_users_events(pg_session):
    _seed(pg_session, owner_user_id=1)

    _, total = search_messages(pg_session, query="велосипед", limit=10, owner_user_id=2)

    assert total == 0


def test_comment_kind_is_reported(pg_session):
    _seed(pg_session)

    hits, _ = search_messages(pg_session, query="куплю", limit=10, owner_user_id=None)

    assert hits[0]["is_comment"] is True
    assert hits[0]["event_type"] == "comment"


def test_finds_by_author_label(pg_session):
    _seed(pg_session)

    hits, total = search_messages(pg_session, query="@alice", limit=10, owner_user_id=None)

    assert total == 1
    assert hits[0]["sender_label"] == "@alice"


def test_limit_is_capped(pg_session):
    _seed(pg_session)

    hits, total = search_messages(pg_session, query="", limit=2, owner_user_id=None)

    assert len(hits) == 2
    assert total == 3
