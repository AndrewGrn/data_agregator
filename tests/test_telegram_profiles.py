import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParserAccount, ParserType, Target, TargetAccountLink, TelegramMembership, TelegramMembershipHistory, TelegramUser
from app.plugins.telegram import TelegramPlugin
from app.services.telegram_profiles import upsert_telegram_profile_from_event


def make_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_upsert_profile_from_message_and_participant_event():
    session = make_session()
    try:
        target = Target(parser_type=ParserType.telegram, name="tg", identifier="@test", config={})
        account = ParserAccount(parser_type=ParserType.telegram, label="acc1", credentials={}, is_active=True)
        session.add_all([target, account])
        session.commit()

        msg_payload = {
            "event_type": "telegram_message",
            "date": "2026-03-23T18:00:00+00:00",
            "sender": {"id": 101, "username": "Alice", "first_name": "Alice"},
            "raw": {"sample": True},
        }
        upsert_telegram_profile_from_event(session, target.id, account.id, msg_payload, dt.datetime(2026, 3, 23, 18, 0, tzinfo=dt.UTC))
        session.commit()

        user = session.execute(select(TelegramUser).where(TelegramUser.telegram_user_id == 101)).scalar_one()
        membership = session.execute(select(TelegramMembership).where(TelegramMembership.telegram_user_ref_id == user.id)).scalar_one()
        history = session.execute(select(TelegramMembershipHistory).where(TelegramMembershipHistory.membership_id == membership.id)).scalars().all()

        assert user.username == "alice"
        assert membership.membership_status == "message_observed"
        assert membership.is_active is True
        assert len(history) == 1

        participant_payload = {
            "event_type": "telegram_participant",
            "date": "2026-03-23T18:10:00+00:00",
            "membership_status": "participant_admin",
            "membership_is_active": True,
            "joined_at": "2026-03-20T10:00:00+00:00",
            "sender": {"id": 101, "username": "alice", "first_name": "Alice"},
            "raw": {"participant": True},
        }
        upsert_telegram_profile_from_event(session, target.id, account.id, participant_payload, dt.datetime(2026, 3, 23, 18, 10, tzinfo=dt.UTC))
        session.commit()

        refreshed = session.execute(select(TelegramMembership).where(TelegramMembership.id == membership.id)).scalar_one()
        history_count = session.execute(
            select(TelegramMembershipHistory).where(TelegramMembershipHistory.membership_id == membership.id)
        ).scalars().all()

        assert refreshed.membership_status == "participant_admin"
        assert refreshed.joined_at == dt.datetime(2026, 3, 20, 10, 0, tzinfo=dt.UTC)
        assert len(history_count) == 2
    finally:
        session.close()


def test_generate_jobs_contains_participants_sync():
    session = make_session()
    try:
        target = Target(
            parser_type=ParserType.telegram,
            name="tg",
            identifier="@test",
            config={"participants_sync_enabled": True, "participants_sync_interval_seconds": 900, "participants_limit": 300},
        )
        account = ParserAccount(
            parser_type=ParserType.telegram,
            label="acc-jobs",
            credentials={"api_id": "1", "api_hash": "x", "session_string": "y"},
            is_active=True,
        )
        session.add_all([target, account])
        session.commit()
        session.add(TargetAccountLink(target_id=target.id, account_id=account.id, is_active=True))
        session.commit()

        plugin = TelegramPlugin()
        jobs = plugin.generate_jobs(session, target)
        keys = {job.job_key for job in jobs}

        assert f"gapfill:{target.id}:{account.id}" in keys
        assert f"participants:{target.id}:{account.id}" in keys
    finally:
        session.close()
