# Поиск на Postgres FTS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить OpenSearch полнотекстовым поиском Postgres и убрать сервис из стека.

**Architecture:** Поиск идёт по generated-колонке `text_search` (`tsvector`, создана миграцией 0014) через GIN-индекс. `websearch_to_tsquery` даёт синтаксис в стиле поисковика, `ts_rank` — ранжирование, `ts_headline` — подсветку. `ts_headline` применяется только к отобранным `LIMIT N` строкам, а не ко всем совпадениям. Контракт ответа API сохраняется, чтобы фронтенд не переписывать.

**Tech Stack:** PostgreSQL 16 FTS, SQLAlchemy 2.0, FastAPI, pytest

**Spec:** `docs/superpowers/specs/2026-09-17-unified-parser-architecture-design.md` (§4)

**Prerequisite:** План `2026-09-21-core-normalized-events.md` выполнен полностью. Колонка `text_search` и индекс `ix_raw_events_text_search` существуют; `raw_events.text` заполняется плагинами.

## Global Constraints

- Конфигурация FTS — `simple`, не `russian`/`english`: данные многоязычные. Морфологии нет, «сообщение» не найдёт «сообщения» — известный потолок, зафиксированный в спеке.
- Контракт ответа `GET /search/messages` не меняется: `{"hits": [...], "total": N}`, каждый hit сохраняет ключи `event_id`, `parser_type`, `target_id`, `target_name`, `target_identifier`, `account_id`, `external_id`, `event_type`, `is_comment`, `sender_id`, `sender_label`, `sender_username`, `text`, `snippet`, `observed_at`, `observed_at_text`.
- Поле `event_type` в ответе заполняется из `raw_events.event_kind`, `sender_label`/`sender_username` — из `author_label`/`author_id`, `is_comment` — из `event_kind == "comment"`. Фронтенд не трогаем.
- Тесты FTS требуют настоящий Postgres — на SQLite `tsvector` не существует. Такие тесты помечаются `@pytest.mark.postgres` и пропускаются, если переменная `TEST_DATABASE_URL` не задана.
- Права доступа сохраняются: не-админ видит только события с `owner_user_id == user.id`.

---

## File Structure

**Создаются:**
- `app/services/message_search.py` — поиск по событиям, ~110 строк. Заменяет `search_index.py`; отдельный модуль, потому что вызывается из двух мест `api.py`.
- `tests/test_message_search.py` — тесты поиска на настоящем Postgres.

**Изменяются:**
- `app/routers/api.py` — эндпоинты `/search/status`, `/search/messages`, блок поиска упоминаний (~3889), эндпоинт переиндексации (~3322-3360).
- `app/config.py` — удаление настроек OpenSearch.
- `docker-compose.yml` — удаление сервиса `opensearch` и его volume.
- `requirements.txt` — удаление `opensearch-py`.
- `.env.example`, `README.md` — удаление переменных OpenSearch.

**Удаляются:**
- `app/services/search_index.py`
- `app/services/search_autosync.py`

---

### Task 1: Поисковый модуль

**Files:**
- Create: `app/services/message_search.py`
- Test: `tests/test_message_search.py`

**Interfaces:**
- Produces: `search_messages(session, *, query, limit, owner_user_id, parser_type=None, target_id=None) -> tuple[list[dict], int]` — список hit-словарей и общее число совпадений.

- [ ] **Step 1: Add the Postgres test fixture**

Дописать в `tests/conftest.py`:

```python
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: requires a real PostgreSQL database")


@pytest.fixture
def pg_session():
    """Session against a real Postgres, skipped when TEST_DATABASE_URL is unset."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")

    from app.db import Base

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE raw_events ADD COLUMN text_search tsvector "
                "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
            )
        )
        conn.execute(text("CREATE INDEX ix_raw_events_text_search ON raw_events USING GIN (text_search)"))

    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()
```

- [ ] **Step 2: Write the failing test**

Создать `tests/test_message_search.py`:

```python
from __future__ import annotations

import datetime as dt

import pytest

from app.models import RawEvent, Target
from app.services.message_search import search_messages

pytestmark = pytest.mark.postgres


def _seed(session, *, owner_user_id: int | None = None) -> Target:
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
```

- [ ] **Step 3: Run test to verify it fails**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && docker compose up -d db && sleep 5 && \
TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator" \
pytest tests/test_message_search.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.message_search'`

Если порт 5432 не проброшен наружу, добавить в `docker-compose.yml` сервису `db`: `ports: ["5432:5432"]`.

- [ ] **Step 4: Write the implementation**

Создать `app/services/message_search.py`:

```python
from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, String, func, literal, or_, select, text
from sqlalchemy.orm import Session

from app.models import RawEvent, Target

FTS_CONFIG = "simple"


def _tsquery(query: str):
    return func.websearch_to_tsquery(literal(FTS_CONFIG), literal(query))


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
    safe_limit = max(10, min(int(limit or 100), 200))
    text_query = str(query or "").strip()

    filters = []
    if owner_user_id is not None:
        filters.append(RawEvent.owner_user_id == int(owner_user_id))
    if parser_type:
        filters.append(RawEvent.parser_type == str(parser_type))
    if target_id is not None:
        filters.append(RawEvent.target_id == int(target_id))

    if text_query:
        pattern = f"%{text_query}%"
        filters.append(
            or_(
                text("raw_events.text_search @@ websearch_to_tsquery('simple', :q)").bindparams(q=text_query),
                RawEvent.author_label.ilike(pattern),
                RawEvent.author_id.ilike(pattern),
                RawEvent.external_id.ilike(pattern),
            )
        )

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
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator" \
pytest tests/test_message_search.py -v
```
Expected: PASS (7 passed)

- [ ] **Step 6: Commit**

```bash
git add app/services/message_search.py tests/test_message_search.py tests/conftest.py
git commit -m "feat: полнотекстовый поиск по событиям на Postgres FTS"
```

---

### Task 2: Переключение API на новый поиск

**Files:**
- Modify: `app/routers/api.py` (`/search/status`, `/search/messages`, реиндексация ~3322, упоминания ~3889)

**Interfaces:**
- Consumes: `search_messages` (Task 1)

- [ ] **Step 1: Replace `/search/messages`**

В `app/routers/api.py` заменить тело эндпоинта. Проверка `parser_filter` теперь идёт через реестр плагинов, а не через закрытый список:

```python
@router.get("/search/messages")
def search_messages_endpoint(
    q: str = "",
    limit: int = 100,
    parser_type: str = "",
    target_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    parser_filter = str(parser_type or "").strip().lower()
    if parser_filter and not plugin_registry.is_known(parser_filter):
        raise HTTPException(status_code=400, detail="Некоректний parser_type")

    if target_id is not None:
        target = _ensure_target_access(db.get(Target, int(target_id)), user)
        if parser_filter and target.parser_type != parser_filter:
            raise HTTPException(status_code=400, detail="target_id не відповідає parser_type")
        parser_filter = target.parser_type

    owner_filter = None if _is_admin(user) else int(user.id)
    hits, total = search_messages(
        db,
        query=q,
        limit=limit,
        owner_user_id=owner_filter,
        parser_type=parser_filter or None,
        target_id=target_id,
    )

    for item in hits:
        observed_raw = str(item.get("observed_at") or "").strip()
        observed_dt = None
        if observed_raw:
            try:
                observed_dt = dt.datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
                if observed_dt.tzinfo is None:
                    observed_dt = observed_dt.replace(tzinfo=dt.UTC)
            except Exception:
                observed_dt = None
        item["observed_at_text"] = _format_kyiv_datetime(observed_dt)

    return {"hits": hits, "total": int(total)}
```

Имя функции меняется на `search_messages_endpoint`, чтобы не конфликтовать с импортом `search_messages`.

- [ ] **Step 2: Replace `/search/status`**

Поиск больше не может быть «недоступен» — он часть БД:

```python
@router.get("/search/status")
def search_status(db: Session = Depends(get_db), user=Depends(get_current_user)):
    total = db.execute(select(func.count()).select_from(RawEvent)).scalar_one()
    return {"enabled": True, "backend": "postgres", "indexed_events": int(total)}
```

- [ ] **Step 3: Remove the reindex endpoint**

Найти эндпоинт с `search_index.index_raw_event` (~строка 3322–3360) и удалить целиком — переиндексация не нужна, `text_search` поддерживается Postgres автоматически. Если на него ссылается фронтенд, удалить и кнопку (найти по `search/reindex` в `frontend/src/`).

- [ ] **Step 4: Fix the mentions search**

В блоке поиска упоминаний (~строка 3889) заменить вызов:

```python
            hits, total = search_messages(
                db,
                query=f"@{username}",
                limit=200,
                owner_user_id=owner_filter,
                parser_type="telegram",
                target_id=None,
            )
```

Обёртку `try/except RuntimeError` и флаг `search_unavailable` сохранить — они безвредны, поиск теперь просто не выбрасывает `RuntimeError`.

- [ ] **Step 5: Fix imports**

В начале `app/routers/api.py`:
- удалить `from app.services.search_index import search_index`
- удалить импорт `get_search_autosync_state` из `app.services.search_autosync`
- добавить `from app.services.message_search import search_messages`
- убедиться, что `plugin_registry` и `func` импортированы

- [ ] **Step 6: Verify the app starts**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && python -c "from app.main import app; print('ok')"
```
Expected: `ok` без трейсбека.

- [ ] **Step 7: Commit**

```bash
git add app/routers/api.py
git commit -m "feat: API поиска работает через Postgres FTS"
```

---

### Task 3: Снос OpenSearch

**Files:**
- Delete: `app/services/search_index.py`, `app/services/search_autosync.py`
- Modify: `app/config.py`, `docker-compose.yml`, `requirements.txt`, `.env.example`, `README.md`
- Modify: `app/services/scheduler.py` (если вызывает autosync)

**Interfaces:**
- Consumes: Task 2 (все обращения к OpenSearch уже сняты)

- [ ] **Step 1: Find every remaining reference**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && grep -rn -i "opensearch\|search_index\|search_autosync" app/ frontend/src/ tests/ docker-compose.yml requirements.txt .env.example README.md 2>/dev/null | grep -v __pycache__ | grep -v node_modules
```
Expected: список мест. Ожидаются вызовы `autosync_search_index_batch` в `scheduler.py` и настройки в `config.py`.

- [ ] **Step 2: Remove the autosync call from the scheduler**

Найти в `app/services/scheduler.py` вызов `autosync_search_index_batch` и удалить его вместе с импортом и относящимся к нему блоком расписания.

- [ ] **Step 3: Delete the modules**

```bash
git rm app/services/search_index.py app/services/search_autosync.py
```

- [ ] **Step 4: Strip the settings**

В `app/config.py` удалить поля `opensearch_enabled`, `opensearch_url`, `opensearch_username`, `opensearch_password`, `opensearch_index_messages`, `opensearch_verify_certs`, `opensearch_timeout_seconds`.

- [ ] **Step 5: Remove the service from compose**

В `docker-compose.yml`:
- удалить сервис `opensearch` целиком
- удалить `opensearch_data` из блока `volumes:`
- у сервисов `api`, `scheduler`, `worker-*`, `telegram-listener` удалить переменные окружения `OPENSEARCH_*` и `depends_on: opensearch`
- удалить сервис `worker-web` (плагина `web` не существует, очередь мёртвая — зафиксировано в спеке §2)

- [ ] **Step 6: Strip the dependency and docs**

```bash
cd /Users/andriihrenchyshen/data_agregator && sed -i '' '/opensearch-py/d' requirements.txt
```

Из `.env.example` удалить строки `OPENSEARCH_*`. В `README.md` заменить упоминания OpenSearch описанием поиска на Postgres FTS.

- [ ] **Step 7: Verify nothing remains**

Run:
```bash
grep -rn -i "opensearch" app/ frontend/src/ tests/ docker-compose.yml requirements.txt .env.example README.md 2>/dev/null | grep -v __pycache__ | grep -v node_modules
```
Expected: пусто.

- [ ] **Step 8: Run the full suite and start the stack**

Run: `pytest tests/ -v`
Expected: PASS.

Run:
```bash
docker compose up -d && sleep 20 && docker compose ps
```
Expected: все сервисы `running`; `opensearch` и `worker-web` отсутствуют.

- [ ] **Step 9: Verify search end to end**

Открыть страницу поиска в UI, ввести слово, встречающееся в сохранённых сообщениях. Ожидается: результаты с подсветкой найденного слова.

Проверить, что находится текст длиннее 200 символов — именно это было сломано до переезда:

```bash
docker compose exec db psql -U postgres -d aggregator -c \
  "SELECT id, length(text) FROM raw_events WHERE length(text) > 200 LIMIT 3;"
```
Взять слово из середины такого сообщения и найти его через UI.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: снят OpenSearch, поиск полностью на Postgres"
```

---

## Self-Review

**Покрытие спеки (§4):**

| Требование | Задача |
|---|---|
| `websearch_to_tsquery` + `ts_rank` + `ts_headline` | Task 1 |
| `ts_headline` только над `LIMIT N` | Task 1 (отдельный запрос по `event_ids`) |
| Конфигурация `simple` | Task 1 (`FTS_CONFIG`) |
| Фильтры owner/parser_type/target_id как WHERE | Task 1 |
| Поиск по полному тексту, не по превью | Task 3, шаг 9 (явная проверка) |
| Снос `search_index.py`, `search_autosync.py`, `opensearch-py`, сервиса, переменных | Task 3 |
| Миграция 0009 остаётся в цепочке | не трогается ни одной задачей — корректно |
| Удаление `worker-web` | Task 3, шаг 5 |

**Согласованность имён:** `search_messages(session, *, query, limit, owner_user_id, parser_type, target_id)` — одна сигнатура в Task 1 и обоих вызовах Task 2. Эндпоинт переименован в `search_messages_endpoint` во избежание конфликта с импортом.

**Принятое ограничение:** поиск по `author_label`/`author_id`/`external_id` реализован через `ILIKE`, а не через FTS — эти поля короткие, отдельный `tsvector` на них избыточен. При росте объёма может потребоваться индекс `pg_trgm`; отмечать в коде не нужно, пока не проявится.
