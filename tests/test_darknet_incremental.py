import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParserType, RawEvent, Target
from app.plugins.darknet import DarknetPlugin


def make_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_select_threads_for_parse_with_reparse_interval():
    plugin = DarknetPlugin()
    target = Target(
        parser_type=ParserType.darknet,
        name="forum",
        identifier="https://example.onion",
        config={"reparse_existing_threads": True, "thread_reparse_interval_minutes": 60},
    )
    now = dt.datetime(2026, 3, 23, 12, 0, tzinfo=dt.UTC)
    thread_urls = [
        "https://example.onion/threads/new.1/",
        "https://example.onion/threads/old.2/",
        "https://example.onion/threads/recent.3/",
    ]
    last_seen_map = {
        "https://example.onion/threads/old.2/": now - dt.timedelta(minutes=120),
        "https://example.onion/threads/recent.3/": now - dt.timedelta(minutes=10),
    }

    selected, stats = plugin._select_threads_for_parse(target, thread_urls, last_seen_map, now)

    assert selected == [
        "https://example.onion/threads/new.1/",
        "https://example.onion/threads/old.2/",
    ]
    assert stats["new_threads"] == 1
    assert stats["reparse_threads"] == 1
    assert stats["skipped_recent_threads"] == 1


def test_select_threads_for_parse_without_reparse():
    plugin = DarknetPlugin()
    target = Target(
        parser_type=ParserType.darknet,
        name="forum",
        identifier="https://example.onion",
        config={"reparse_existing_threads": False, "thread_reparse_interval_minutes": 60},
    )
    now = dt.datetime(2026, 3, 23, 12, 0, tzinfo=dt.UTC)
    thread_urls = [
        "https://example.onion/threads/new.1/",
        "https://example.onion/threads/old.2/",
    ]
    last_seen_map = {
        "https://example.onion/threads/old.2/": now - dt.timedelta(minutes=300),
    }

    selected, stats = plugin._select_threads_for_parse(target, thread_urls, last_seen_map, now)

    assert selected == ["https://example.onion/threads/new.1/"]
    assert stats["new_threads"] == 1
    assert stats["reparse_threads"] == 0
    assert stats["skipped_recent_threads"] == 1


def test_load_existing_external_ids():
    session = make_session()
    try:
        target = Target(
            parser_type=ParserType.darknet,
            name="forum",
            identifier="https://example.onion",
            config={},
        )
        session.add(target)
        session.commit()

        session.add(
            RawEvent(
                parser_type=ParserType.darknet,
                target_id=target.id,
                external_id="https://example.onion/threads/x#post:100",
                observed_at=dt.datetime.now(dt.UTC),
                payload={},
            )
        )
        session.commit()

        plugin = DarknetPlugin()
        existing = plugin._load_existing_external_ids(
            session,
            target,
            [
                "https://example.onion/threads/x#post:100",
                "https://example.onion/threads/x#post:101",
            ],
        )

        assert "https://example.onion/threads/x#post:100" in existing
        assert "https://example.onion/threads/x#post:101" not in existing
    finally:
        session.close()
