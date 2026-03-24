import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParserAccount, ParserType, Target, TelegramOffset
from app.services.telegram_offsets import get_or_create_offset, update_gapfill_state, update_offset_from_message


def make_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_offset_upsert_and_gapfill_state():
    session = make_session()
    try:
        target = Target(parser_type=ParserType.telegram, name="tg", identifier="@test", config={})
        account = ParserAccount(parser_type=ParserType.telegram, label="acc-offset", credentials={}, is_active=True)
        session.add_all([target, account])
        session.commit()

        row = get_or_create_offset(session, target.id, account.id)
        session.commit()
        assert row.max_message_id == 0

        update_offset_from_message(
            session=session,
            target_id=target.id,
            account_id=account.id,
            message_id=123,
            observed_at=dt.datetime(2026, 3, 23, 20, 0, tzinfo=dt.UTC),
            source="listener",
        )
        session.commit()

        update_gapfill_state(
            session=session,
            target_id=target.id,
            account_id=account.id,
            max_message_id=140,
            max_observed_at=dt.datetime(2026, 3, 23, 20, 5, tzinfo=dt.UTC),
            batch_count=50,
            batch_limit=200,
        )
        session.commit()

        saved = session.execute(select(TelegramOffset).where(TelegramOffset.target_id == target.id)).scalar_one()
        assert saved.max_message_id == 140
        assert saved.last_source == "gap_fill"
        assert saved.is_caught_up is True
        assert saved.last_gapfill_batch_count == 50
        assert saved.last_gapfill_limit == 200
    finally:
        session.close()

