# Telegram: автоподключение, живость, failover — бэкенд Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Канал подключается одной строкой ввода: система сама выбирает аккаунт пула, вступает в канал с безопасным темпом и запускает аккуратный парсинг; мёртвые аккаунты обнаруживаются фоном, их каналы переезжают к живым.

**Architecture:** Новый сервис `telegram_onboarding.py` создаёт таргет в состоянии `queued` и ставит задание `onboard` в очередь `telegram_backfill`; задание выбирает аккаунт (предпочитая уже состоящие в канале), вступает через Telethon с темпом и ставит одну активную связь. Планировщик получает шаг проверки живости с правилом двух неудач; смерть аккаунта пула гасит его связи и возвращает каналы в онбординг. Работник получает исключение `DeferJob` — отложить задание без штрафа.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, Alembic, Telethon 1.41.2, FastAPI, pytest (SQLite для юнит-тестов, `pg_session` для FK и TestClient).

**Spec:** `docs/superpowers/specs/2026-09-22-telegram-module-redesign-design.md`

## Global Constraints

- Один активный аккаунт на таргет: частичный уникальный индекс `uq_tal_one_active ON target_account_links (target_id) WHERE is_active`.
- `pool_mode ∈ {"shared", "dedicated"}`, default `"shared"`. Только `shared` участвует в авто-выборе и failover.
- Темп вступлений: `telegram_join_min_gap_seconds = 900`, `telegram_join_daily_limit = 10` (настройки в `app/config.py`).
- Живость: `telegram_liveness_interval_seconds = 600`; мёртвым признаётся аккаунт после **двух** подряд проверок `alive = False`.
- Тихий период после вступления: `random.randint(120, 300)` секунд, в течение которого `generate_jobs` не создаёт заданий по таргету.
- Дефолты автоподключённого таргета: `live_enabled=True`, `backfill.enabled=True`, `backfill.mode="full"`, `backfill_limit=200`, `poll_interval_seconds=300`, `comments_enabled=True`, `participants_sync_enabled=True`, без range-дат.
- Приглашения до вступления хранятся как `identifier = "invite:<hash>"`; после вступления заменяются каноническим (`@username` или `-100…`), хеш остаётся в `config["invite_hash"]`.
- Новые эндпоинты — под `/modules/telegram/...`, обновления через `POST` (как существующие `update-label`, `reset-cooldown`), не `PATCH`.
- Логирование только через `logging.getLogger(__name__)` с ленивыми `%s`-аргументами. Никаких `print()`.
- `from __future__ import annotations`, аннотации типов, 4 пробела.
- Рабочая база `aggregator` — только чтение. Тесты с Postgres — только `aggregator_test` через фикстуру `pg_session` (`TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test"`).
- Текущая голова Alembic: `0016_search_trigram_indexes`. Новая миграция — `0017`.

---

## File Structure

**Создаются:**
- `alembic/versions/0017_tg_onboarding_liveness.py` — колонки, дедупликация связей, частичный индекс.
- `app/services/telegram_onboarding.py` — `onboard()`, `pick_account()`, `join_channel()`, темп вступлений, `run_onboard_job()`. Отдельный модуль: вызывается из API, из плагина и из failover.
- `app/services/telegram_liveness.py` — `check_accounts_liveness()`, `failover_account()`. Отдельно от онбординга: другая частота, другой триггер.
- `tests/test_telegram_identifier.py`, `tests/test_defer_job.py`, `tests/test_telegram_onboarding.py`, `tests/test_telegram_liveness.py`, `tests/test_telegram_module_api.py`.

**Изменяются:**
- `app/models.py` — колонки `ParserAccount`, `Target`; индекс.
- `app/config.py` — четыре настройки.
- `app/services/telegram_accounts.py` — нормализация приглашений.
- `app/plugins/base.py` — `JobSpec.run_after`, `DeferJob`.
- `app/services/scheduler.py` — `run_after` из спеки; шаг живости в `schedule_once`.
- `app/services/worker.py` — обработка `DeferJob`.
- `app/plugins/telegram.py` — режим `onboard` в `run`; тихий период в `generate_jobs`; фильтр `pool_mode`/`alive` в выборе аккаунтов.
- `app/routers/api.py` — новые эндпоинты; расширение `GET /modules/telegram`; `smart-add` и `dialogs/add-target` поверх `onboard()`.

---

### Task 1: Схема — колонки, дедупликация связей, частичный индекс

**Files:**
- Modify: `app/models.py` (классы `ParserAccount`, `Target`, блок `Index(...)` в конце)
- Create: `alembic/versions/0017_tg_onboarding_liveness.py`
- Test: `tests/test_one_active_link.py`

**Interfaces:**
- Produces: `ParserAccount.pool_mode: str`, `.alive: bool | None`, `.last_checked_at`, `.last_alive_at`, `.dead_reason: str | None`, `.join_window_start`, `.join_window_count: int`, `.last_join_at`; `Target.onboarding_step: str`, `.onboarding_error: str | None`; индекс `uq_tal_one_active`.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_one_active_link.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParserAccount, Target, TargetAccountLink


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _fixtures(session):
    target = Target(parser_type="telegram", name="c", identifier="@c")
    a1 = ParserAccount(parser_type="telegram", label="a1", credentials={})
    a2 = ParserAccount(parser_type="telegram", label="a2", credentials={})
    session.add_all([target, a1, a2])
    session.commit()
    return target, a1, a2


def test_second_active_link_is_rejected():
    session = _session()
    target, a1, a2 = _fixtures(session)
    session.add(TargetAccountLink(target_id=target.id, account_id=a1.id, is_active=True))
    session.commit()
    session.add(TargetAccountLink(target_id=target.id, account_id=a2.id, is_active=True))

    with pytest.raises(IntegrityError):
        session.commit()


def test_inactive_links_may_coexist_with_one_active():
    session = _session()
    target, a1, a2 = _fixtures(session)
    session.add(TargetAccountLink(target_id=target.id, account_id=a1.id, is_active=False))
    session.add(TargetAccountLink(target_id=target.id, account_id=a2.id, is_active=True))
    session.commit()

    assert session.query(TargetAccountLink).count() == 2


def test_new_account_columns_have_defaults():
    session = _session()
    acc = ParserAccount(parser_type="telegram", label="x", credentials={})
    session.add(acc)
    session.commit()

    assert acc.pool_mode == "shared"
    assert acc.alive is None
    assert acc.join_window_count == 0


def test_new_target_columns_have_defaults():
    session = _session()
    t = Target(parser_type="telegram", name="c", identifier="@c")
    session.add(t)
    session.commit()

    assert t.onboarding_step == "idle"
    assert t.onboarding_error is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_one_active_link.py -v`
Expected: FAIL — `TypeError: 'pool_mode' is an invalid keyword argument` / `AttributeError`.

- [ ] **Step 3: Add the columns and the partial index to the models**

В `app/models.py`, в класс `ParserAccount` после `last_success_at`:

```python
    pool_mode: Mapped[str] = mapped_column(String(16), default="shared", index=True)
    alive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alive_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    join_window_start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    join_window_count: Mapped[int] = mapped_column(Integer, default=0)
    last_join_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

В класс `Target` после `onboarding_status`:

```python
    onboarding_step: Mapped[str] = mapped_column(String(32), default="idle", index=True)
    onboarding_error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

В блок индексов в конце файла (рядом с `uq_raw_events_parser_target_external`):

```python
Index(
    "uq_tal_one_active",
    TargetAccountLink.target_id,
    unique=True,
    postgresql_where=TargetAccountLink.is_active.is_(True),
    sqlite_where=TargetAccountLink.is_active.is_(True),
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_one_active_link.py -v`
Expected: PASS (4 passed). SQLite поддерживает частичные индексы, поэтому первый тест ловит `IntegrityError` и на нём.

- [ ] **Step 5: Write the migration**

Создать `alembic/versions/0017_tg_onboarding_liveness.py`:

```python
"""telegram onboarding + liveness: account pool/alive/join-window columns,
target onboarding step, one active link per target

Revision ID: 0017_tg_onboarding_liveness
Revises: 0016_search_trigram_indexes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_tg_onboarding_liveness"
down_revision = "0016_search_trigram_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("parser_accounts", sa.Column("pool_mode", sa.String(16), nullable=False, server_default="shared"))
    op.add_column("parser_accounts", sa.Column("alive", sa.Boolean(), nullable=True))
    op.add_column("parser_accounts", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("parser_accounts", sa.Column("last_alive_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("parser_accounts", sa.Column("dead_reason", sa.Text(), nullable=True))
    op.add_column("parser_accounts", sa.Column("join_window_start", sa.DateTime(timezone=True), nullable=True))
    op.add_column("parser_accounts", sa.Column("join_window_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("parser_accounts", sa.Column("last_join_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_parser_accounts_pool_mode", "parser_accounts", ["pool_mode"])

    op.add_column("targets", sa.Column("onboarding_step", sa.String(32), nullable=False, server_default="idle"))
    op.add_column("targets", sa.Column("onboarding_error", sa.Text(), nullable=True))
    op.create_index("ix_targets_onboarding_step", "targets", ["onboarding_step"])

    # One active link per target. Keep the link whose account is healthiest,
    # deactivate the rest, then enforce with a partial unique index.
    op.execute(
        """
        WITH ranked AS (
            SELECT l.id,
                   ROW_NUMBER() OVER (
                       PARTITION BY l.target_id
                       ORDER BY a.health_score DESC, l.id ASC
                   ) AS rn
            FROM target_account_links l
            JOIN parser_accounts a ON a.id = l.account_id
            WHERE l.is_active
        )
        UPDATE target_account_links SET is_active = false
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tal_one_active ON target_account_links (target_id) WHERE is_active"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tal_one_active")
    op.drop_index("ix_targets_onboarding_step", table_name="targets")
    op.drop_column("targets", "onboarding_error")
    op.drop_column("targets", "onboarding_step")
    op.drop_index("ix_parser_accounts_pool_mode", table_name="parser_accounts")
    for name in (
        "last_join_at", "join_window_count", "join_window_start",
        "dead_reason", "last_alive_at", "last_checked_at", "alive", "pool_mode",
    ):
        op.drop_column("parser_accounts", name)
```

- [ ] **Step 6: Apply the migration to a scratch database and verify**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && docker compose exec -T db psql -U postgres -c "DROP DATABASE IF EXISTS mig_scratch" -c "CREATE DATABASE mig_scratch" && \
DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/mig_scratch" alembic upgrade head && \
docker compose exec -T db psql -U postgres -d mig_scratch -c "\d target_account_links" | grep -i uq_tal_one_active && \
docker compose exec -T db psql -U postgres -c "DROP DATABASE mig_scratch"
```
Expected: миграции проходят до `0017`; в описании таблицы есть `uq_tal_one_active ... WHERE is_active`.

Дополнительно проверить дедупликацию на данных: в `mig_scratch` перед `upgrade head` до 0017 (`alembic upgrade 0016`) вставить таргет и две активные связи, затем `alembic upgrade head` и убедиться, что активной осталась одна.

- [ ] **Step 7: Run the full suite**

Run: `TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" pytest tests/ -q`
Expected: все зелёные (сейчас 91 passed + 4 новых).

- [ ] **Step 8: Commit**

```bash
git add app/models.py alembic/versions/0017_tg_onboarding_liveness.py tests/test_one_active_link.py
git commit -m "feat(telegram): колонки пула/живости/темпа, шаг онбординга, один активный аккаунт на таргет"
```

---

### Task 2: Нормализация ввода — приглашения и определение вида

**Files:**
- Modify: `app/services/telegram_accounts.py:12-30`
- Test: `tests/test_telegram_identifier.py`

**Interfaces:**
- Produces: `normalize_telegram_identifier(value) -> str` (расширено: `"invite:<hash>"` для приглашений), `is_invite_identifier(identifier) -> bool`, `invite_hash(identifier) -> str | None`.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_telegram_identifier.py`:

```python
from __future__ import annotations

import pytest

from app.services.telegram_accounts import (
    invite_hash,
    is_invite_identifier,
    normalize_telegram_identifier,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("@Durov", "@durov"),
        ("durov", "durov"),
        ("t.me/durov", "@durov"),
        ("https://t.me/Durov", "@durov"),
        ("https://telegram.me/durov", "@durov"),
        ("-1001234567890", "-1001234567890"),
        ("1234567890", "1234567890"),
        ("https://t.me/+AbCdEf123", "invite:AbCdEf123"),
        ("t.me/+AbCdEf123", "invite:AbCdEf123"),
        ("https://t.me/joinchat/AbCdEf123", "invite:AbCdEf123"),
        ("telegram.me/joinchat/AbCdEf123", "invite:AbCdEf123"),
        ("  ", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize_telegram_identifier(raw) == expected


def test_invite_helpers():
    assert is_invite_identifier("invite:AbC") is True
    assert is_invite_identifier("@durov") is False
    assert invite_hash("invite:AbC") == "AbC"
    assert invite_hash("@durov") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_telegram_identifier.py -v`
Expected: FAIL — `ImportError: cannot import name 'invite_hash'`, и параметры с `+`/`joinchat` дали бы `@joinchat` / сырую строку.

- [ ] **Step 3: Extend the normalizer**

В `app/services/telegram_accounts.py` заменить `normalize_telegram_identifier` и добавить два хелпера:

```python
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_USERNAME_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)",
    re.IGNORECASE,
)


def normalize_telegram_identifier(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    invite = _INVITE_RE.search(raw)
    if invite:
        return f"invite:{invite.group(1)}"

    link = _USERNAME_LINK_RE.search(raw)
    if link:
        return f"@{link.group(1).lower()}"

    if raw.startswith("@"):
        return f"@{raw[1:].strip().lower()}"

    if raw.lstrip("-").isdigit():
        return raw

    return raw


def is_invite_identifier(identifier: str) -> bool:
    return str(identifier or "").startswith("invite:")


def invite_hash(identifier: str) -> str | None:
    if not is_invite_identifier(identifier):
        return None
    return str(identifier).split(":", 1)[1] or None
```

Порядок проверок важен: приглашение проверяется **до** ссылки с username, иначе `t.me/joinchat/X` распарсится как `@joinchat`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_telegram_identifier.py -v`
Expected: PASS (13 passed).

- [ ] **Step 5: Run the existing telegram tests**

Run: `pytest tests/test_telegram_listener_routing.py tests/test_telegram_offsets.py tests/test_telegram_profiles.py -q`
Expected: PASS — старые формы ввода нормализуются как прежде.

- [ ] **Step 6: Commit**

```bash
git add app/services/telegram_accounts.py tests/test_telegram_identifier.py
git commit -m "feat(telegram): нормализация ссылок-приглашений в identifier invite:<hash>"
```

---

### Task 3: `DeferJob` и `JobSpec.run_after` — отложить без штрафа

**Files:**
- Modify: `app/plugins/base.py`
- Modify: `app/services/scheduler.py:60-82`
- Modify: `app/services/worker.py:384-404`
- Test: `tests/test_defer_job.py`

**Interfaces:**
- Produces: `class DeferJob(Exception)` с `.seconds: int`, `.reason: str`; `JobSpec.run_after: dt.datetime | None = None`; работник при `DeferJob` ставит `status=retry`, `run_after=now+seconds`, **не** увеличивает счётчик попыток и **не** штрафует аккаунт.

- [ ] **Step 1: Find where `attempt` is incremented**

Run: `grep -n "attempt" app/services/worker.py | head -20`
Ожидается строка вида `job.attempt += 1` при захвате задания (claim). Запомнить: если инкремент происходит при захвате, обработчик `DeferJob` должен его откатить.

- [ ] **Step 2: Write the failing test**

Создать `tests/test_defer_job.py`:

```python
from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import JobStatus, ParseJob, ParserAccount, Target
from app.plugins.base import DeferJob, JobSpec
from app.services import worker as worker_mod
from app.services.scheduler import _enqueue_job_specs


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_jobspec_run_after_is_honoured():
    session = _session()
    target = Target(parser_type="telegram", name="c", identifier="@c")
    session.add(target)
    session.commit()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)

    _enqueue_job_specs(
        session,
        target,
        [JobSpec(parser_type="telegram", target_id=target.id, account_id=None, job_key="k", payload={}, run_after=later)],
    )
    session.commit()

    job = session.query(ParseJob).one()
    assert abs((job.run_after.replace(tzinfo=dt.UTC) - later).total_seconds()) < 1


def test_defer_job_reschedules_without_penalty(monkeypatch):
    session = _session()
    target = Target(parser_type="telegram", name="c", identifier="@c")
    account = ParserAccount(parser_type="telegram", label="a", credentials={}, health_score=90.0, fail_count=0)
    session.add_all([target, account])
    session.commit()
    job = ParseJob(
        parser_type="telegram", target_id=target.id, account_id=account.id,
        job_key="onboard:1", payload={"mode": "onboard"}, queue="telegram_backfill",
        status=JobStatus.running, attempt=1, max_attempts=5,
    )
    session.add(job)
    session.commit()

    class _Plugin:
        def run(self, session, job, target, account):
            raise DeferJob(seconds=600, reason="join pacing")

    monkeypatch.setattr(worker_mod.plugin_registry, "get", lambda name: _Plugin())

    worker_mod._process_job(session, job)
    session.commit()

    job = session.get(ParseJob, job.id)
    account = session.get(ParserAccount, account.id)
    assert job.status == JobStatus.retry
    assert job.attempt == 0, "deferral must not consume an attempt"
    assert (job.run_after.replace(tzinfo=dt.UTC) - dt.datetime.now(dt.UTC)).total_seconds() > 500
    assert job.last_error == "join pacing"
    assert account.fail_count == 0
    assert account.health_score == 90.0
    assert account.cooldown_until is None
```

Если шаг 1 показал, что `attempt` **не** инкрементируется при захвате, заменить ожидание на `assert job.attempt == 1`.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_defer_job.py -v`
Expected: FAIL — `ImportError: cannot import name 'DeferJob'`.

- [ ] **Step 4: Add `DeferJob` and `run_after`**

В `app/plugins/base.py`:

```python
class DeferJob(Exception):
    """Ask the worker to re-run this job later without counting a failure.

    Raised by a plugin when the work is not possible *right now* for a
    reason that is not the job's fault: join pacing, a FloodWait on the
    chosen account, no free account in the pool yet.
    """

    def __init__(self, seconds: int, reason: str) -> None:
        super().__init__(reason)
        self.seconds = max(int(seconds), 1)
        self.reason = reason
```

В `JobSpec` добавить поле после `max_attempts`:

```python
    run_after: dt.datetime | None = None
```

- [ ] **Step 5: Honour `run_after` in the scheduler**

В `app/services/scheduler.py`, в `_enqueue_job_specs`, в конструкторе `ParseJob(...)` заменить `run_after=now,` на:

```python
                run_after=getattr(spec, "run_after", None) or now,
```

- [ ] **Step 6: Handle `DeferJob` in the worker**

В `app/services/worker.py` добавить импорт `from app.plugins.base import DeferJob` и **перед** `except Exception as exc:` в `_process_job` вставить:

```python
    except DeferJob as defer:
        session.rollback()
        job = session.get(ParseJob, job_id)
        if not job:
            return
        now = dt.datetime.now(dt.UTC)
        job.status = JobStatus.retry
        job.run_after = now + dt.timedelta(seconds=defer.seconds)
        # The claim already counted this attempt; a deferral is not a failure.
        job.attempt = max(int(job.attempt or 0) - 1, 0)
        job.last_error = defer.reason[:2000]
        job.locked_by = None
        job.lock_expires_at = None
        logger.info("defer job#%s for %ss: %s", job.id, defer.seconds, defer.reason)
        return
```

Если шаг 1 показал, что `attempt` не инкрементируется при захвате — строку с `max(...)` удалить.

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_defer_job.py -v`
Expected: PASS (2 passed).

- [ ] **Step 8: Commit**

```bash
git add app/plugins/base.py app/services/scheduler.py app/services/worker.py tests/test_defer_job.py
git commit -m "feat(worker): DeferJob — отложить задание без штрафа; JobSpec.run_after"
```

---

### Task 4: Сервис онбординга — выбор аккаунта, темп, вступление

**Files:**
- Create: `app/services/telegram_onboarding.py`
- Modify: `app/config.py`
- Test: `tests/test_telegram_onboarding.py`

**Interfaces:**
- Consumes: `DeferJob`, `JobSpec` (Task 3); `normalize_telegram_identifier`, `is_invite_identifier`, `invite_hash`, `find_dialog_match`, `compute_account_load_score` (`app/services/telegram_accounts.py`); `_telegram_target_config` (`app/routers/api.py`) — **не импортировать из роутера**, скопировать вызов дефолтов в сервис через новую функцию `default_onboard_config()`.
- Produces:
  - `onboard(session, *, raw_input, owner_user_id, account_id=None, allow_join=True) -> Target`
  - `pick_account(session, target, *, now) -> ParserAccount | None`
  - `join_pacing_wait_seconds(account, now) -> int`
  - `register_join(account, now) -> None`
  - `async join_channel(client, identifier) -> JoinOutcome`
  - `run_onboard_job(session, job, target, *, client_factory=default_client_factory) -> None`
  - `@dataclass JoinOutcome(identifier: str, kind: str, title: str | None, joined: bool)`

- [ ] **Step 1: Add the settings**

В `app/config.py`, рядом с `telegram_fetch_limit`:

```python
    telegram_join_min_gap_seconds: int = 900
    telegram_join_daily_limit: int = 10
    telegram_liveness_interval_seconds: int = 600
    telegram_liveness_failures_to_dead: int = 2
```

- [ ] **Step 2: Write the failing tests**

Создать `tests/test_telegram_onboarding.py`:

```python
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from telethon.errors import (
    ChannelsTooMuchError,
    FloodWaitError,
    InviteHashExpiredError,
    UserAlreadyParticipantError,
)

from app.db import Base
from app.models import JobStatus, ParseJob, ParserAccount, Target, TargetAccountLink
from app.plugins.base import DeferJob
from app.services import telegram_onboarding as onb


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _account(session, label, *, pool_mode="shared", alive=None, dialogs=None, health=100.0):
    acc = ParserAccount(
        parser_type="telegram", label=label, pool_mode=pool_mode, alive=alive,
        health_score=health, credentials={"api_id": 1, "api_hash": "h", "session_string": "s",
                                          "dialogs_cache": dialogs or []},
    )
    session.add(acc)
    session.commit()
    return acc


class _FakeEntity:
    def __init__(self, id=123, username="chan", megagroup=False, broadcast=True, title="Chan"):
        self.id = id
        self.username = username
        self.megagroup = megagroup
        self.broadcast = broadcast
        self.title = title


class _FakeClient:
    """Stands in for TelegramClient: records calls, raises on demand."""

    def __init__(self, *, entity=None, raise_on_join=None, invite_chat_id=555):
        self.entity = entity or _FakeEntity()
        self.raise_on_join = raise_on_join
        self.invite_chat_id = invite_chat_id
        self.calls: list[str] = []

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def is_user_authorized(self):
        return True

    async def get_entity(self, ident):
        self.calls.append(f"get_entity:{ident}")
        return self.entity

    async def __call__(self, request):
        name = type(request).__name__
        self.calls.append(name)
        if self.raise_on_join:
            raise self.raise_on_join
        if name == "ImportChatInviteRequest":
            class _Updates:
                chats = [_FakeEntity(id=self.invite_chat_id, username=None, megagroup=True, broadcast=False, title="Priv")]
            return _Updates()
        return None


def _run(coro):
    return asyncio.run(coro)


# ---- onboard(): creates target + job ------------------------------------

def test_onboard_creates_queued_target_and_job():
    session = _session()
    _account(session, "a1")
    target = onb.onboard(session, raw_input="https://t.me/durov", owner_user_id=7)
    session.commit()

    assert target.identifier == "@durov"
    assert target.onboarding_step == "queued"
    assert target.config["live_enabled"] is True
    assert target.config["backfill"]["mode"] == "full"
    job = session.execute(select(ParseJob)).scalar_one()
    assert job.job_key == f"onboard:{target.id}"
    assert job.payload["mode"] == "onboard"
    assert job.account_id is None


def test_onboard_with_explicit_account_pins_it_in_payload():
    session = _session()
    acc = _account(session, "priv", pool_mode="dedicated")
    target = onb.onboard(session, raw_input="@secret", owner_user_id=7, account_id=acc.id, allow_join=False)
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()
    assert job.payload["account_id"] == acc.id
    assert job.payload["allow_join"] is False


def test_onboard_is_idempotent_for_same_identifier():
    session = _session()
    _account(session, "a1")
    t1 = onb.onboard(session, raw_input="@durov", owner_user_id=7)
    session.commit()
    t2 = onb.onboard(session, raw_input="t.me/DUROV", owner_user_id=7)
    session.commit()
    assert t1.id == t2.id
    assert session.query(ParseJob).count() == 1


# ---- pick_account() ------------------------------------------------------

def test_pick_prefers_account_already_in_dialogs():
    session = _session()
    _account(session, "idle", health=100.0)
    member = _account(session, "member", health=60.0,
                      dialogs=[{"identifier": "@durov", "title": "Durov", "username": "durov"}])
    target = Target(parser_type="telegram", name="d", identifier="@durov")
    session.add(target)
    session.commit()

    chosen = onb.pick_account(session, target, now=dt.datetime.now(dt.UTC))
    assert chosen.id == member.id


def test_pick_skips_dedicated_dead_and_full_accounts():
    session = _session()
    _account(session, "dedicated", pool_mode="dedicated")
    _account(session, "dead", alive=False)
    full = _account(session, "full")
    full.credentials = {**full.credentials, "channels_full": True}
    session.commit()
    target = Target(parser_type="telegram", name="d", identifier="@durov")
    session.add(target)
    session.commit()

    assert onb.pick_account(session, target, now=dt.datetime.now(dt.UTC)) is None


# ---- pacing --------------------------------------------------------------

def test_join_pacing_enforces_min_gap_and_daily_limit():
    session = _session()
    acc = _account(session, "a")
    now = dt.datetime.now(dt.UTC)
    assert onb.join_pacing_wait_seconds(acc, now) == 0

    onb.register_join(acc, now)
    assert onb.join_pacing_wait_seconds(acc, now + dt.timedelta(seconds=10)) > 800

    acc.join_window_count = 10
    acc.last_join_at = now - dt.timedelta(hours=2)
    assert onb.join_pacing_wait_seconds(acc, now) > 3600


# ---- run_onboard_job(): the state machine --------------------------------

def _queued(session, identifier="@durov", **onboard_kwargs):
    _account(session, "a1")
    target = onb.onboard(session, raw_input=identifier, owner_user_id=7, **onboard_kwargs)
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()
    job.status = JobStatus.running
    session.commit()
    return target, job


def test_run_joins_public_channel_links_and_sets_quiet_period():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient()

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    session.commit()

    assert "JoinChannelRequest" in client.calls
    assert target.onboarding_step == "joined"
    assert target.onboarding_status.value == "ready"
    assert target.config["kind"] == "channel"
    link = session.execute(select(TargetAccountLink)).scalar_one()
    assert link.is_active is True and link.auto_detected is True
    quiet_until = dt.datetime.fromisoformat(target.config["quiet_until"])
    delta = (quiet_until - dt.datetime.now(dt.UTC)).total_seconds()
    assert 100 < delta <= 300


def test_run_resolves_invite_to_canonical_id():
    session = _session()
    target, job = _queued(session, identifier="https://t.me/+AbC")
    client = _FakeClient(invite_chat_id=555)

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    session.commit()

    assert "ImportChatInviteRequest" in client.calls
    assert target.identifier == "-100555"
    assert target.config["invite_hash"] == "AbC"
    assert target.config["kind"] == "group"


def test_run_skips_join_when_account_already_member():
    session = _session()
    _account(session, "member", dialogs=[{"identifier": "@durov", "title": "D", "username": "durov"}])
    target = onb.onboard(session, raw_input="@durov", owner_user_id=7)
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()
    client = _FakeClient()

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert "JoinChannelRequest" not in client.calls
    assert target.onboarding_step == "joined"


def test_run_floodwait_cools_account_and_defers():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=FloodWaitError(request=None, capture=120))

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.cooldown_until is not None
    assert target.onboarding_step == "joining"


def test_run_channels_too_much_marks_full_and_defers():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=ChannelsTooMuchError(request=None))

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.credentials.get("channels_full") is True


def test_run_already_participant_is_success():
    session = _session()
    target, job = _queued(session)
    client = _FakeClient(raise_on_join=UserAlreadyParticipantError(request=None))

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)
    assert target.onboarding_step == "joined"


def test_run_expired_invite_fails_target_without_punishing_account():
    session = _session()
    target, job = _queued(session, identifier="t.me/+Old")
    client = _FakeClient(raise_on_join=InviteHashExpiredError(request=None))

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert target.onboarding_step == "failed"
    assert "expired" in (target.onboarding_error or "").lower() or "InviteHashExpired" in (target.onboarding_error or "")
    acc = session.execute(select(ParserAccount)).scalar_one()
    assert acc.cooldown_until is None


def test_run_no_candidates_fails_with_needs_account():
    session = _session()
    target = onb.onboard(session, raw_input="@durov", owner_user_id=7)  # no accounts at all
    session.commit()
    job = session.execute(select(ParseJob)).scalar_one()

    onb.run_onboard_job(session, job, target, client_factory=lambda acc: _FakeClient())

    assert target.onboarding_step == "failed"
    assert target.onboarding_status.value == "needs_account"


def test_run_pacing_defers_before_touching_telegram():
    session = _session()
    target, job = _queued(session)
    acc = session.execute(select(ParserAccount)).scalar_one()
    onb.register_join(acc, dt.datetime.now(dt.UTC))
    session.commit()
    client = _FakeClient()

    with pytest.raises(DeferJob):
        onb.run_onboard_job(session, job, target, client_factory=lambda acc: client)

    assert client.calls == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_telegram_onboarding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.telegram_onboarding'`.

- [ ] **Step 4: Write the service**

Создать `app/services/telegram_onboarding.py`:

```python
from __future__ import annotations

import datetime as dt
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from app.config import get_settings
from app.models import (
    JobStatus,
    OnboardingStatus,
    ParseJob,
    ParserAccount,
    Target,
    TargetAccountLink,
)
from app.plugins.base import DeferJob, JobSpec
from app.services.scheduler import _enqueue_job_specs
from app.services.telegram_accounts import (
    compute_account_load_score,
    find_dialog_match,
    invite_hash,
    is_invite_identifier,
    normalize_telegram_identifier,
)

logger = logging.getLogger(__name__)
settings = get_settings()

QUEUE_ONBOARD = "telegram_backfill"
_TERMINAL_JOIN_ERRORS = (
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
)


@dataclass(slots=True)
class JoinOutcome:
    identifier: str
    kind: str
    title: str | None
    joined: bool


def default_onboard_config() -> dict[str, Any]:
    """Defaults for a channel the user only gave us a link for."""
    return {
        "limit": int(settings.telegram_fetch_limit),
        "poll_interval_seconds": 300,
        "live_enabled": True,
        "gapfill_limit": int(settings.telegram_fetch_limit),
        "participants_sync_enabled": True,
        "participants_sync_interval_seconds": 3600,
        "participants_limit": 1000,
        "comments_enabled": True,
        "gapfill_comments_enabled": True,
        "backfill": {
            "enabled": True,
            "mode": "full",
            "limit": 200,
            "full_batch_size": 300,
            "comments_enabled": True,
        },
    }


# --------------------------------------------------------------------------
# Entry point from the API: create the target row and queue the work.
# --------------------------------------------------------------------------

def onboard(
    session: Session,
    *,
    raw_input: str,
    owner_user_id: int | None,
    account_id: int | None = None,
    allow_join: bool = True,
) -> Target:
    identifier = normalize_telegram_identifier(raw_input)
    if not identifier:
        raise ValueError("Порожній ідентифікатор каналу")

    target = session.execute(
        select(Target).where(Target.parser_type == "telegram", Target.identifier == identifier)
    ).scalar_one_or_none()
    if target is None:
        target = Target(
            parser_type="telegram",
            name=raw_input.strip(),
            identifier=identifier,
            owner_user_id=owner_user_id,
            config=default_onboard_config(),
            is_active=True,
        )
        session.add(target)
        session.flush()

    target.is_active = True
    target.onboarding_step = "queued"
    target.onboarding_error = None
    target.onboarding_status = OnboardingStatus.needs_account

    payload: dict[str, Any] = {"mode": "onboard", "allow_join": bool(allow_join)}
    if account_id is not None:
        payload["account_id"] = int(account_id)

    _enqueue_job_specs(
        session,
        target,
        [
            JobSpec(
                parser_type="telegram",
                target_id=target.id,
                account_id=None,
                job_key=f"onboard:{target.id}",
                payload=payload,
                priority=50,
                queue=QUEUE_ONBOARD,
                max_attempts=20,
            )
        ],
    )
    logger.info("onboard queued target=%s identifier=%s account=%s", target.id, identifier, account_id)
    return target


# --------------------------------------------------------------------------
# Account choice and join pacing.
# --------------------------------------------------------------------------

def _is_member(account: ParserAccount, identifier: str) -> bool:
    dialogs = (account.credentials or {}).get("dialogs_cache") or []
    return find_dialog_match(identifier, dialogs) is not None


def _queued_jobs(session: Session, account_id: int) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(ParseJob).where(
                ParseJob.parser_type == "telegram",
                ParseJob.account_id == account_id,
                ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
            )
        )
        or 0
    )


def _available(account: ParserAccount, now: dt.datetime) -> bool:
    if account.alive is False:
        return False
    if (account.credentials or {}).get("channels_full"):
        return False
    if account.cooldown_until and account.cooldown_until > now:
        return False
    if account.health_score < 20:
        return False
    return True


def pick_account(session: Session, target: Target, *, now: dt.datetime) -> ParserAccount | None:
    """Prefer an account that is already a member; otherwise the least loaded."""
    accounts = session.execute(
        select(ParserAccount).where(
            ParserAccount.parser_type == "telegram",
            ParserAccount.is_active.is_(True),
            ParserAccount.pool_mode == "shared",
        )
    ).scalars().all()
    candidates = [a for a in accounts if _available(a, now)]
    if not candidates:
        return None

    def key(a: ParserAccount) -> tuple[int, float]:
        member = 0 if _is_member(a, target.identifier) else 1
        return (member, compute_account_load_score(a, queued_jobs=_queued_jobs(session, a.id), now=now))

    return sorted(candidates, key=key)[0]


def _refresh_join_window(account: ParserAccount, now: dt.datetime) -> None:
    if not account.join_window_start or (now - account.join_window_start) >= dt.timedelta(days=1):
        account.join_window_start = now
        account.join_window_count = 0


def join_pacing_wait_seconds(account: ParserAccount, now: dt.datetime) -> int:
    """Seconds until this account may join another chat; 0 if it may now."""
    _refresh_join_window(account, now)
    min_gap = int(settings.telegram_join_min_gap_seconds)
    daily = int(settings.telegram_join_daily_limit)

    wait = 0
    if account.last_join_at:
        since = (now - account.last_join_at).total_seconds()
        if since < min_gap:
            wait = max(wait, int(min_gap - since))
    if account.join_window_count >= daily and account.join_window_start:
        until_reset = (account.join_window_start + dt.timedelta(days=1) - now).total_seconds()
        wait = max(wait, int(until_reset))
    return max(wait, 0)


def register_join(account: ParserAccount, now: dt.datetime) -> None:
    _refresh_join_window(account, now)
    account.join_window_count += 1
    account.last_join_at = now


# --------------------------------------------------------------------------
# Telethon side.
# --------------------------------------------------------------------------

def default_client_factory(account: ParserAccount) -> TelegramClient:
    creds = account.credentials or {}
    return TelegramClient(StringSession(str(creds["session_string"])), int(creds["api_id"]), str(creds["api_hash"]))


def _kind_of(entity: Any) -> str:
    if getattr(entity, "megagroup", False):
        return "group"
    if getattr(entity, "broadcast", False):
        return "channel"
    if type(entity).__name__ == "Chat":
        return "group"
    return "private"


def _canonical(entity: Any) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"@{str(username).lower()}"
    return f"-100{int(entity.id)}"


async def join_channel(client: Any, identifier: str) -> JoinOutcome:
    """Join by @username/id or by invite hash. Raises Telethon errors as-is."""
    if is_invite_identifier(identifier):
        updates = await client(ImportChatInviteRequest(invite_hash(identifier)))
        chat = updates.chats[0]
        return JoinOutcome(identifier=_canonical(chat), kind=_kind_of(chat), title=getattr(chat, "title", None), joined=True)

    entity = await client.get_entity(identifier.lstrip("@") if identifier.startswith("@") else identifier)
    await client(JoinChannelRequest(entity))
    return JoinOutcome(identifier=_canonical(entity), kind=_kind_of(entity), title=getattr(entity, "title", None), joined=True)


async def _resolve_only(client: Any, identifier: str) -> JoinOutcome:
    entity = await client.get_entity(identifier.lstrip("@") if identifier.startswith("@") else identifier)
    return JoinOutcome(identifier=_canonical(entity), kind=_kind_of(entity), title=getattr(entity, "title", None), joined=False)


async def _with_client(client: Any, coro_factory):
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("account session is not authorized")
        return await coro_factory()
    finally:
        await client.disconnect()


# --------------------------------------------------------------------------
# The job body: called by TelegramPlugin.run for payload.mode == "onboard".
# --------------------------------------------------------------------------

def _fail(target: Target, reason: str, *, status: OnboardingStatus = OnboardingStatus.needs_account) -> None:
    target.onboarding_step = "failed"
    target.onboarding_error = reason[:2000]
    target.onboarding_status = status
    logger.warning("onboard failed target=%s: %s", target.id, reason)


def _link(session: Session, target: Target, account: ParserAccount) -> None:
    for link in session.execute(
        select(TargetAccountLink).where(TargetAccountLink.target_id == target.id, TargetAccountLink.is_active.is_(True))
    ).scalars():
        link.is_active = False
    session.flush()
    session.add(
        TargetAccountLink(
            target_id=target.id,
            account_id=account.id,
            owner_user_id=target.owner_user_id,
            is_active=True,
            auto_detected=True,
        )
    )


def run_onboard_job(
    session: Session,
    job: ParseJob,
    target: Target,
    *,
    client_factory: Callable[[ParserAccount], Any] = default_client_factory,
) -> None:
    import asyncio

    now = dt.datetime.now(dt.UTC)
    payload = dict(job.payload or {})
    allow_join = bool(payload.get("allow_join", True))
    pinned_id = payload.get("account_id")

    target.onboarding_step = "resolving"

    if pinned_id is not None:
        account = session.get(ParserAccount, int(pinned_id))
        if account is None or not account.is_active:
            _fail(target, "призначений акаунт не знайдено або вимкнено")
            return
    else:
        account = pick_account(session, target, now=now)
        if account is None:
            _fail(target, "немає доступних акаунтів пулу")
            return

    already_member = _is_member(account, target.identifier)
    needs_join = not already_member and not is_invite_identifier(target.identifier) is False or (
        is_invite_identifier(target.identifier) and not already_member
    )
    needs_join = not already_member

    if needs_join and not allow_join:
        _fail(target, "акаунт не є учасником, а вступ заборонено")
        return

    if needs_join:
        wait = join_pacing_wait_seconds(account, now)
        if wait > 0:
            raise DeferJob(seconds=wait, reason=f"join pacing on account #{account.id}")

    target.onboarding_step = "joining" if needs_join else "resolving"
    client = client_factory(account)

    try:
        if needs_join:
            outcome = asyncio.run(_with_client(client, lambda: join_channel(client, target.identifier)))
            register_join(account, now)
        else:
            outcome = asyncio.run(_with_client(client, lambda: _resolve_only(client, target.identifier)))
    except FloodWaitError as exc:
        seconds = int(getattr(exc, "seconds", 60) or 60)
        account.cooldown_until = now + dt.timedelta(seconds=seconds)
        logger.warning("onboard floodwait account=%s seconds=%s", account.id, seconds)
        raise DeferJob(seconds=min(seconds, 3600), reason=f"FloodWait {seconds}s on account #{account.id}")
    except ChannelsTooMuchError:
        account.credentials = {**(account.credentials or {}), "channels_full": True}
        logger.warning("onboard account=%s is full (ChannelsTooMuch)", account.id)
        raise DeferJob(seconds=30, reason=f"account #{account.id} has too many channels")
    except UserAlreadyParticipantError:
        outcome = asyncio.run(_with_client(client, lambda: _resolve_only(client, target.identifier)))
    except _TERMINAL_JOIN_ERRORS as exc:
        _fail(target, f"{type(exc).__name__}: {exc}")
        return

    # Canonical identity: invites become -100<id>, usernames stay @lower.
    if is_invite_identifier(target.identifier):
        config = dict(target.config or {})
        config["invite_hash"] = invite_hash(target.identifier)
        target.config = config
    target.identifier = outcome.identifier
    if not target.name or target.name.startswith(("http", "t.me", "@", "invite:")):
        target.name = outcome.title or outcome.identifier

    config = dict(target.config or {})
    config["kind"] = outcome.kind
    quiet_until = now + dt.timedelta(seconds=random.randint(120, 300))
    config["quiet_until"] = quiet_until.isoformat()
    target.config = config

    _link(session, target, account)
    target.onboarding_step = "joined"
    target.onboarding_error = None
    target.onboarding_status = OnboardingStatus.ready
    logger.info("onboard joined target=%s identifier=%s account=%s kind=%s", target.id, target.identifier, account.id, outcome.kind)
```

Убрать из `run_onboard_job` две промежуточные строки с `needs_join = ...` до финальной `needs_join = not already_member` — оставить только её (они здесь показаны, чтобы исполнитель не восстановил лишнюю логику).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_telegram_onboarding.py -v`
Expected: PASS (15 passed). Если тест `test_run_floodwait_cools_account_and_defers` падает на конструкторе `FloodWaitError(request=None, capture=120)` — свериться с сигнатурой в установленном Telethon (`python -c "from telethon.errors import FloodWaitError; help(FloodWaitError.__init__)"`) и поправить фикстуру, не код.

- [ ] **Step 6: Commit**

```bash
git add app/services/telegram_onboarding.py app/config.py tests/test_telegram_onboarding.py
git commit -m "feat(telegram): сервис онбординга — выбор аккаунта, темп вступлений, вступление, связь"
```

---

### Task 5: Плагин — режим `onboard`, тихий период, фильтр аккаунтов

**Files:**
- Modify: `app/plugins/telegram.py` (`run`, `generate_jobs`)
- Test: `tests/test_telegram_plugin_onboard.py`

**Interfaces:**
- Consumes: `run_onboard_job` (Task 4).
- Produces: `TelegramPlugin.run` при `payload.mode == "onboard"` вызывает `run_onboard_job` и возвращает `[]`; `generate_jobs` возвращает `[]`, пока `config["quiet_until"]` в будущем; выбор аккаунтов в `generate_jobs` игнорирует `alive is False`.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_telegram_plugin_onboard.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_telegram_plugin_onboard.py -v`
Expected: FAIL — `AttributeError: module 'app.plugins.telegram' has no attribute 'run_onboard_job'`; тест тихого периода возвращает задания.

- [ ] **Step 3: Wire the plugin**

В `app/plugins/telegram.py`:

Импорт (рядом с остальными из `app.services`):

```python
from app.services.telegram_onboarding import run_onboard_job
```

В `run(...)`, **первой** строкой после `mode = str(job.payload.get("mode") or "poll")`:

```python
        if mode == "onboard":
            run_onboard_job(session, job, target)
            return []
```

В `generate_jobs(...)`, сразу после `rows = session.execute(...).all()` и до проверки `if not rows:` — тихий период:

```python
        quiet_raw = (target.config or {}).get("quiet_until")
        quiet_until = _parse_iso_datetime(quiet_raw) if quiet_raw else None
        if quiet_until and quiet_until > dt.datetime.now(dt.UTC):
            return []
```

В `_is_account_available(...)` первой проверкой:

```python
        if account.alive is False:
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_telegram_plugin_onboard.py tests/test_telegram_onboarding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/plugins/telegram.py tests/test_telegram_plugin_onboard.py
git commit -m "feat(telegram): режим onboard в плагине, тихий период после вступления, мёртвые аккаунты не выбираются"
```

---

### Task 6: Живость аккаунтов и failover

**Files:**
- Create: `app/services/telegram_liveness.py`
- Modify: `app/services/scheduler.py` (`schedule_once`)
- Test: `tests/test_telegram_liveness.py`

**Interfaces:**
- Consumes: `refresh_account_session_info_sync(credentials) -> dict` (`telegram_accounts.py`), `onboard` / `_enqueue_job_specs`, `JobSpec`, `ServiceState`.
- Produces:
  - `check_accounts_liveness(session, *, now, checker=refresh_account_session_info_sync) -> dict` — `{"checked": n, "alive": n, "dead": n, "failed_over": n}`; сам гейтит по интервалу через `ServiceState["telegram_liveness_last_run"]`, параметр `force=True` обходит гейт.
  - `failover_account(session, account, *, now) -> int` — число таргетов, отправленных в онбординг.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_telegram_liveness.py`:

```python
from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParseJob, ParserAccount, ServiceState, Target, TargetAccountLink
from app.services import telegram_liveness as live


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _setup(session, *, pool_mode="shared"):
    acc = ParserAccount(parser_type="telegram", label="a", pool_mode=pool_mode,
                        credentials={"api_id": 1, "api_hash": "h", "session_string": "s"})
    t1 = Target(parser_type="telegram", name="c1", identifier="@c1", config={})
    t2 = Target(parser_type="telegram", name="c2", identifier="@c2", config={})
    session.add_all([acc, t1, t2])
    session.commit()
    session.add_all([
        TargetAccountLink(target_id=t1.id, account_id=acc.id, is_active=True),
        TargetAccountLink(target_id=t2.id, account_id=acc.id, is_active=True),
    ])
    session.commit()
    return acc, t1, t2


def _dead(creds):
    return {"alive": False, "is_authorized": False, "error": "AuthKeyUnregistered"}


def _ok(creds):
    return {"alive": True, "is_authorized": True, "username": "a"}


def test_single_failure_marks_but_does_not_failover():
    session = _session()
    acc, t1, t2 = _setup(session)
    now = dt.datetime.now(dt.UTC)

    result = live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    session.commit()

    assert result["dead"] == 0 and result["failed_over"] == 0
    assert acc.alive is False
    assert acc.dead_reason == "AuthKeyUnregistered"
    links = session.execute(select(TargetAccountLink)).scalars().all()
    assert all(l.is_active for l in links)


def test_two_failures_failover_shared_account():
    session = _session()
    acc, t1, t2 = _setup(session)
    now = dt.datetime.now(dt.UTC)

    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    result = live.check_accounts_liveness(session, now=now + dt.timedelta(minutes=11), checker=_dead, force=True)
    session.commit()

    assert result["dead"] == 1 and result["failed_over"] == 2
    links = session.execute(select(TargetAccountLink)).scalars().all()
    assert all(not l.is_active for l in links)
    for t in (t1, t2):
        assert t.onboarding_step == "queued"
    jobs = session.execute(select(ParseJob).where(ParseJob.job_key.like("onboard:%"))).scalars().all()
    assert len(jobs) == 2
    assert acc.is_active is True, "dead account stays active so it can come back"


def test_dedicated_account_does_not_failover():
    session = _session()
    acc, t1, t2 = _setup(session, pool_mode="dedicated")
    now = dt.datetime.now(dt.UTC)

    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    result = live.check_accounts_liveness(session, now=now + dt.timedelta(minutes=11), checker=_dead, force=True)
    session.commit()

    assert result["dead"] == 1 and result["failed_over"] == 0
    assert all(not l.is_active for l in session.execute(select(TargetAccountLink)).scalars())
    assert t1.onboarding_status.value == "needs_account"
    assert t1.onboarding_step == "idle"
    assert session.query(ParseJob).count() == 0


def test_alive_resets_failure_counter():
    session = _session()
    acc, *_ = _setup(session)
    now = dt.datetime.now(dt.UTC)

    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    live.check_accounts_liveness(session, now=now, checker=_ok, force=True)
    live.check_accounts_liveness(session, now=now, checker=_dead, force=True)
    session.commit()

    assert acc.alive is False
    assert all(l.is_active for l in session.execute(select(TargetAccountLink)).scalars()), "1 failure after recovery is not death"


def test_interval_gate_skips_when_recent():
    session = _session()
    _setup(session)
    now = dt.datetime.now(dt.UTC)

    first = live.check_accounts_liveness(session, now=now, checker=_ok)
    second = live.check_accounts_liveness(session, now=now + dt.timedelta(seconds=30), checker=_ok)

    assert first["checked"] == 1
    assert second["checked"] == 0
    state = session.get(ServiceState, "telegram_liveness_last_run")
    assert state is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_telegram_liveness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.telegram_liveness'`.

- [ ] **Step 3: Write the service**

Создать `app/services/telegram_liveness.py`:

```python
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import OnboardingStatus, ParserAccount, ServiceState, Target, TargetAccountLink
from app.plugins.base import JobSpec
from app.services.scheduler import _enqueue_job_specs
from app.services.telegram_accounts import refresh_account_session_info_sync

logger = logging.getLogger(__name__)
settings = get_settings()

STATE_KEY = "telegram_liveness_last_run"
QUEUE_ONBOARD = "telegram_backfill"


def _last_run(session: Session) -> dt.datetime | None:
    state = session.get(ServiceState, STATE_KEY)
    raw = (state.value or {}).get("at") if state else None
    if not raw:
        return None
    parsed = dt.datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _mark_run(session: Session, now: dt.datetime) -> None:
    state = session.get(ServiceState, STATE_KEY)
    if state is None:
        session.add(ServiceState(key=STATE_KEY, value={"at": now.isoformat()}))
    else:
        state.value = {"at": now.isoformat()}


def failover_account(session: Session, account: ParserAccount, *, now: dt.datetime) -> int:
    """Detach a dead account from its targets. Shared pool → re-onboard elsewhere."""
    links = session.execute(
        select(TargetAccountLink).where(TargetAccountLink.account_id == account.id, TargetAccountLink.is_active.is_(True))
    ).scalars().all()
    moved = 0
    for link in links:
        link.is_active = False
        target = session.get(Target, link.target_id)
        if target is None:
            continue
        target.onboarding_status = OnboardingStatus.needs_account
        if account.pool_mode == "shared":
            target.onboarding_step = "queued"
            target.onboarding_error = None
            _enqueue_job_specs(
                session,
                target,
                [
                    JobSpec(
                        parser_type="telegram",
                        target_id=target.id,
                        account_id=None,
                        job_key=f"onboard:{target.id}",
                        payload={"mode": "onboard", "allow_join": True},
                        priority=40,
                        queue=QUEUE_ONBOARD,
                        max_attempts=20,
                    )
                ],
            )
            moved += 1
        else:
            target.onboarding_step = "idle"
    logger.warning("failover account=%s pool_mode=%s detached=%s re-onboarded=%s", account.id, account.pool_mode, len(links), moved)
    return moved


def check_accounts_liveness(
    session: Session,
    *,
    now: dt.datetime,
    checker: Callable[[dict[str, Any]], dict[str, Any]] = refresh_account_session_info_sync,
    force: bool = False,
) -> dict[str, int]:
    """Probe every active Telegram account; two consecutive failures = dead."""
    result = {"checked": 0, "alive": 0, "dead": 0, "failed_over": 0}
    interval = dt.timedelta(seconds=int(settings.telegram_liveness_interval_seconds))
    last = _last_run(session)
    if not force and last is not None and (now - last) < interval:
        return result
    _mark_run(session, now)

    threshold = int(settings.telegram_liveness_failures_to_dead)
    accounts = session.execute(
        select(ParserAccount).where(ParserAccount.parser_type == "telegram", ParserAccount.is_active.is_(True))
    ).scalars().all()

    for account in accounts:
        result["checked"] += 1
        try:
            info = checker(dict(account.credentials or {}))
        except Exception as exc:  # probe itself blew up: treat as a failed check
            info = {"alive": False, "error": str(exc)}

        creds = dict(account.credentials or {})
        was_dead = account.alive is False and int(creds.get("liveness_failures", 0)) >= threshold
        account.last_checked_at = now

        if info.get("alive"):
            account.alive = True
            account.last_alive_at = now
            account.dead_reason = None
            creds["liveness_failures"] = 0
            account.credentials = creds
            result["alive"] += 1
            continue

        failures = int(creds.get("liveness_failures", 0)) + 1
        creds["liveness_failures"] = failures
        account.credentials = creds
        account.alive = False
        account.dead_reason = str(info.get("error") or "unknown")[:2000]
        logger.warning("liveness account=%s failed (%s/%s): %s", account.id, failures, threshold, account.dead_reason)

        if failures >= threshold and not was_dead:
            result["dead"] += 1
            result["failed_over"] += failover_account(session, account, now=now)

    return result
```

- [ ] **Step 4: Hook it into the scheduler**

В `app/services/scheduler.py`, в `schedule_once`, **первой** строкой тела (до `stmt = select(Target)...`):

```python
    from app.services.telegram_liveness import check_accounts_liveness

    check_accounts_liveness(session, now=dt.datetime.now(dt.UTC))
```

Импорт внутри функции — намеренно: `telegram_liveness` импортирует `_enqueue_job_specs` из `scheduler`, импорт на уровне модуля дал бы цикл. Пометить комментарием `# local import: telegram_liveness imports this module`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_telegram_liveness.py tests/test_scheduler.py -v`
Expected: PASS. `test_scheduler.py` продолжает проходить: гейт по интервалу пропускает проверку, а `checker` по умолчанию не вызывается, если аккаунтов в тесте нет.

- [ ] **Step 6: Commit**

```bash
git add app/services/telegram_liveness.py app/services/scheduler.py tests/test_telegram_liveness.py
git commit -m "feat(telegram): фоновая проверка живости аккаунтов, failover каналов пула"
```

---

### Task 7: API модуля — онбординг, аккаунты, переназначение

**Files:**
- Modify: `app/routers/api.py` (`GET /modules/telegram` ~1243; `smart-add` ~3101; `dialogs/add-target` ~3000; новые эндпоинты рядом с `/modules/telegram/accounts/...` ~2237)
- Test: `tests/test_telegram_module_api.py`

**Interfaces:**
- Consumes: `onboard`, `pick_account` (Task 4); `check_accounts_liveness`, `failover_account` (Task 6); `_ensure_target_access`, `_ensure_account_access`, `_is_admin` (существуют).
- Produces (все под `/api/modules/telegram`):
  - `POST /onboard` `{input: str, account_id?: int, allow_join?: bool}` → `TargetRow`
  - `GET /accounts/overview` → `[AccountRow]`
  - `POST /accounts/{id}/check-alive` → `{alive, error, checked_at}`
  - `POST /accounts/{id}/pool-mode` `{pool_mode}` → `AccountRow`
  - `GET /accounts/{id}/targets` → `[TargetRow]`
  - `POST /targets/{id}/reassign` `{account_id?: int}` → `TargetRow`
  - `POST /targets/{id}/retry-onboarding` → `TargetRow`
  - `GET /modules/telegram` — каждый элемент `targets[]` дополнен `account: {id, label, alive} | null`, `onboarding_step`, `onboarding_error`, `kind`.

Формы:

```python
TargetRow = {
  "id": int, "name": str, "identifier": str, "kind": str | None,
  "is_active": bool, "onboarding_status": str, "onboarding_step": str, "onboarding_error": str | None,
  "account": {"id": int, "label": str, "alive": bool | None} | None,
  "events_count": int, "last_event_at": str | None,
}
AccountRow = {
  "id": int, "label": str, "username": str | None, "pool_mode": str, "is_active": bool,
  "alive": bool | None, "last_checked_at": str | None, "dead_reason": str | None,
  "health_score": float, "cooldown_until": str | None,
  "targets_count": int, "joins_today": int, "join_daily_limit": int,
}
```

- [ ] **Step 1: Write the failing tests**

Создать `tests/test_telegram_module_api.py` (использует `pg_session` — нужны FK и `TestClient`; образец — `tests/test_whatsapp_api.py`, оттуда взять способ подмены `get_db`/`get_current_user`):

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import ParseJob, ParserAccount, Target, TargetAccountLink, User

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(pg_session):
    admin = User(username="adm", password_hash="x", role="admin", is_admin=True, is_active=True,
                 totp_enabled=True, totp_confirmed=True)
    pg_session.add(admin)
    pg_session.commit()

    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: admin
    yield TestClient(app), pg_session, admin
    app.dependency_overrides.clear()


def _account(session, label="a", pool_mode="shared"):
    acc = ParserAccount(parser_type="telegram", label=label, pool_mode=pool_mode,
                        credentials={"api_id": 1, "api_hash": "h", "session_string": "s"})
    session.add(acc)
    session.commit()
    return acc


def test_onboard_returns_queued_row(client):
    c, session, _ = client
    _account(session)

    resp = c.post("/api/modules/telegram/onboard", json={"input": "https://t.me/durov"})

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["identifier"] == "@durov"
    assert row["onboarding_step"] == "queued"
    assert row["account"] is None
    assert session.query(ParseJob).count() == 1


def test_onboard_rejects_empty_input(client):
    c, *_ = client
    assert c.post("/api/modules/telegram/onboard", json={"input": "   "}).status_code == 400


def test_accounts_overview_reports_liveness_and_load(client):
    c, session, _ = client
    acc = _account(session)
    acc.alive = True
    acc.join_window_count = 3
    t = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=True))
    session.commit()

    rows = c.get("/api/modules/telegram/accounts/overview").json()

    assert rows[0]["alive"] is True
    assert rows[0]["targets_count"] == 1
    assert rows[0]["joins_today"] == 3
    assert rows[0]["join_daily_limit"] == 10


def test_pool_mode_toggle(client):
    c, session, _ = client
    acc = _account(session)

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/pool-mode", json={"pool_mode": "dedicated"})

    assert resp.status_code == 200
    assert resp.json()["pool_mode"] == "dedicated"
    assert c.post(f"/api/modules/telegram/accounts/{acc.id}/pool-mode", json={"pool_mode": "weird"}).status_code == 400


def test_check_alive_updates_account(client, monkeypatch):
    c, session, _ = client
    acc = _account(session)
    import app.routers.api as api_mod
    monkeypatch.setattr(api_mod, "refresh_account_session_info_sync", lambda creds: {"alive": True, "username": "x"})

    resp = c.post(f"/api/modules/telegram/accounts/{acc.id}/check-alive")

    assert resp.status_code == 200
    assert resp.json()["alive"] is True
    session.refresh(acc)
    assert acc.alive is True and acc.last_checked_at is not None


def test_reassign_detaches_and_requeues(client):
    c, session, _ = client
    a1 = _account(session, "a1")
    a2 = _account(session, "a2", pool_mode="dedicated")
    t = Target(parser_type="telegram", name="c", identifier="@c", config={})
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=a1.id, is_active=True))
    session.commit()

    resp = c.post(f"/api/modules/telegram/targets/{t.id}/reassign", json={"account_id": a2.id})

    assert resp.status_code == 200
    assert resp.json()["onboarding_step"] == "queued"
    active = session.execute(select(TargetAccountLink).where(TargetAccountLink.is_active.is_(True))).scalars().all()
    assert active == []
    job = session.execute(select(ParseJob)).scalar_one()
    assert job.payload["account_id"] == a2.id


def test_retry_onboarding_from_failed(client):
    c, session, _ = client
    _account(session)
    t = Target(parser_type="telegram", name="c", identifier="@c", config={}, onboarding_step="failed", onboarding_error="x")
    session.add(t)
    session.commit()

    resp = c.post(f"/api/modules/telegram/targets/{t.id}/retry-onboarding")

    assert resp.status_code == 200
    assert resp.json()["onboarding_step"] == "queued"
    assert resp.json()["onboarding_error"] is None


def test_module_overview_includes_account_and_step(client):
    c, session, _ = client
    acc = _account(session)
    acc.alive = True
    t = Target(parser_type="telegram", name="c", identifier="@c", config={"kind": "channel"}, onboarding_step="joined")
    session.add(t)
    session.commit()
    session.add(TargetAccountLink(target_id=t.id, account_id=acc.id, is_active=True))
    session.commit()

    data = c.get("/api/modules/telegram").json()
    row = next(x for x in data["targets"] if x["id"] == t.id)

    assert row["account"] == {"id": acc.id, "label": "a", "alive": True}
    assert row["onboarding_step"] == "joined"
    assert row["kind"] == "channel"


def test_account_targets_lists_only_its_active_links(client):
    c, session, _ = client
    a1 = _account(session, "a1")
    a2 = _account(session, "a2")
    t1 = Target(parser_type="telegram", name="c1", identifier="@c1", config={})
    t2 = Target(parser_type="telegram", name="c2", identifier="@c2", config={})
    session.add_all([t1, t2])
    session.commit()
    session.add_all([
        TargetAccountLink(target_id=t1.id, account_id=a1.id, is_active=True),
        TargetAccountLink(target_id=t2.id, account_id=a2.id, is_active=True),
    ])
    session.commit()

    rows = c.get(f"/api/modules/telegram/accounts/{a1.id}/targets").json()
    assert [r["id"] for r in rows] == [t1.id]
```

Если в `tests/test_whatsapp_api.py` `User` создаётся иначе (другие обязательные поля) — скопировать оттуда фабрику пользователя дословно.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" pytest tests/test_telegram_module_api.py -v`
Expected: FAIL — `404 Not Found` на новых путях; overview без `account`.

- [ ] **Step 3: Add the row builders**

В `app/routers/api.py`, рядом с `_ensure_account_access`:

```python
def _active_account_for(db: Session, target_id: int) -> ParserAccount | None:
    return db.execute(
        select(ParserAccount)
        .join(TargetAccountLink, TargetAccountLink.account_id == ParserAccount.id)
        .where(TargetAccountLink.target_id == int(target_id), TargetAccountLink.is_active.is_(True))
    ).scalar_one_or_none()


def _telegram_target_row(db: Session, target: Target, *, events_count: int = 0, last_event_at: dt.datetime | None = None) -> dict:
    account = _active_account_for(db, target.id)
    return {
        "id": int(target.id),
        "name": target.name,
        "identifier": target.identifier,
        "kind": (target.config or {}).get("kind"),
        "is_active": bool(target.is_active),
        "onboarding_status": target.onboarding_status.value if target.onboarding_status else "ready",
        "onboarding_step": target.onboarding_step or "idle",
        "onboarding_error": target.onboarding_error,
        "account": {"id": int(account.id), "label": account.label, "alive": account.alive} if account else None,
        "events_count": int(events_count),
        "last_event_at": last_event_at.isoformat() if last_event_at else None,
    }


def _telegram_account_row(db: Session, account: ParserAccount) -> dict:
    now = dt.datetime.now(dt.UTC)
    targets_count = int(
        db.scalar(
            select(func.count()).select_from(TargetAccountLink).where(
                TargetAccountLink.account_id == account.id, TargetAccountLink.is_active.is_(True)
            )
        )
        or 0
    )
    joins_today = int(account.join_window_count or 0)
    if account.join_window_start and (now - account.join_window_start) >= dt.timedelta(days=1):
        joins_today = 0
    creds = account.credentials or {}
    return {
        "id": int(account.id),
        "label": account.label,
        "username": creds.get("username"),
        "pool_mode": account.pool_mode or "shared",
        "is_active": bool(account.is_active),
        "alive": account.alive,
        "last_checked_at": account.last_checked_at.isoformat() if account.last_checked_at else None,
        "dead_reason": account.dead_reason,
        "health_score": float(account.health_score or 0.0),
        "cooldown_until": account.cooldown_until.isoformat() if account.cooldown_until else None,
        "targets_count": targets_count,
        "joins_today": joins_today,
        "join_daily_limit": int(settings.telegram_join_daily_limit),
    }
```

- [ ] **Step 4: Add the endpoints**

Рядом с `POST /modules/telegram/accounts/reset-cooldown`:

```python
class OnboardRequest(BaseModel):
    input: str
    account_id: int | None = None
    allow_join: bool = True


@router.post("/modules/telegram/onboard")
def telegram_onboard(payload: OnboardRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not str(payload.input or "").strip():
        raise HTTPException(status_code=400, detail="Вкажіть посилання, @канал або ID")
    if payload.account_id is not None:
        _ensure_account_access(db.get(ParserAccount, int(payload.account_id)), user)
    try:
        target = telegram_onboarding.onboard(
            db,
            raw_input=payload.input,
            owner_user_id=int(user.id),
            account_id=payload.account_id,
            allow_join=payload.allow_join,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _telegram_target_row(db, target)


@router.get("/modules/telegram/accounts/overview")
def telegram_accounts_overview(db: Session = Depends(get_db), user=Depends(get_current_user)):
    stmt = select(ParserAccount).where(ParserAccount.parser_type == ParserType.telegram)
    if not _is_admin(user):
        stmt = stmt.where(ParserAccount.owner_user_id == int(user.id))
    return [_telegram_account_row(db, a) for a in db.execute(stmt.order_by(ParserAccount.id)).scalars()]


@router.post("/modules/telegram/accounts/{account_id}/check-alive")
def telegram_account_check_alive(account_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    account = _ensure_account_access(db.get(ParserAccount, int(account_id)), user)
    now = dt.datetime.now(dt.UTC)
    info = refresh_account_session_info_sync(dict(account.credentials or {}))
    account.last_checked_at = now
    account.alive = bool(info.get("alive"))
    if account.alive:
        account.last_alive_at = now
        account.dead_reason = None
    else:
        account.dead_reason = str(info.get("error") or "unknown")[:2000]
    db.commit()
    return {"alive": account.alive, "error": account.dead_reason, "checked_at": now.isoformat()}


class PoolModeRequest(BaseModel):
    pool_mode: str


@router.post("/modules/telegram/accounts/{account_id}/pool-mode")
def telegram_account_pool_mode(account_id: int, payload: PoolModeRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if payload.pool_mode not in {"shared", "dedicated"}:
        raise HTTPException(status_code=400, detail="pool_mode: shared або dedicated")
    account = _ensure_account_access(db.get(ParserAccount, int(account_id)), user)
    account.pool_mode = payload.pool_mode
    db.commit()
    return _telegram_account_row(db, account)


@router.get("/modules/telegram/accounts/{account_id}/targets")
def telegram_account_targets(account_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    account = _ensure_account_access(db.get(ParserAccount, int(account_id)), user)
    targets = db.execute(
        select(Target)
        .join(TargetAccountLink, TargetAccountLink.target_id == Target.id)
        .where(TargetAccountLink.account_id == account.id, TargetAccountLink.is_active.is_(True))
        .order_by(Target.name)
    ).scalars().all()
    return [_telegram_target_row(db, t) for t in targets]


class ReassignRequest(BaseModel):
    account_id: int | None = None


@router.post("/modules/telegram/targets/{target_id}/reassign")
def telegram_target_reassign(target_id: int, payload: ReassignRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = _ensure_target_access(db.get(Target, int(target_id)), user)
    if payload.account_id is not None:
        _ensure_account_access(db.get(ParserAccount, int(payload.account_id)), user)
    for link in db.execute(
        select(TargetAccountLink).where(TargetAccountLink.target_id == target.id, TargetAccountLink.is_active.is_(True))
    ).scalars():
        link.is_active = False
    db.flush()
    telegram_onboarding.onboard(
        db, raw_input=target.identifier, owner_user_id=target.owner_user_id, account_id=payload.account_id, allow_join=True
    )
    db.commit()
    return _telegram_target_row(db, target)


@router.post("/modules/telegram/targets/{target_id}/retry-onboarding")
def telegram_target_retry_onboarding(target_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = _ensure_target_access(db.get(Target, int(target_id)), user)
    telegram_onboarding.onboard(db, raw_input=target.identifier, owner_user_id=target.owner_user_id, allow_join=True)
    db.commit()
    return _telegram_target_row(db, target)
```

Импорт в начале файла: `from app.services import telegram_onboarding`. `BaseModel` уже импортирован (используется `CreateWhatsappAccountRequest`).

**Внимание к порядку маршрутов:** `GET /modules/telegram/accounts/overview` должен быть объявлен **до** любого `/modules/telegram/accounts/{account_id}/...`-GET, иначе FastAPI попытается разобрать `overview` как `account_id`. Существующий `GET .../accounts/{account_id}/dialogs` — GET, конфликт реален; разместить `overview` выше него.

- [ ] **Step 5: Extend `GET /modules/telegram`**

В обработчике `GET /modules/telegram` (~1243) найти место, где формируется список таргетов (словарь с `"id"`, `"name"`, `"identifier"`), и заменить построение элемента вызовом `_telegram_target_row(db, target, events_count=..., last_event_at=...)`, передав уже посчитанные там агрегаты. Остальные ключи ответа (`accounts`, `jobs`, …) не трогать.

- [ ] **Step 6: Route old forms through `onboard()`**

В `POST /modules/telegram/targets/smart-add` (~3101): цикл `for entry in entries:` заменить на

```python
    created = []
    for entry in entries:
        try:
            target = telegram_onboarding.onboard(db, raw_input=entry, owner_user_id=int(user.id), allow_join=True)
            created.append(target.id)
        except ValueError:
            continue
    db.commit()
    return {"created": len(created), "target_ids": created}
```

и удалить ставший ненужным код выбора аккаунта по диалогам внутри этого обработчика (кэш диалогов теперь читает `pick_account`). Если фронтенд читает другие ключи из ответа `smart-add` — сохранить их с нулевыми значениями; проверить `grep -n "smart-add" -A6 frontend/src/pages/TelegramPage.tsx`.

В `POST /modules/telegram/accounts/{account_id}/dialogs/add-target` (~3000): тело заменить на `onboard(..., account_id=account.id, allow_join=False)` — аккаунт уже состоит в диалоге, вступать не нужно.

- [ ] **Step 7: Run tests to verify they pass**

Run: `TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" pytest tests/test_telegram_module_api.py -v`
Expected: PASS (9 passed).

- [ ] **Step 8: Run the full suite and start the API**

Run: `TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" pytest tests/ -q`
Expected: 0 failed.

Run: `python -c "from app.main import app; print('ok')"` → `ok`.

- [ ] **Step 9: Commit**

```bash
git add app/routers/api.py tests/test_telegram_module_api.py
git commit -m "feat(telegram): API онбординга, обзор аккаунтов с живостью, pool-mode, переназначение"
```

---

### Task 8: Вход аккаунта по QR-коду

Спека §6 «Подключение аккаунта». QR-сессии живут в памяти процесса API:
`ParseJob.target_id` — `NOT NULL`, у входа нет таргета, воркер не подходит; а
клиент, ждущий скан, и запрос с 2FA-паролем должны быть в одном процессе.
Потолок — один uvicorn-воркер (так в compose).

**Files:**
- Create: `app/services/telegram_qr_login.py`
- Modify: `app/config.py` (`telegram_qr_login_ttl_seconds`), `requirements.txt` (`segno==1.6.6`), `app/routers/api.py`
- Test: `tests/test_telegram_qr_login.py`, `tests/test_telegram_qr_api.py`

**Interfaces:**
- Produces:
  - `async start_qr_login(*, api_id: int, api_hash: str, owner_user_id: int, label: str, hourly_limit: int, client_factory=...) -> QrSession`
  - `get_qr_session(token: str) -> QrSession | None`
  - `submit_password(token: str, password: str) -> bool`
  - `async cancel_qr_login(token: str) -> None`
  - `public_view(s: QrSession) -> dict` → `{token, status, url, qr_svg, error, account_id, expires_at}`
  - `QrSession.status ∈ {"pending", "password_needed", "done", "expired", "error"}`
  - Эндпоинты: `POST /modules/telegram/accounts/qr-login/start`, `GET /modules/telegram/accounts/qr-login/{token}`, `POST …/qr-login/{token}/password`, `POST …/qr-login/{token}/cancel`.

- [ ] **Step 1: Dependency and setting**

Run: `pip install segno==1.6.6` — **это единственная задача плана, где `pip install` разрешён.** Добавить строку `segno==1.6.6` в `requirements.txt`. В `app/config.py` рядом с `telegram_join_*`:

```python
    telegram_qr_login_ttl_seconds: int = 180
```

- [ ] **Step 2: Write the failing unit tests**

Создать `tests/test_telegram_qr_login.py`:

```python
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from telethon.errors import SessionPasswordNeededError

from app.services import telegram_qr_login as qr


class _FakeQr:
    """Scripted QRLogin: each wait() pops the next outcome."""

    def __init__(self, outcomes: list[str]):
        self.outcomes = list(outcomes)
        self.recreates = 0
        self.url = "tg://login?token=T0"
        self.expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)

    async def wait(self, timeout=None):
        outcome = self.outcomes.pop(0)
        if outcome == "timeout":
            raise asyncio.TimeoutError
        if outcome == "password":
            raise SessionPasswordNeededError(request=None)
        return SimpleNamespace(id=1)

    async def recreate(self):
        self.recreates += 1
        self.url = f"tg://login?token=T{self.recreates}"
        self.expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)


class _FakeClient:
    def __init__(self, fake_qr: _FakeQr):
        self._qr = fake_qr
        self.signed_in_with: str | None = None
        self.disconnected = False
        self.session = SimpleNamespace(save=lambda: "SESSION-STRING")

    async def connect(self): ...
    async def disconnect(self): self.disconnected = True
    async def qr_login(self): return self._qr
    async def sign_in(self, password=None): self.signed_in_with = password
    async def get_me(self): return SimpleNamespace(username="Alice", phone="380671234567", id=42)


def _start(outcomes, **kw):
    fake = _FakeQr(outcomes)
    client = _FakeClient(fake)

    async def go():
        s = await qr.start_qr_login(
            api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
            client_factory=lambda api_id, api_hash: client, **kw,
        )
        return s

    return asyncio.run(_wrap(go, fake, client))


async def _wrap(go, fake, client):
    s = await go()
    return s, fake, client


def _finish(s):
    """Await the background task inside a fresh loop (tests are sync)."""
    async def wait():
        await s._task
    asyncio.run(wait())


def test_start_exposes_url_and_svg():
    async def go():
        fake = _FakeQr(["ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        assert s.status == "pending"
        assert s.url == "tg://login?token=T0"
        assert s.qr_svg.startswith("data:image/svg+xml")
        assert qr.get_qr_session(s.token) is s
        await s._task
        return s, client

    s, client = asyncio.run(go())
    assert s.status == "done"
    assert s.session_string == "SESSION-STRING"
    assert s.me["username"] == "alice"
    assert client.disconnected is True


def test_token_timeout_recreates_and_updates_url():
    async def go():
        fake = _FakeQr(["timeout", "timeout", "ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s, fake

    s, fake = asyncio.run(go())
    assert fake.recreates == 2
    assert s.url == "tg://login?token=T2"
    assert s.status == "done"


def test_password_needed_then_submit_signs_in():
    async def go():
        fake = _FakeQr(["password"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        for _ in range(50):
            if s.status == "password_needed":
                break
            await asyncio.sleep(0.01)
        assert s.status == "password_needed"
        assert qr.submit_password(s.token, "cloud-pw") is True
        await s._task
        return s, client

    s, client = asyncio.run(go())
    assert client.signed_in_with == "cloud-pw"
    assert s.status == "done"


def test_submit_password_when_not_needed_is_rejected():
    async def go():
        fake = _FakeQr(["ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s

    s = asyncio.run(go())
    assert qr.submit_password(s.token, "x") is False
    assert qr.submit_password("no-such-token", "x") is False


def test_ttl_expiry_marks_expired(monkeypatch):
    monkeypatch.setattr(qr.settings, "telegram_qr_login_ttl_seconds", 0)

    async def go():
        fake = _FakeQr(["timeout"] * 5)
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s, client

    s, client = asyncio.run(go())
    assert s.status == "expired"
    assert client.disconnected is True


def test_cancel_removes_session():
    async def go():
        fake = _FakeQr(["timeout"] * 100)
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await qr.cancel_qr_login(s.token)
        return s, client

    s, client = asyncio.run(go())
    assert qr.get_qr_session(s.token) is None
    assert client.disconnected is True


def test_public_view_never_leaks_session_string():
    async def go():
        fake = _FakeQr(["ok"])
        client = _FakeClient(fake)
        s = await qr.start_qr_login(api_id=1, api_hash="h", owner_user_id=7, label="acc", hourly_limit=120,
                                    client_factory=lambda a, b: client)
        await s._task
        return s

    s = asyncio.run(go())
    view = qr.public_view(s)
    assert set(view) == {"token", "status", "url", "qr_svg", "error", "account_id", "expires_at"}
    assert "SESSION-STRING" not in str(view)
```

Удалить из файла вспомогательные `_start`/`_wrap`/`_finish`, если после написания они не используются — они здесь как черновик, тесты выше самодостаточны.

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/test_telegram_qr_login.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.telegram_qr_login'`.

- [ ] **Step 4: Write the service**

Создать `app/services/telegram_qr_login.py`:

```python
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

import segno
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ponytail: in-process registry; one uvicorn worker (as in compose). Move to
# Redis/DB if the API ever runs with several workers.
_SESSIONS: dict[str, "QrSession"] = {}
_PRUNE_AFTER = dt.timedelta(minutes=15)

ClientFactory = Callable[[int, str], Any]


@dataclass
class QrSession:
    token: str
    api_id: int
    api_hash: str
    owner_user_id: int
    label: str
    hourly_limit: int
    status: str = "pending"
    url: str | None = None
    qr_svg: str | None = None
    error: str | None = None
    session_string: str | None = None
    me: dict[str, Any] = field(default_factory=dict)
    account_id: int | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    expires_at: dt.datetime | None = None
    _client: Any = None
    _qr: Any = None
    _password: asyncio.Future | None = None
    _task: asyncio.Task | None = None


def _default_client_factory(api_id: int, api_hash: str) -> Any:
    return TelegramClient(StringSession(), api_id, api_hash)


def _svg(url: str) -> str:
    return segno.make(url, error="m").svg_data_uri(scale=6, border=1)


def _prune(now: dt.datetime) -> None:
    for token in [t for t, s in _SESSIONS.items() if now - s.created_at > _PRUNE_AFTER]:
        _SESSIONS.pop(token, None)


async def start_qr_login(
    *,
    api_id: int,
    api_hash: str,
    owner_user_id: int,
    label: str,
    hourly_limit: int,
    client_factory: ClientFactory = _default_client_factory,
) -> QrSession:
    now = dt.datetime.now(dt.UTC)
    _prune(now)
    s = QrSession(
        token=secrets.token_urlsafe(24),
        api_id=int(api_id),
        api_hash=str(api_hash),
        owner_user_id=int(owner_user_id),
        label=label,
        hourly_limit=int(hourly_limit),
        expires_at=now + dt.timedelta(seconds=int(settings.telegram_qr_login_ttl_seconds)),
    )
    s._client = client_factory(s.api_id, s.api_hash)
    await s._client.connect()
    s._qr = await s._client.qr_login()
    s.url = s._qr.url
    s.qr_svg = _svg(s.url)
    s._task = asyncio.create_task(_run(s))
    _SESSIONS[s.token] = s
    logger.info("qr-login started token=%s owner=%s label=%s", s.token[:8], owner_user_id, label)
    return s


async def _run(s: QrSession) -> None:
    try:
        while True:
            now = dt.datetime.now(dt.UTC)
            remaining = (s.expires_at - now).total_seconds()
            if remaining <= 0:
                s.status = "expired"
                return
            token_left = max((s._qr.expires - now).total_seconds(), 0.05)
            try:
                await s._qr.wait(timeout=min(token_left, remaining))
            except asyncio.TimeoutError:
                await s._qr.recreate()
                s.url = s._qr.url
                s.qr_svg = _svg(s.url)
                continue
            except SessionPasswordNeededError:
                s.status = "password_needed"
                s._password = asyncio.get_running_loop().create_future()
                try:
                    password = await asyncio.wait_for(s._password, timeout=max(remaining, 30))
                except asyncio.TimeoutError:
                    s.status = "expired"
                    return
                await s._client.sign_in(password=password)
            break

        me = await s._client.get_me()
        s.me = {
            "username": (str(getattr(me, "username", "") or "").strip().lower() or None),
            "phone": (str(getattr(me, "phone", "") or "").strip() or None),
            "user_id": int(getattr(me, "id", 0) or 0) or None,
        }
        s.session_string = s._client.session.save()
        s.status = "done"
        logger.info("qr-login done token=%s user=%s", s.token[:8], s.me.get("username"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        s.status = "error"
        s.error = str(exc)[:500]
        logger.warning("qr-login error token=%s: %s", s.token[:8], exc)
    finally:
        try:
            await s._client.disconnect()
        except Exception:
            pass


def get_qr_session(token: str) -> QrSession | None:
    return _SESSIONS.get(str(token or ""))


def submit_password(token: str, password: str) -> bool:
    s = get_qr_session(token)
    if s is None or s.status != "password_needed" or s._password is None or s._password.done():
        return False
    s._password.set_result(password)
    return True


async def cancel_qr_login(token: str) -> None:
    s = _SESSIONS.pop(str(token or ""), None)
    if s is None:
        return
    if s._task and not s._task.done():
        s._task.cancel()
        try:
            await s._task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        await s._client.disconnect()
    except Exception:
        pass


def public_view(s: QrSession) -> dict[str, Any]:
    return {
        "token": s.token,
        "status": s.status,
        "url": s.url,
        "qr_svg": s.qr_svg,
        "error": s.error,
        "account_id": s.account_id,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
    }
```

- [ ] **Step 5: Run unit tests**

Run: `pytest tests/test_telegram_qr_login.py -v`
Expected: PASS (7 passed). Если `SessionPasswordNeededError(request=None)` не конструируется — проверить сигнатуру в установленном Telethon и поправить **фикстуру**, не сервис.

- [ ] **Step 6: Write the failing API tests**

Создать `tests/test_telegram_qr_api.py` (фабрика `client`/`User` — как в `tests/test_telegram_module_api.py`):

```python
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import ParserAccount, User
from app.services import telegram_qr_login as qr

pytestmark = pytest.mark.postgres


class _Qr:
    def __init__(self):
        self.url = "tg://login?token=X"
        import datetime as dt
        self.expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
    async def wait(self, timeout=None): return SimpleNamespace(id=1)
    async def recreate(self): ...


class _Client:
    def __init__(self):
        self.session = SimpleNamespace(save=lambda: "SESSION-STRING")
    async def connect(self): ...
    async def disconnect(self): ...
    async def qr_login(self): return _Qr()
    async def sign_in(self, password=None): ...
    async def get_me(self): return SimpleNamespace(username="alice", phone="380671234567", id=42)


@pytest.fixture
def client(pg_session, monkeypatch):
    admin = User(username="adm", password_hash="x", role="admin", is_admin=True, is_active=True,
                 totp_enabled=True, totp_confirmed=True)
    pg_session.add(admin)
    pg_session.commit()
    monkeypatch.setattr(qr, "_default_client_factory", lambda a, b: _Client())
    app.dependency_overrides[get_db] = lambda: pg_session
    app.dependency_overrides[get_current_user] = lambda: admin
    yield TestClient(app), pg_session
    app.dependency_overrides.clear()
    qr._SESSIONS.clear()


def test_start_then_status_creates_account_once(client):
    c, session = client
    started = c.post("/api/modules/telegram/accounts/qr-login/start",
                     json={"api_id": 1, "api_hash": "h", "label": "qr-acc"}).json()
    assert started["status"] == "pending"
    assert started["qr_svg"].startswith("data:image/svg+xml")
    token = started["token"]

    s = qr.get_qr_session(token)
    asyncio.get_event_loop_policy().new_event_loop()  # no-op guard for sync test
    # Let the background task finish in the app's loop by polling the endpoint.
    for _ in range(50):
        view = c.get(f"/api/modules/telegram/accounts/qr-login/{token}").json()
        if view["status"] == "done":
            break
    assert view["status"] == "done"
    assert view["account_id"] is not None

    again = c.get(f"/api/modules/telegram/accounts/qr-login/{token}").json()
    assert again["account_id"] == view["account_id"]
    accounts = session.execute(select(ParserAccount).where(ParserAccount.label == "qr-acc")).scalars().all()
    assert len(accounts) == 1
    creds = accounts[0].credentials
    assert creds["session_string"] == "SESSION-STRING"
    assert creds["api_id"] == "1" and creds["api_hash"] == "h"
    assert creds["username"] == "alice" and creds["phone"] == "380671234567"
    assert creds["session_status"]["alive"] is True
    assert "SESSION-STRING" not in c.get(f"/api/modules/telegram/accounts/qr-login/{token}").text


def test_duplicate_label_rejected(client):
    c, session = client
    session.add(ParserAccount(parser_type="telegram", label="taken", credentials={}))
    session.commit()
    resp = c.post("/api/modules/telegram/accounts/qr-login/start", json={"api_id": 1, "api_hash": "h", "label": "taken"})
    assert resp.status_code == 400


def test_password_endpoint_rejects_when_not_needed(client):
    c, _ = client
    token = c.post("/api/modules/telegram/accounts/qr-login/start",
                   json={"api_id": 1, "api_hash": "h", "label": "a2"}).json()["token"]
    assert c.post(f"/api/modules/telegram/accounts/qr-login/{token}/password", json={"password": "x"}).status_code == 400


def test_unknown_token_is_404(client):
    c, _ = client
    assert c.get("/api/modules/telegram/accounts/qr-login/nope").status_code == 404


def test_cancel(client):
    c, _ = client
    token = c.post("/api/modules/telegram/accounts/qr-login/start",
                   json={"api_id": 1, "api_hash": "h", "label": "a3"}).json()["token"]
    assert c.post(f"/api/modules/telegram/accounts/qr-login/{token}/cancel").status_code == 200
    assert c.get(f"/api/modules/telegram/accounts/qr-login/{token}").status_code == 404
```

Убрать строку с `new_event_loop()` — она лишняя; опрос эндпоинта уже даёт фоновой задаче выполниться, потому что `TestClient` гоняет приложение в своём цикле событий. Если задача не успевает завершиться за 50 опросов — добавить `time.sleep(0.02)` внутри цикла.

- [ ] **Step 7: Add the endpoints**

В `app/routers/api.py` — **до** любого `GET /modules/telegram/accounts/{account_id}/...` (иначе FastAPI попробует разобрать `qr-login` как `account_id: int` и вернёт 422, а не пойдёт дальше):

```python
class QrLoginStartRequest(BaseModel):
    api_id: int
    api_hash: str
    label: str
    hourly_limit: int = 120


class QrLoginPasswordRequest(BaseModel):
    password: str


def _qr_session_for(token: str, user: User):
    s = telegram_qr_login.get_qr_session(token)
    if s is None:
        raise HTTPException(status_code=404, detail="QR-сесію не знайдено або вона завершилась")
    if not _is_admin(user) and s.owner_user_id != int(user.id):
        raise HTTPException(status_code=403, detail="Немає доступу")
    return s


@router.post("/modules/telegram/accounts/qr-login/start")
async def telegram_qr_login_start(payload: QrLoginStartRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    label = payload.label.strip()
    if not label or not payload.api_hash.strip():
        raise HTTPException(status_code=400, detail="Поля api_id, api_hash і мітка обов'язкові")
    if db.execute(select(ParserAccount).where(ParserAccount.label == label)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Акаунт з такою міткою вже існує")
    try:
        s = await telegram_qr_login.start_qr_login(
            api_id=payload.api_id, api_hash=payload.api_hash.strip(), owner_user_id=int(user.id),
            label=label, hourly_limit=max(int(payload.hourly_limit), 1),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не вдалося почати QR-вхід: {exc}") from exc
    return telegram_qr_login.public_view(s)


@router.get("/modules/telegram/accounts/qr-login/{token}")
async def telegram_qr_login_status(token: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    s = _qr_session_for(token, user)
    if s.status == "done" and s.account_id is None:
        account = ParserAccount(
            parser_type=ParserType.telegram,
            label=s.label,
            owner_user_id=s.owner_user_id,
            credentials={
                "api_id": str(s.api_id),
                "api_hash": s.api_hash,
                "phone": s.me.get("phone"),
                "username": s.me.get("username"),
                "session_string": s.session_string,
                "session_status": {
                    "alive": True,
                    "is_authorized": True,
                    "phone": s.me.get("phone"),
                    "checked_at": dt.datetime.now(dt.UTC).isoformat(),
                },
            },
            hourly_limit=s.hourly_limit,
            alive=True,
            last_checked_at=dt.datetime.now(dt.UTC),
            last_alive_at=dt.datetime.now(dt.UTC),
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        s.account_id = int(account.id)
        s.session_string = None  # not needed in memory once persisted
    return telegram_qr_login.public_view(s)


@router.post("/modules/telegram/accounts/qr-login/{token}/password")
async def telegram_qr_login_password(token: str, payload: QrLoginPasswordRequest, user=Depends(get_current_user)):
    _qr_session_for(token, user)
    if not telegram_qr_login.submit_password(token, payload.password):
        raise HTTPException(status_code=400, detail="Пароль зараз не потрібен")
    return {"ok": True}


@router.post("/modules/telegram/accounts/qr-login/{token}/cancel")
async def telegram_qr_login_cancel(token: str, user=Depends(get_current_user)):
    _qr_session_for(token, user)
    await telegram_qr_login.cancel_qr_login(token)
    return {"ok": True}
```

Импорт: `from app.services import telegram_qr_login`. Эндпоинты **`async def`** — фоновая задача должна жить в цикле uvicorn; остальные обработчики файла синхронные, это осознанное исключение.

- [ ] **Step 8: Run all tests**

Run: `TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" pytest tests/test_telegram_qr_login.py tests/test_telegram_qr_api.py -v`
Expected: PASS (12 passed). Затем полный прогон: 0 failed.

- [ ] **Step 9: Commit**

```bash
git add app/services/telegram_qr_login.py app/routers/api.py app/config.py requirements.txt tests/test_telegram_qr_login.py tests/test_telegram_qr_api.py
git commit -m "feat(telegram): вход аккаунта по QR-коду — сессии в процессе API, 2FA-пароль, segno"
```

---

## Self-Review

**Покрытие спеки:**

| Спека | Задача |
|---|---|
| §1 колонки `ParserAccount`/`Target`, дедупликация, частичный индекс | Task 1 |
| §2 нормализация приглашений `invite:<hash>` | Task 2 |
| §2 таргет создаётся сразу в `queued`, задание в очередь | Task 4 (`onboard`) |
| §2 выбор аккаунта: shared, живой, доступный, предпочтение состоящим | Task 4 (`pick_account`) |
| §2 темп: 900 с между вступлениями, 10 в сутки | Task 4 (`join_pacing_wait_seconds`) |
| §2 ветки ошибок Telethon | Task 4 (`run_onboard_job`) |
| §2 канонический identifier после приглашения, `invite_hash`, `kind` | Task 4 |
| §2 связь + гашение прежних; пауза 2–5 мин | Task 4 (`_link`, `quiet_until`), Task 5 (`generate_jobs`) |
| §2 старые формы поверх `onboard()` | Task 7 шаг 6 |
| §3 проверка в `schedule_once`, гейт по интервалу | Task 6 |
| §3 правило двух неудач | Task 6 |
| §3 failover shared / dedicated; мёртвый остаётся `is_active` | Task 6 |
| §4 дефолты автоподключённого таргета | Task 4 (`default_onboard_config`) |
| §4 продолжение full-бэкфилла с `next_offset_id` предыдущего аккаунта | уже так работает: `full_progress` per account читается по `account_id` — новый аккаунт начнёт с начала, что спека допускает |
| §5 dedicated исключён из выбора и failover; ручное назначение с `allow_join` | Task 4, Task 6, Task 7 (`/onboard` с `account_id`) |
| §7 эндпоинты | Task 7 |
| §8 логирование переходов | Tasks 4, 6 |
| «отложить без штрафа» | Task 3 |

**Плейсхолдеры:** нет. Единственное «свериться с сигнатурой» — конструктор `FloodWaitError` в фикстуре теста (Task 4, шаг 5), с готовой командой проверки.

**Согласованность имён:** `run_onboard_job(session, job, target, *, client_factory)` — Task 4 определяет, Task 5 вызывает как `run_onboard_job(session, job, target)`. `onboard(session, *, raw_input, owner_user_id, account_id, allow_join)` — Task 4 / Task 6 (не вызывает, ставит `JobSpec` напрямую с тем же `payload`) / Task 7. `DeferJob(seconds, reason)` — Task 3 / Task 4. `JobSpec.run_after` — Task 3; в Task 4/6 не используется (отложение идёт через `DeferJob`), оставлен для планировщика.

**Известный риск:** Task 6 импортирует `_enqueue_job_specs` из `scheduler`, а `scheduler` вызывает `check_accounts_liveness` — цикл разорван локальным импортом внутри `schedule_once` (шаг 4). Исполнитель Task 6 должен не «улучшить» это, подняв импорт наверх.
