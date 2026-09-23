from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParseJob, ParserAccount, Target, TargetAccountLink
from app.plugins import telegram as tg


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _linked_target(session, **config):
    acc = ParserAccount(parser_type="telegram", label="a", credentials={"api_id": 1, "api_hash": "h", "session_string": "s"})
    target = Target(parser_type="telegram", name="c", identifier="@c", config={"live_enabled": True, **config})
    session.add_all([acc, target])
    session.commit()
    session.add(TargetAccountLink(target_id=target.id, account_id=acc.id, is_active=True))
    session.commit()
    return target, acc


def test_run_dispatches_onboard_mode(monkeypatch):
    session = _session()
    target, _ = _linked_target(session)
    job = ParseJob(parser_type="telegram", target_id=target.id, payload={"mode": "onboard"}, queue="telegram_backfill")
    called = {}

    def fake_run(session_, job_, target_, **kw):
        called["ok"] = True

    monkeypatch.setattr(tg, "run_onboard_job", fake_run)
    events = tg.TelegramPlugin().run(session, job, target, None)

    assert called == {"ok": True}
    assert events == []


def test_generate_jobs_respects_quiet_period():
    session = _session()
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=3)).isoformat()
    target, _ = _linked_target(session, quiet_until=future)

    assert tg.TelegramPlugin().generate_jobs(session, target) == []


def test_generate_jobs_resumes_after_quiet_period():
    session = _session()
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=3)).isoformat()
    target, _ = _linked_target(session, quiet_until=past)

    assert len(tg.TelegramPlugin().generate_jobs(session, target)) > 0


def test_generate_jobs_ignores_dead_account():
    session = _session()
    target, acc = _linked_target(session)
    acc.alive = False
    session.commit()

    jobs = tg.TelegramPlugin().generate_jobs(session, target)

    assert jobs == []
    assert target.onboarding_status.value == "blocked"


def test_active_target_without_an_account_starts_its_own_onboarding():
    """Regression: a target predating auto-onboarding (step 'idle', no link, no job)
    sat on "Чекає акаунт" forever — the scheduler re-flagged it every tick and
    nothing ever created a job for it."""
    session = _session()
    target = Target(parser_type="telegram", name="KIEVAVTO", identifier="@kievavto2",
                    config={}, is_active=True, onboarding_step="idle")
    session.add(target)
    session.commit()

    specs = tg.TelegramPlugin().generate_jobs(session, target)

    assert [s.job_key for s in specs] == [f"onboard:{target.id}"]
    assert specs[0].account_id is None and specs[0].payload["mode"] == "onboard"
    assert target.onboarding_step == "queued"


def test_failed_onboarding_is_not_restarted_automatically():
    """A verdict like 'private chat, nobody is inside' must not loop: re-queueing it
    would spend the pool's ten daily join slots on guaranteed failures."""
    session = _session()
    target = Target(parser_type="telegram", name="ЖК", identifier="-1001338286165",
                    config={}, is_active=True, onboarding_step="failed",
                    onboarding_error="Приватний чат: жоден акаунт пулу не є його учасником.")
    session.add(target)
    session.commit()

    assert tg.TelegramPlugin().generate_jobs(session, target) == []
    assert target.onboarding_step == "failed"
    assert target.onboarding_error.startswith("Приватний чат")
