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


def test_finds_by_target_name(pg_session):
    _seed(pg_session)

    hits, total = search_messages(pg_session, query="Канал", limit=10, owner_user_id=None)

    assert total == 3
    assert len(hits) == 3


def test_finds_by_target_identifier(pg_session):
    _seed(pg_session)
    # A second target that matches the query but owns no events: a count query
    # missing the join would cross-join and report 6 instead of 3.
    pg_session.add(Target(parser_type="telegram", name="Канал 2", identifier="@chan2"))
    pg_session.commit()

    hits, total = search_messages(pg_session, query="@chan", limit=10, owner_user_id=None)

    assert total == len(hits) == 3
    assert {hit["target_identifier"] for hit in hits} == {"@chan"}


def test_underscore_is_not_a_wildcard(pg_session):
    _seed(pg_session)

    _, total = search_messages(pg_session, query="_", limit=10, owner_user_id=None)

    assert total == 0


def _seed_fragments(session) -> Target:
    target = Target(parser_type="telegram", name="Канал", identifier="@chan")
    session.add(target)
    session.commit()
    now = dt.datetime.now(dt.UTC)
    session.add_all(
        [
            RawEvent(
                parser_type="telegram", target_id=target.id, external_id="10",
                observed_at=now, payload={}, event_kind="message",
                text="КУРЬЕРЫ наличных средств, писать в лс",
                author_id="111", author_label="ID 111",
            ),
            RawEvent(
                parser_type="telegram", target_id=target.id, external_id="11",
                observed_at=now, payload={}, event_kind="message",
                text="кошелёк bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh оплата сегодня",
                author_id="222", author_label="ID 222",
            ),
            RawEvent(
                parser_type="telegram", target_id=target.id, external_id="12",
                observed_at=now, payload={}, event_kind="message",
                text="пишите @cryptoDealer77 по всем вопросам",
                author_id="333", author_label="ID 333",
            ),
        ]
    )
    session.commit()
    return target


def test_finds_a_fragment_inside_a_word(pg_session):
    """to_tsvector only matches whole words: 'курьер' never matches 'КУРЬЕРЫ'.
    Substring search is the stated use case, so the trigram arm must cover it."""
    _seed_fragments(pg_session)

    hits, total = search_messages(pg_session, query="курьер", limit=10, owner_user_id=None)

    assert total == 1, "a fragment of a longer word must still match"
    assert "КУРЬЕРЫ" in hits[0]["text"]


def test_finds_a_slice_of_a_crypto_wallet(pg_session):
    """A wallet address is one giant token for FTS, so only a substring match
    can find a pasted fragment of it."""
    _seed_fragments(pg_session)

    hits, total = search_messages(pg_session, query="2n0yrf2493", limit=10, owner_user_id=None)

    assert total == 1
    assert "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh" in hits[0]["text"]


def test_finds_a_nickname_mentioned_inside_a_message(pg_session):
    """The nickname belongs to somebody else — it is in the text, not in
    author_label — and the search is case-insensitive."""
    _seed_fragments(pg_session)

    hits, total = search_messages(pg_session, query="cryptodealer", limit=10, owner_user_id=None)

    assert total == 1
    assert "@cryptoDealer77" in hits[0]["text"]


def test_short_query_does_not_use_the_substring_arm(pg_session):
    """Under 3 characters pg_trgm has no trigrams to look up, so the arm is
    skipped rather than forcing a sequential scan over every event."""
    _seed_fragments(pg_session)

    _, total = search_messages(pg_session, query="ку", limit=10, owner_user_id=None)

    assert total == 0, "'ку' is a substring of 'КУРЬЕРЫ' but must not trigger a scan"
