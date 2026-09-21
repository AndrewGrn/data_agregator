# Ядро: нормализованные события Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести события парсеров на нормализованный контракт и хранение payload в Postgres JSONB, убрав S3 из пути JSON.

**Architecture:** `ParsedEvent` получает нормализованные поля (text, author_id, event_kind, thread_id, files) поверх сырого payload. `parser_type` перестаёт быть enum в БД и становится строкой, источник истины — реестр плагинов. `raw_events.payload` — JSONB NOT NULL вместо ссылки на S3. Запись событий выносится из `worker.py` в единую функцию `persist_events`, которой пользуются и воркер, и листенеры.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, Alembic, PostgreSQL 16, pytest

**Spec:** `docs/superpowers/specs/2026-09-17-unified-parser-architecture-design.md`

## Global Constraints

- Накопленные данные не переносятся: `raw_events` пересоздаётся. Таблицы `targets`, `parser_accounts`, `parse_jobs` сохраняют данные — их `parser_type` меняется через `ALTER TYPE ... USING`.
- `payload` объявляется `NOT NULL`. Событие без сырого JSON не сохраняется.
- Уникальный индекс `uq_raw_events_parser_target_external` (`parser_type, target_id, external_id`) сохраняется — это механизм дедупликации.
- Существующие тесты работают на SQLite in-memory (`tests/test_darknet_incremental.py`, `test_telegram_offsets.py`, `test_telegram_profiles.py`, `test_scheduler.py`). JSONB на SQLite не существует, поэтому колонка объявляется как `JSONB().with_variant(JSON, "sqlite")`.
- Колонка `text_search` (`tsvector`, GENERATED ALWAYS) создаётся только в миграции и **отсутствует в модели** — Postgres поддерживает её сам, Python в неё не пишет и напрямую не читает. Это сохраняет работоспособность SQLite-тестов.
- Значения `event_kind` на старте: `message`, `comment`, `post`, `reply`.
- Новые/изменённые файлы форматируются под стиль проекта: `from __future__ import annotations` первой строкой, типы в сигнатурах, отступ 4 пробела.

---

## File Structure

**Создаются:**
- `app/services/event_sink.py` — единая запись событий (`persist_events`), ~90 строк. Отдельный модуль, а не функция в `worker.py`, потому что вызывается тремя потребителями: воркером, telegram-листенером и (в плане 4) whatsapp-листенером.
- `alembic/versions/0014_normalized_events.py` — миграция схемы.
- `tests/test_event_sink.py` — тесты записи и дедупликации.
- `tests/test_normalization.py` — тесты маппинга плагинов в нормализованные поля.

**Изменяются:**
- `app/models.py` — `ParserTypeStr`, колонки `parser_type`, новая `RawEvent`, модели `EventFile`/`RawEventFile`.
- `app/plugins/base.py` — `FileRef`, расширенный `ParsedEvent`.
- `app/plugins/registry.py` — валидация типов.
- `app/plugins/telegram.py` — заполнение нормализованных полей.
- `app/plugins/darknet.py` — заполнение нормализованных полей.
- `app/services/worker.py` — переход на `persist_events`.
- `app/services/telegram_listener.py` — переход на `persist_events`.
- `app/services/object_store.py` — удаление `put_json`/`get_json`.
- `app/routers/api.py`, `app/routers/ui.py` — 13 вызовов `get_json` → `event.payload`.

---

### Task 1: `parser_type` как строка

Колонка перестаёт быть enum. `ParserType` остаётся в коде как набор констант — удалять его не нужно, он используется в 144 местах.

**Ловушка:** `ParserType(str, enum.Enum)` — не `StrEnum`. В Python 3.12 `str(ParserType.telegram)` возвращает `"ParserType.telegram"`, а не `"telegram"`. Запись enum-члена в `String`-колонку испортит данные. `TypeDecorator` снимает проблему во всех 144 местах разом, без их аудита.

**Files:**
- Modify: `app/models.py` (добавить `ParserTypeStr`, заменить 4 колонки)
- Test: `tests/test_parser_type_column.py`

**Interfaces:**
- Produces: `ParserTypeStr` — SQLAlchemy-тип для колонок `parser_type`; принимает `ParserType` и `str`, в БД пишет строковое значение.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_parser_type_column.py`:

```python
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ParserType, Target


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_enum_member_is_stored_as_plain_value():
    session = _session()
    session.add(Target(parser_type=ParserType.telegram, name="t", identifier="@x"))
    session.commit()

    stored = session.execute(
        Target.__table__.select().with_only_columns(Target.__table__.c.parser_type)
    ).scalar_one()
    assert stored == "telegram"


def test_plain_string_is_accepted():
    session = _session()
    session.add(Target(parser_type="whatsapp", name="t", identifier="1@c.us"))
    session.commit()

    stored = session.execute(
        Target.__table__.select().with_only_columns(Target.__table__.c.parser_type)
    ).scalar_one()
    assert stored == "whatsapp"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parser_type_column.py -v`
Expected: FAIL — `test_plain_string_is_accepted` падает с `LookupError: 'whatsapp' is not among the defined enum values`.

- [ ] **Step 3: Add the TypeDecorator**

В `app/models.py`, после блока импортов, до объявления `ParserType`:

```python
from sqlalchemy import TypeDecorator


class ParserTypeStr(TypeDecorator):
    """String column accepting both ParserType members and plain strings.

    ParserType is `(str, Enum)`, not StrEnum, so str() on a member yields
    "ParserType.telegram". Coercing on bind keeps the stored value plain.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return getattr(value, "value", value)
```

- [ ] **Step 4: Replace the four columns**

В `app/models.py` заменить тип колонки в четырёх местах. Было `mapped_column(Enum(ParserType), index=True)`, стало:

```python
# class ParserAccount (строка ~103)
parser_type: Mapped[str] = mapped_column(ParserTypeStr, index=True)

# class Target (строка ~126)
parser_type: Mapped[str] = mapped_column(ParserTypeStr, index=True)

# class ParseJob (найти по `parser_type: Mapped[ParserType]`)
parser_type: Mapped[str] = mapped_column(ParserTypeStr, index=True)

# class RawEvent (строка ~194)
parser_type: Mapped[str] = mapped_column(ParserTypeStr, index=True)
```

`ParserType` не удалять — класс остаётся набором констант.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_parser_type_column.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Fix `.value` on model instances**

Колонка теперь отдаёт `str`, у которого нет атрибута `.value`. Обращения вида `target.parser_type.value` упадут с `AttributeError` — в шаблонах только в рантайме, при рендере страницы.

Найти все вхождения:

```bash
cd /Users/andriihrenchyshen/data_agregator && grep -rn "\.parser_type\.value" app/ | grep -v __pycache__ | grep -v "ParserType\."
```

Ожидается 21 вхождение. Убрать `.value` в каждом — остаётся просто `.parser_type`:

- `app/routers/api.py:1033` — `"parser_type": a.parser_type`
- `app/routers/api.py:3266` — `if parser_filter and target.parser_type != parser_filter:`
- `app/routers/api.py:3268` — `parser_filter = target.parser_type`
- `app/routers/api.py:4581` — `"parser_type": e.parser_type`
- `app/routers/ui.py:535` — `"parser_type": event.parser_type`
- `app/services/worker.py:61` — `if account and account.parser_type == "telegram":`
- `app/services/worker.py:211` — `job.parser_type == "telegram"`
- `app/services/worker.py:292` — `parser_type_value = job.parser_type`
- `app/services/worker.py:346`, `:371` — `parser_type=job.parser_type`
- `app/services/scheduler.py:85`, `:107` — `plugin_registry.get(target.parser_type)`
- `app/services/scheduler.py:225` — `"parser_type": job.parser_type`
- `app/templates/targets.html:71,77,104` — `{{ t.parser_type }}`, `{{ a.parser_type }}`
- `app/templates/data.html:200` — `{{ selected_event.parser_type }}`

Вхождения вида `ParserType.telegram.value` (на самом классе, не на экземпляре) **не трогать** — они продолжают работать.

- [ ] **Step 7: Verify nothing remains**

Run:
```bash
grep -rn "\.parser_type\.value" app/ | grep -v __pycache__ | grep -v "ParserType\."
```
Expected: пусто.

- [ ] **Step 8: Run the existing suite**

Run: `pytest tests/ -v`
Expected: PASS. Сравнения вида `RawEvent.parser_type == ParserType.telegram` продолжают работать: `ParserType` наследует `str`, и `"telegram" == ParserType.telegram` истинно.

- [ ] **Step 9: Commit**

```bash
git add app/models.py app/routers/ app/services/ app/templates/ tests/test_parser_type_column.py
git commit -m "feat: parser_type как строковая колонка вместо enum"
```

---

### Task 2: Контракт `ParsedEvent`

**Files:**
- Modify: `app/plugins/base.py`
- Modify: `app/plugins/registry.py`
- Test: `tests/test_plugin_contract.py`

**Interfaces:**
- Produces:
  - `FileRef(source_ref: str, filename: str | None, mime: str | None, size: int | None, sha256: str | None)`
  - `ParsedEvent(external_id, observed_at, payload, text=None, author_id=None, author_label=None, event_kind="message", thread_id=None, reply_to=None, files=[])`
  - `plugin_registry.is_known(parser_type: str) -> bool`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_plugin_contract.py`:

```python
from __future__ import annotations

import datetime as dt

from app.plugins.base import FileRef, ParsedEvent
from app.plugins.registry import plugin_registry


def test_parsed_event_defaults():
    event = ParsedEvent(external_id="x1", observed_at=None, payload={"a": 1})

    assert event.event_kind == "message"
    assert event.text is None
    assert event.files == []


def test_parsed_event_files_are_not_shared_between_instances():
    first = ParsedEvent(external_id="a", observed_at=None, payload={})
    second = ParsedEvent(external_id="b", observed_at=None, payload={})
    first.files.append(FileRef(source_ref="f1"))

    assert second.files == []


def test_parsed_event_accepts_normalized_fields():
    now = dt.datetime.now(dt.UTC)
    event = ParsedEvent(
        external_id="m1",
        observed_at=now,
        payload={"raw": True},
        text="привет",
        author_id="380671234567@c.us",
        author_label="Andrii",
        event_kind="reply",
        thread_id="120363@g.us",
        reply_to="m0",
        files=[FileRef(source_ref="m1", mime="image/jpeg", size=100, sha256="ab")],
    )

    assert event.author_id == "380671234567@c.us"
    assert event.files[0].sha256 == "ab"


def test_registry_knows_registered_types():
    assert plugin_registry.is_known("telegram") is True
    assert plugin_registry.is_known("nonexistent") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plugin_contract.py -v`
Expected: FAIL — `ImportError: cannot import name 'FileRef'`

- [ ] **Step 3: Extend the contract**

В `app/plugins/base.py` заменить `ParsedEvent` и добавить `FileRef`. Импорт `field` обязателен — без него мутабельный дефолт разделяется между экземплярами:

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class FileRef:
    """Attachment reference. sha256 is set when the file is already in S3."""

    source_ref: str
    filename: str | None = None
    mime: str | None = None
    size: int | None = None
    sha256: str | None = None


@dataclass(slots=True)
class ParsedEvent:
    external_id: str | None
    observed_at: dt.datetime | None
    payload: dict
    text: str | None = None
    author_id: str | None = None
    author_label: str | None = None
    event_kind: str = "message"
    thread_id: str | None = None
    reply_to: str | None = None
    files: list[FileRef] = field(default_factory=list)
```

- [ ] **Step 4: Add registry validation**

В `app/plugins/registry.py`, в класс `PluginRegistry`:

```python
    def is_known(self, parser_type: str) -> bool:
        return str(parser_type) in self._plugins
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_plugin_contract.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add app/plugins/base.py app/plugins/registry.py tests/test_plugin_contract.py
git commit -m "feat: нормализованные поля и FileRef в контракте плагина"
```

---

### Task 3: Схема `RawEvent` и таблицы файлов

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_event_models.py`

**Interfaces:**
- Consumes: `ParserTypeStr` (Task 1)
- Produces: `RawEvent` с полями `payload`, `text`, `author_id`, `author_label`, `event_kind`, `thread_id`, `reply_to`; модели `EventFile(sha256, storage_key, mime, size, filename)`, `RawEventFile(raw_event_id, file_id, source_ref, position)`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_event_models.py`:

```python
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import EventFile, RawEvent, RawEventFile, Target


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _target(session):
    target = Target(parser_type="telegram", name="chan", identifier="@chan")
    session.add(target)
    session.commit()
    return target


def test_raw_event_stores_normalized_fields():
    session = _session()
    target = _target(session)
    session.add(
        RawEvent(
            parser_type="telegram",
            target_id=target.id,
            external_id="42",
            observed_at=dt.datetime.now(dt.UTC),
            payload={"event_type": "telegram_message", "text": "hello"},
            text="hello",
            author_id="777",
            author_label="@alice",
            event_kind="message",
            thread_id="@chan",
        )
    )
    session.commit()

    stored = session.query(RawEvent).one()
    assert stored.payload["text"] == "hello"
    assert stored.author_id == "777"
    assert stored.event_kind == "message"


def test_payload_is_required():
    session = _session()
    target = _target(session)
    session.add(RawEvent(parser_type="telegram", target_id=target.id, external_id="1"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_one_file_links_to_many_events():
    session = _session()
    target = _target(session)
    first = RawEvent(parser_type="telegram", target_id=target.id, external_id="1", payload={})
    second = RawEvent(parser_type="telegram", target_id=target.id, external_id="2", payload={})
    file = EventFile(sha256="deadbeef", storage_key="media/deadbeef", mime="image/jpeg", size=10)
    session.add_all([first, second, file])
    session.flush()
    session.add_all(
        [
            RawEventFile(raw_event_id=first.id, file_id=file.id, source_ref="a", position=0),
            RawEventFile(raw_event_id=second.id, file_id=file.id, source_ref="b", position=0),
        ]
    )
    session.commit()

    assert session.query(EventFile).count() == 1
    assert session.query(RawEventFile).count() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_event_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'EventFile'`

- [ ] **Step 3: Rewrite RawEvent and add file models**

В `app/models.py` добавить импорт JSONB рядом с существующими импортами SQLAlchemy:

```python
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

JSONB_OR_JSON = JSONB().with_variant(JSON, "sqlite")
```

Заменить класс `RawEvent` целиком (было: строки 190–208):

```python
class RawEvent(Base):
    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_type: Mapped[str] = mapped_column(ParserTypeStr, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("parser_accounts.id", ondelete="SET NULL"), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB_OR_JSON, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    author_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_kind: Mapped[str] = mapped_column(String(32), default="message", index=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    reply_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)

    target: Mapped[Target] = relationship(back_populates="events")


class EventFile(Base):
    __tablename__ = "event_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    storage_key: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class RawEventFile(Base):
    __tablename__ = "raw_event_files"

    raw_event_id: Mapped[int] = mapped_column(ForeignKey("raw_events.id", ondelete="CASCADE"), primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("event_files.id", ondelete="CASCADE"), primary_key=True)
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
```

Проверить, что `Text` есть в импортах `sqlalchemy` в начале файла; если нет — добавить.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_event_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_event_models.py
git commit -m "feat: RawEvent на JSONB, таблицы event_files и raw_event_files"
```

---

### Task 4: Миграция 0014

`raw_events` пересоздаётся (чистый старт). `targets`, `parser_accounts`, `parse_jobs` сохраняют данные — их колонка конвертируется через `USING`.

**Files:**
- Create: `alembic/versions/0014_normalized_events.py`

**Interfaces:**
- Consumes: модели из Task 3
- Produces: схема БД, включая `text_search` (`tsvector`) — колонка используется планом 2

- [ ] **Step 1: Confirm the current head**

Run: `cd /Users/andriihrenchyshen/data_agregator && ls alembic/versions/ | sort | tail -3`
Expected: последняя миграция — `0013_parse_job_dispatch_audit.py`. Открыть её и прочитать значение `revision =` — оно становится `down_revision` новой миграции.

- [ ] **Step 2: Write the migration**

Создать `alembic/versions/0014_normalized_events.py`:

```python
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
```

- [ ] **Step 3: Verify the migration applies**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && docker compose up -d db && sleep 5 && docker compose run --rm api python -m app.cli db-upgrade
```
Expected: миграция проходит без ошибок, в выводе присутствует `0014_normalized_events`.

- [ ] **Step 4: Verify the generated column works**

Run:
```bash
docker compose exec db psql -U postgres -d aggregator -c \
  "INSERT INTO raw_events (parser_type, target_id, payload, text, event_kind, created_at) \
   SELECT 'telegram', id, '{}'::jsonb, 'привет мир', 'message', now() FROM targets LIMIT 1; \
   SELECT text_search IS NOT NULL AS ok FROM raw_events LIMIT 1;"
```
Expected: `ok | t`. Если таблица `targets` пуста — сначала создать таргет через UI или пропустить эту проверку до Task 8.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0014_normalized_events.py
git commit -m "feat: миграция 0014 — JSONB payload, нормализованные колонки, таблицы файлов"
```

---

### Task 5: Единая запись событий

Сейчас запись размазана: `worker.py:336-400` и `telegram_listener.py:256-310` делают одно и то же разным кодом. Третий потребитель (whatsapp-листенер, план 4) стал бы третьей копией.

**Files:**
- Create: `app/services/event_sink.py`
- Test: `tests/test_event_sink.py`

**Interfaces:**
- Consumes: `ParsedEvent` (Task 2), `RawEvent` (Task 3)
- Produces: `persist_events(session, *, target, parser_type, account_id, owner_user_id, events) -> int` — возвращает число новых записей; дубликаты по `(parser_type, target_id, external_id)` пропускаются.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_event_sink.py`:

```python
from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import RawEvent, Target
from app.plugins.base import ParsedEvent
from app.services.event_sink import persist_events


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _target(session):
    target = Target(parser_type="telegram", name="chan", identifier="@chan")
    session.add(target)
    session.commit()
    return target


def _event(external_id: str, text: str = "hello") -> ParsedEvent:
    return ParsedEvent(
        external_id=external_id,
        observed_at=dt.datetime.now(dt.UTC),
        payload={"event_type": "telegram_message", "text": text},
        text=text,
        author_id="777",
        author_label="@alice",
        event_kind="message",
        thread_id="@chan",
    )


def test_persists_normalized_fields():
    session = _session()
    target = _target(session)

    written = persist_events(
        session,
        target=target,
        parser_type="telegram",
        account_id=None,
        owner_user_id=None,
        events=[_event("1")],
    )
    session.commit()

    assert written == 1
    stored = session.query(RawEvent).one()
    assert stored.text == "hello"
    assert stored.author_label == "@alice"
    assert stored.payload["event_type"] == "telegram_message"


def test_duplicate_external_id_is_skipped():
    session = _session()
    target = _target(session)

    persist_events(session, target=target, parser_type="telegram", account_id=None, owner_user_id=None, events=[_event("1")])
    session.commit()
    written = persist_events(session, target=target, parser_type="telegram", account_id=None, owner_user_id=None, events=[_event("1", text="changed")])
    session.commit()

    assert written == 0
    assert session.query(RawEvent).count() == 1


def test_event_without_external_id_is_always_written():
    session = _session()
    target = _target(session)

    persist_events(session, target=target, parser_type="darknet", account_id=None, owner_user_id=None, events=[
        ParsedEvent(external_id=None, observed_at=None, payload={"a": 1}),
    ])
    persist_events(session, target=target, parser_type="darknet", account_id=None, owner_user_id=None, events=[
        ParsedEvent(external_id=None, observed_at=None, payload={"a": 2}),
    ])
    session.commit()

    assert session.query(RawEvent).count() == 2


def test_owner_falls_back_to_target_owner():
    session = _session()
    target = _target(session)
    target.owner_user_id = 5
    session.commit()

    persist_events(session, target=target, parser_type="telegram", account_id=None, owner_user_id=None, events=[_event("1")])
    session.commit()

    assert session.query(RawEvent).one().owner_user_id == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_event_sink.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.event_sink'`

- [ ] **Step 3: Write the implementation**

Создать `app/services/event_sink.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import RawEvent, Target
from app.plugins.base import ParsedEvent


def persist_events(
    session: Session,
    *,
    target: Target,
    parser_type: str,
    account_id: int | None,
    owner_user_id: int | None,
    events: list[ParsedEvent],
) -> int:
    """Write parsed events, skipping ones already stored.

    Deduplication is by (parser_type, target_id, external_id); events without
    an external_id are always written. Returns the number of new rows.
    """
    if not events:
        return 0

    external_ids = [str(event.external_id) for event in events if event.external_id]
    known: set[str] = set()
    if external_ids:
        rows = session.execute(
            select(RawEvent.external_id).where(
                RawEvent.parser_type == parser_type,
                RawEvent.target_id == target.id,
                RawEvent.external_id.in_(external_ids),
            )
        ).all()
        known = {str(row[0]) for row in rows if row[0]}

    resolved_owner = owner_user_id if owner_user_id is not None else target.owner_user_id
    written = 0

    for event in events:
        if event.external_id and str(event.external_id) in known:
            continue

        raw_event = RawEvent(
            parser_type=parser_type,
            target_id=target.id,
            account_id=account_id,
            owner_user_id=resolved_owner,
            external_id=event.external_id,
            observed_at=event.observed_at,
            payload=event.payload if isinstance(event.payload, dict) else {},
            text=event.text,
            author_id=event.author_id,
            author_label=event.author_label,
            event_kind=event.event_kind,
            thread_id=event.thread_id,
            reply_to=event.reply_to,
        )
        try:
            # A savepoint keeps a concurrent duplicate from failing the batch.
            with session.begin_nested():
                session.add(raw_event)
                session.flush()
        except IntegrityError:
            continue

        if event.external_id:
            known.add(str(event.external_id))
        written += 1

    return written
```

Файлы (`event.files`) в этой функции пока не обрабатываются — это план 3.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_event_sink.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/event_sink.py tests/test_event_sink.py
git commit -m "feat: persist_events — единая запись событий"
```

---

### Task 6: Нормализация в Telegram-плагине

**Files:**
- Modify: `app/plugins/telegram.py:600-612`
- Test: `tests/test_normalization.py`

**Interfaces:**
- Consumes: `ParsedEvent` (Task 2)
- Produces: `normalize_telegram_payload(item: dict) -> dict` — словарь нормализованных полей, пригодный для `ParsedEvent(**fields)`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_normalization.py`:

```python
from __future__ import annotations

from app.plugins.telegram import normalize_telegram_payload


def _message(**overrides) -> dict:
    item = {
        "event_type": "telegram_message",
        "message_id": 42,
        "date": "2026-01-01T10:00:00+00:00",
        "text": "привет",
        "chat_id": 12345,
        "sender": {"id": 777, "username": "alice", "first_name": "Alice", "last_name": None},
    }
    item.update(overrides)
    return item


def test_message_maps_to_normalized_fields():
    fields = normalize_telegram_payload(_message())

    assert fields["text"] == "привет"
    assert fields["author_id"] == "777"
    assert fields["author_label"] == "@alice"
    assert fields["event_kind"] == "message"
    assert fields["thread_id"] == "12345"


def test_comment_is_a_comment_kind_and_keeps_reply_target():
    fields = normalize_telegram_payload(
        _message(event_type="telegram_comment", root_post_id=10, parent_message_id=11)
    )

    assert fields["event_kind"] == "comment"
    assert fields["reply_to"] == "11"
    assert fields["thread_id"] == "10"


def test_sender_without_username_falls_back_to_full_name():
    fields = normalize_telegram_payload(
        _message(sender={"id": 777, "username": None, "first_name": "Alice", "last_name": "Smith"})
    )

    assert fields["author_label"] == "Alice Smith"


def test_sender_without_any_name_falls_back_to_id():
    fields = normalize_telegram_payload(
        _message(sender={"id": 777, "username": None, "first_name": None, "last_name": None})
    )

    assert fields["author_label"] == "ID 777"


def test_missing_sender_does_not_raise():
    fields = normalize_telegram_payload(_message(sender=None))

    assert fields["author_id"] is None
    assert fields["author_label"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_normalization.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_telegram_payload'`

- [ ] **Step 3: Write the normalizer**

В `app/plugins/telegram.py`, рядом с другими модульными функциями (после `_entity_ref`, до `class TelegramPlugin`):

```python
def normalize_telegram_payload(item: dict) -> dict:
    """Map a Telegram message dict onto the shared ParsedEvent fields."""
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}

    sender_id = sender.get("id")
    author_id = str(sender_id) if sender_id is not None else None

    username = str(sender.get("username") or "").strip().removeprefix("@")
    full_name = " ".join(
        part for part in [
            str(sender.get("first_name") or "").strip(),
            str(sender.get("last_name") or "").strip(),
        ] if part
    ).strip()
    if username:
        author_label = f"@{username}"
    elif full_name:
        author_label = full_name
    elif author_id:
        author_label = f"ID {author_id}"
    else:
        author_label = None

    is_comment = str(item.get("event_type") or "") == "telegram_comment"
    root_post_id = item.get("root_post_id")
    parent_message_id = item.get("parent_message_id")
    chat_id = item.get("chat_id")

    if is_comment and root_post_id is not None:
        thread_id = str(root_post_id)
    elif chat_id is not None:
        thread_id = str(chat_id)
    else:
        thread_id = None

    return {
        "text": str(item.get("text") or "") or None,
        "author_id": author_id,
        "author_label": author_label,
        "event_kind": "comment" if is_comment else "message",
        "thread_id": thread_id,
        "reply_to": str(parent_message_id) if parent_message_id is not None else None,
    }
```

- [ ] **Step 4: Use it at the construction site**

В `app/plugins/telegram.py` заменить конструирование `ParsedEvent` (строки 606–610):

```python
            events.append(
                ParsedEvent(
                    external_id=external_id,
                    observed_at=observed_at,
                    payload=item,
                    **normalize_telegram_payload(item),
                )
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_normalization.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add app/plugins/telegram.py tests/test_normalization.py
git commit -m "feat: нормализация полей в Telegram-плагине"
```

---

### Task 7: Нормализация в darknet-плагине

У darknet три вида событий: `darknet_discovery` (служебное, без текста), `forum_post`, `forum_user`.

**Files:**
- Modify: `app/plugins/darknet.py:353-360`, `:409-433`, `:435-445`
- Test: `tests/test_normalization.py` (дополнить)

**Interfaces:**
- Consumes: `ParsedEvent` (Task 2)
- Produces: `normalize_darknet_payload(payload: dict) -> dict`

- [ ] **Step 1: Write the failing test**

Дописать в конец `tests/test_normalization.py`:

```python
from app.plugins.darknet import normalize_darknet_payload


def test_forum_post_maps_title_and_body():
    fields = normalize_darknet_payload(
        {
            "event_type": "forum_post",
            "thread_url": "https://f.onion/threads/1/",
            "thread_title": "Заголовок",
            "post_id": "p1",
            "author": "alice",
            "content": "тело поста",
        }
    )

    assert fields["event_kind"] == "post"
    assert fields["author_id"] == "alice"
    assert fields["author_label"] == "alice"
    assert fields["thread_id"] == "https://f.onion/threads/1/"
    assert "Заголовок" in fields["text"]
    assert "тело поста" in fields["text"]


def test_discovery_event_has_no_text():
    fields = normalize_darknet_payload({"event_type": "darknet_discovery", "target_id": 1})

    assert fields["text"] is None
    assert fields["event_kind"] == "darknet_discovery"


def test_forum_user_event_keeps_username_as_author():
    fields = normalize_darknet_payload(
        {"event_type": "forum_user", "thread_url": "https://f.onion/t/2/", "user": {"username": "bob"}}
    )

    assert fields["author_id"] == "bob"
    assert fields["event_kind"] == "forum_user"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_normalization.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_darknet_payload'`

- [ ] **Step 3: Write the normalizer**

В `app/plugins/darknet.py`, рядом с `_now_utc`:

```python
def normalize_darknet_payload(payload: dict) -> dict:
    """Map a darknet forum payload onto the shared ParsedEvent fields."""
    event_type = str(payload.get("event_type") or "darknet_event")

    author = str(payload.get("author") or "").strip()
    if not author:
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        author = str(user.get("username") or "").strip()

    text = "\n".join(
        chunk for chunk in [
            str(payload.get("thread_title") or "").strip(),
            str(payload.get("content") or payload.get("text") or "").strip(),
        ] if chunk
    ).strip()

    return {
        "text": text or None,
        "author_id": author or None,
        "author_label": author or None,
        "event_kind": "post" if event_type == "forum_post" else event_type,
        "thread_id": str(payload.get("thread_url") or "") or None,
        "reply_to": None,
    }
```

- [ ] **Step 4: Use it at all three construction sites**

В `app/plugins/darknet.py` у каждого из трёх `ParsedEvent(...)` (строки ~353, ~409, ~435) вынести payload в переменную и передать нормализованные поля. Пример для `forum_post` (~409):

```python
                post_payload = {
                    "event_type": "forum_post",
                    "thread_url": result.thread_url,
                    "thread_title": result.thread_title,
                    "post_id": post.post_id,
                    "author": post.author,
                    # ...остальные поля без изменений
                }
                events.append(
                    ParsedEvent(
                        external_id=external_id,
                        observed_at=_now_utc(),
                        payload=post_payload,
                        **normalize_darknet_payload(post_payload),
                    )
                )
```

Повторить для `darknet_discovery` (~353) и `forum_user` (~435).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_normalization.py -v`
Expected: PASS (8 passed)

- [ ] **Step 6: Commit**

```bash
git add app/plugins/darknet.py tests/test_normalization.py
git commit -m "feat: нормализация полей в darknet-плагине"
```

---

### Task 8: Воркер и листенер на `persist_events`

**Files:**
- Modify: `app/services/worker.py:336-400`
- Modify: `app/services/telegram_listener.py:256-310`
- Test: `tests/test_telegram_listener_routing.py` (существующий — проверить, что проходит)

**Interfaces:**
- Consumes: `persist_events` (Task 5)

- [ ] **Step 1: Replace the write block in worker.py**

В `app/services/worker.py` удалить блок записи событий целиком (от `existing_external_ids` до конца цикла `for event in events:`, строки ~310–400) и заменить на:

```python
        written = persist_events(
            session,
            target=target,
            parser_type=job.parser_type,
            account_id=job.account_id,
            owner_user_id=job.owner_user_id,
            events=events,
        )
```

Добавить импорт в начало файла:

```python
from app.services.event_sink import persist_events
```

Удалить импорт `object_store` и вызовы `search_index.index_raw_event` из этого файла — индексация уходит вместе с OpenSearch (план 2), а self-heal `payload_ref` больше не нужен: рассинхрона S3↔БД не существует.

**Важно:** блок `upsert_darknet_profile_from_event` внутри старого цикла сохранить. Перенести его отдельным циклом после `persist_events`:

```python
        if is_darknet_job:
            for event in events:
                payload = event.payload if isinstance(event.payload, dict) else {}
                upsert_darknet_profile_from_event(
                    session=session,
                    target_id=job.target_id,
                    target_identifier=str(target.identifier or ""),
                    account_id=job.account_id,
                    payload=payload,
                    observed_at=event.observed_at,
                    increment_post_counter=False,
                )
```

- [ ] **Step 2: Replace the write block in telegram_listener.py**

В `app/services/telegram_listener.py` заменить блок с `object_store.put_json` и созданием `RawEvent` (строки ~256–310) на:

```python
            persist_events(
                session,
                target=target,
                parser_type="telegram",
                account_id=account_id,
                owner_user_id=owner_user_id,
                events=[
                    ParsedEvent(
                        external_id=external_id,
                        observed_at=observed_at,
                        payload=payload,
                        **normalize_telegram_payload(payload),
                    )
                ],
            )
            session.commit()
```

Добавить импорты:

```python
from app.plugins.base import ParsedEvent
from app.plugins.telegram import normalize_telegram_payload
from app.services.event_sink import persist_events
```

Переменные `external_id`, `observed_at`, `payload`, `account_id`, `owner_user_id`, `target` уже существуют в этом месте — проверить их имена по месту и подставить фактические.

- [ ] **Step 3: Run the full suite**

Run: `pytest tests/ -v`
Expected: PASS. Если `tests/test_object_store.py` падает на отсутствии `put_json` — это ожидаемо, он чинится в Task 9.

- [ ] **Step 4: Verify end to end**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && docker compose up -d db && docker compose run --rm api python -m app.cli db-upgrade
```
Затем запустить воркер на существующем таргете и проверить:
```bash
docker compose exec db psql -U postgres -d aggregator -c \
  "SELECT parser_type, event_kind, author_label, left(text, 40) AS text FROM raw_events ORDER BY id DESC LIMIT 5;"
```
Expected: строки с заполненными `event_kind`, `author_label`, `text`.

- [ ] **Step 5: Commit**

```bash
git add app/services/worker.py app/services/telegram_listener.py
git commit -m "refactor: воркер и telegram-листенер пишут через persist_events"
```

---

### Task 9: Снос S3-пути для JSON

**Files:**
- Modify: `app/services/object_store.py` (удалить `put_json`, `get_json`, `_build_preview`, `StoredObject`, `local_dir`)
- Modify: `app/routers/api.py` (9 вызовов), `app/routers/ui.py` (4 вызова)
- Modify: `tests/test_object_store.py`

**Interfaces:**
- Consumes: `RawEvent.payload` (Task 3)

- [ ] **Step 1: Find every call site**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && grep -rn "get_json\|put_json\|payload_ref\|payload_preview\|storage_type" app/ tests/ | grep -v __pycache__
```
Expected: список мест для правки. Ожидается 13 вызовов `get_json` в `api.py`/`ui.py`, упоминания `payload_ref` в `api.py:4587` и `ui.py:522,542`.

- [ ] **Step 2: Replace the read pattern**

Каждое вхождение вида

```python
payload = event.payload if isinstance(event.payload, dict) else object_store.get_json(event)
```

заменить на

```python
payload = event.payload
```

`payload` теперь `NOT NULL`, проверка `isinstance` избыточна. Вхождения вида `payload = object_store.get_json(event)` (например `api.py:4600`, `ui.py:561`) заменить так же.

Из ответов API удалить ключ `"payload_ref"` (`api.py:4587`, `ui.py:542`) и колонку `payload_ref` из CSV-выгрузки (`ui.py:522`).

- [ ] **Step 3: Strip object_store down to file storage**

В `app/services/object_store.py` удалить `put_json`, `get_json`, `_build_preview`, `_json_default`, `StoredObject`, `_safe_object_basename` и создание `self.local_dir`. Оставить `__init__` с S3-клиентом и `_ensure_bucket` — на них опирается план 3.

Класс становится пустым по публичным методам до плана 3; это ожидаемо.

- [ ] **Step 4: Update the object store test**

Открыть `tests/test_object_store.py`. Тесты на `put_json`/`get_json` удалить — функциональности больше нет. Если после удаления файл пуст, удалить файл:

```bash
git rm tests/test_object_store.py
```

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Verify no dead references remain**

Run:
```bash
grep -rn "get_json\|put_json\|payload_ref\|payload_preview\|storage_type\|payload_sha256\|payload_size" app/ tests/ | grep -v __pycache__
```
Expected: пусто.

- [ ] **Step 7: Commit**

```bash
git add -A app/services/object_store.py app/routers/api.py app/routers/ui.py tests/
git commit -m "refactor: JSON читается из JSONB, S3-путь для payload удалён"
```

---

## Self-Review

**Покрытие спеки (§1–3, §«Порядок работ» этап 1):**

| Требование спеки | Задача |
|---|---|
| `FileRef` + нормализованные поля `ParsedEvent` | Task 2 |
| `parser_type` → String в четырёх таблицах | Task 1, Task 4 |
| Валидация через реестр (`is_known`) | Task 2 |
| `payload` JSONB NOT NULL | Task 3, Task 4 |
| Нормализованные колонки + индексы | Task 3, Task 4 |
| Уникальный индекс сохранён | Task 4, проверен тестом в Task 5 |
| `text_search` + GIN, вне модели | Task 4 |
| `event_files` / `raw_event_files`, m2m | Task 3, Task 4 |
| `persist_events` как единая запись | Task 5, Task 8 |
| Telegram/darknet заполняют поля | Task 6, Task 7 |
| Удаление `put_json`/`get_json`, self-heal, 13 вызовов | Task 8, Task 9 |
| Чистка `service_state` от autosync-ключа | Task 4 |
| Миграция 0009 остаётся в цепочке | Task 4 (комментарий в коде) |

Не входит в этот план (отдельные планы): `search_messages` на FTS и снос OpenSearch (план 2); `put_bytes`/`presigned_url` и скачивание медиа (план 3); WhatsApp (план 4). `event.files` в `persist_events` намеренно не обрабатывается — отмечено в Task 5.

**Согласованность имён:** `persist_events` — одна сигнатура в Task 5 и Task 8. `normalize_telegram_payload` — Task 6 и Task 8. `ParserTypeStr` — Task 1 и Task 3. `JSONB_OR_JSON` — Task 3 и косвенно Task 4 (в миграции явный `postgresql.JSONB()`, потому что миграция всегда исполняется на Postgres).

**Риск, принятый осознанно:** Task 8 правит два крупных блока по номерам строк, которые сдвинутся после предыдущих задач. Исполнителю следует искать блоки по содержимому (`object_store.put_json`, `existing_external_ids`), а не по номерам.
