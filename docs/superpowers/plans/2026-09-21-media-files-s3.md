# Медиафайлы в S3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Скачивать вложения из источников в S3 с дедупликацией по sha256 и отдавать их во фронтенд по временным ссылкам.

**Architecture:** `ObjectStore` получает `put_bytes`/`presigned_url`. Файл кладётся по ключу `media/<sha256>`, поэтому один и тот же файл из разных сообщений и разных источников хранится один раз. `persist_events` связывает событие с файлами через `event_files`/`raw_event_files`. Telegram-плагин скачивает медиа через Telethon; лимит размера отсекает крупные файлы.

**Tech Stack:** boto3, MinIO, Telethon, SQLAlchemy 2.0, pytest

**Spec:** `docs/superpowers/specs/2026-09-17-unified-parser-architecture-design.md` (§5)

**Prerequisite:** План `2026-09-21-core-normalized-events.md` выполнен. Таблицы `event_files`/`raw_event_files` и `FileRef` существуют.

## Global Constraints

- Ключ объекта — ровно `media/<sha256>`, одинаковый для всех источников. Это условие дедупликации: Telegram-медиа заливает Python, WhatsApp-медиа (план 4) — Node-мост, оба пишут по тому же ключу.
- Фолбэка на локальный диск нет. MinIO недоступен → файл не сохраняется, событие сохраняется без связи с файлом, ошибка в лог. Это сознательное отличие от прежнего поведения `put_json`, который молча писал в `data/raw/`.
- Лимит размера — настройка `media_max_bytes`, по умолчанию 52428800 (50 MB). Файл больше лимита не скачивается; связь не создаётся; метаданные остаются в `payload`.
- Пропущенные файлы не довыкачиваются автоматически. В коде оставляется пометка `# ponytail: без ретрая, добавить очередь, когда пропуски станут заметны`.
- Presigned URL живёт 3600 секунд.

---

## File Structure

**Изменяются:**
- `app/services/object_store.py` — `put_bytes`, `presigned_url` вместо удалённых JSON-методов.
- `app/services/event_sink.py` — обработка `event.files`.
- `app/config.py` — `media_max_bytes`.
- `app/plugins/telegram.py` — скачивание медиа.
- `app/routers/api.py` — эндпоинт выдачи ссылки на файл.
- `frontend/src/pages/DataPage.tsx` — показ вложений.

**Создаются:**
- `tests/test_object_store_files.py`
- `tests/test_event_files.py`

---

### Task 1: `put_bytes` и `presigned_url`

**Files:**
- Modify: `app/services/object_store.py`
- Modify: `app/config.py`
- Test: `tests/test_object_store_files.py`

**Interfaces:**
- Produces:
  - `object_store.put_bytes(data: bytes, mime: str | None = None, filename: str | None = None) -> StoredFile | None`
  - `StoredFile(sha256: str, storage_key: str, size: int, mime: str | None, filename: str | None)`
  - `object_store.presigned_url(storage_key: str, ttl: int = 3600) -> str | None`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_object_store_files.py`:

```python
from __future__ import annotations

import hashlib

from app.services.object_store import ObjectStore


class _FakeClient:
    """Minimal stand-in for the boto3 S3 client."""

    def __init__(self, existing: set[str] | None = None):
        self.existing = existing or set()
        self.put_calls: list[tuple[str, bytes]] = []

    def head_bucket(self, Bucket):
        return {}

    def head_object(self, Bucket, Key):
        if Key not in self.existing:
            raise Exception("404 not found")
        return {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.put_calls.append((Key, Body))
        self.existing.add(Key)
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://minio.local/{Params['Key']}?exp={ExpiresIn}"


def _store(client) -> ObjectStore:
    store = ObjectStore()
    store._client = client
    store._bucket_ready = True
    return store


def test_put_bytes_uses_sha256_key():
    client = _FakeClient()
    store = _store(client)
    data = b"hello world"

    stored = store.put_bytes(data, mime="text/plain", filename="a.txt")

    digest = hashlib.sha256(data).hexdigest()
    assert stored.sha256 == digest
    assert stored.storage_key == f"media/{digest}"
    assert stored.size == 11
    assert client.put_calls[0][0] == f"media/{digest}"


def test_existing_object_is_not_uploaded_again():
    data = b"hello world"
    digest = hashlib.sha256(data).hexdigest()
    client = _FakeClient(existing={f"media/{digest}"})
    store = _store(client)

    stored = store.put_bytes(data)

    assert stored.storage_key == f"media/{digest}"
    assert client.put_calls == []


def test_put_bytes_returns_none_without_client():
    store = ObjectStore()
    store._client = None

    assert store.put_bytes(b"data") is None


def test_presigned_url_is_built():
    client = _FakeClient()
    store = _store(client)

    url = store.presigned_url("media/abc", ttl=60)

    assert url == "https://minio.local/media/abc?exp=60"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_object_store_files.py -v`
Expected: FAIL — `AttributeError: 'ObjectStore' object has no attribute 'put_bytes'`

- [ ] **Step 3: Add the settings**

В `app/config.py`, рядом с настройками S3:

```python
    media_max_bytes: int = 52428800  # 50 MB
```

- [ ] **Step 4: Implement the methods**

В `app/services/object_store.py` добавить датакласс и два метода в `ObjectStore`:

```python
@dataclass(slots=True)
class StoredFile:
    sha256: str
    storage_key: str
    size: int
    mime: str | None
    filename: str | None
```

```python
    def put_bytes(self, data: bytes, mime: str | None = None, filename: str | None = None) -> StoredFile | None:
        """Store bytes under media/<sha256>, skipping the upload if present.

        Returns None when the object store is unavailable: the caller keeps
        the event and drops the attachment.
        """
        if not self._client:
            return None

        digest = hashlib.sha256(data).hexdigest()
        key = f"media/{digest}"
        try:
            self._ensure_bucket()
            try:
                self._client.head_object(Bucket=self.settings.s3_bucket, Key=key)
            except Exception:
                self._client.put_object(
                    Bucket=self.settings.s3_bucket,
                    Key=key,
                    Body=data,
                    ContentType=mime or "application/octet-stream",
                )
        except Exception as exc:
            # ponytail: без ретрая, добавить очередь, когда пропуски станут заметны
            print(f"[object-store] put_bytes {key} failed: {exc}", flush=True)
            return None

        return StoredFile(
            sha256=digest,
            storage_key=key,
            size=len(data),
            mime=mime,
            filename=filename,
        )

    def presigned_url(self, storage_key: str, ttl: int = 3600) -> str | None:
        if not self._client:
            return None
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.settings.s3_bucket, "Key": storage_key},
                ExpiresIn=int(ttl),
            )
        except Exception as exc:
            print(f"[object-store] presigned_url {storage_key} failed: {exc}", flush=True)
            return None
```

Убедиться, что `hashlib` и `dataclass` импортированы (они были в файле до плана 1).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_object_store_files.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add app/services/object_store.py app/config.py tests/test_object_store_files.py
git commit -m "feat: put_bytes и presigned_url с дедупликацией по sha256"
```

---

### Task 2: Связывание файлов с событиями

**Files:**
- Modify: `app/services/event_sink.py`
- Test: `tests/test_event_files.py`

**Interfaces:**
- Consumes: `FileRef` (план 1), `EventFile`/`RawEventFile` (план 1)
- Produces: `persist_events` создаёт строки `event_files`/`raw_event_files` для `event.files`, у которых заполнен `sha256`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_event_files.py`:

```python
from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import EventFile, RawEventFile, Target
from app.plugins.base import FileRef, ParsedEvent
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


def _event(external_id: str, files: list[FileRef]) -> ParsedEvent:
    return ParsedEvent(
        external_id=external_id,
        observed_at=dt.datetime.now(dt.UTC),
        payload={"text": "with media"},
        text="with media",
        files=files,
    )


def _persist(session, target, event):
    persist_events(
        session, target=target, parser_type="telegram",
        account_id=None, owner_user_id=None, events=[event],
    )
    session.commit()


def test_file_row_is_created():
    session = _session()
    target = _target(session)

    _persist(session, target, _event("1", [
        FileRef(source_ref="f1", sha256="aa", mime="image/jpeg", size=100, filename="a.jpg"),
    ]))

    file = session.query(EventFile).one()
    assert file.storage_key == "media/aa"
    assert session.query(RawEventFile).count() == 1


def test_same_file_in_two_events_is_stored_once():
    session = _session()
    target = _target(session)
    shared = FileRef(source_ref="f1", sha256="aa", mime="image/jpeg", size=100)

    _persist(session, target, _event("1", [shared]))
    _persist(session, target, _event("2", [shared]))

    assert session.query(EventFile).count() == 1
    assert session.query(RawEventFile).count() == 2


def test_file_without_sha256_is_skipped():
    session = _session()
    target = _target(session)

    _persist(session, target, _event("1", [FileRef(source_ref="f1", sha256=None)]))

    assert session.query(EventFile).count() == 0


def test_several_files_keep_their_order():
    session = _session()
    target = _target(session)

    _persist(session, target, _event("1", [
        FileRef(source_ref="f1", sha256="aa"),
        FileRef(source_ref="f2", sha256="bb"),
    ]))

    links = session.query(RawEventFile).order_by(RawEventFile.position).all()
    assert [link.position for link in links] == [0, 1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_event_files.py -v`
Expected: FAIL — `assert 0 == 1`, файлы не создаются.

- [ ] **Step 3: Handle files in persist_events**

В `app/services/event_sink.py` добавить импорт и функцию, затем вызвать её в цикле:

```python
from app.models import EventFile, RawEvent, RawEventFile, Target
from app.plugins.base import FileRef, ParsedEvent


def _link_files(session: Session, raw_event_id: int, files: list[FileRef]) -> None:
    """Attach already-uploaded files to an event, deduplicating by sha256."""
    for position, ref in enumerate(files):
        if not ref.sha256:
            continue

        file = session.execute(
            select(EventFile).where(EventFile.sha256 == ref.sha256)
        ).scalar_one_or_none()
        if file is None:
            file = EventFile(
                sha256=ref.sha256,
                storage_key=f"media/{ref.sha256}",
                mime=ref.mime,
                size=ref.size,
                filename=ref.filename,
            )
            session.add(file)
            session.flush()

        exists = session.execute(
            select(RawEventFile).where(
                RawEventFile.raw_event_id == raw_event_id,
                RawEventFile.file_id == file.id,
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                RawEventFile(
                    raw_event_id=raw_event_id,
                    file_id=file.id,
                    source_ref=ref.source_ref,
                    position=position,
                )
            )
```

В цикле `for event in events:`, сразу после успешного `session.flush()` внутри `begin_nested`:

```python
        if event.files:
            _link_files(session, raw_event.id, event.files)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_event_files.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/event_sink.py tests/test_event_files.py
git commit -m "feat: persist_events связывает события с файлами"
```

---

### Task 3: Скачивание медиа в Telegram-плагине

**Files:**
- Modify: `app/plugins/telegram.py` (`_fetch_messages`, ~строки 330–412)

**Interfaces:**
- Consumes: `object_store.put_bytes` (Task 1), `FileRef` (план 1)

- [ ] **Step 1: Add the download helper**

В `app/plugins/telegram.py`, рядом с `normalize_telegram_payload`:

```python
async def _download_media(msg, max_bytes: int) -> FileRef | None:
    """Download a message attachment into S3, returning its reference.

    Returns None when there is no media, it exceeds the limit, or the object
    store is unavailable — the event is still saved, just without the file.
    """
    if not getattr(msg, "media", None):
        return None

    size = getattr(getattr(msg, "file", None), "size", None)
    if size is not None and int(size) > max_bytes:
        return None

    try:
        data = await msg.download_media(file=bytes)
    except Exception as exc:
        print(f"[telegram] download_media failed for {msg.id}: {exc}", flush=True)
        return None
    if not data or len(data) > max_bytes:
        return None

    file_obj = getattr(msg, "file", None)
    stored = object_store.put_bytes(
        data,
        mime=getattr(file_obj, "mime_type", None),
        filename=getattr(file_obj, "name", None),
    )
    if stored is None:
        return None

    return FileRef(
        source_ref=str(msg.id),
        filename=stored.filename,
        mime=stored.mime,
        size=stored.size,
        sha256=stored.sha256,
    )
```

Добавить импорты в начало файла:

```python
from app.plugins.base import FileRef, JobSpec, ParsedEvent, ParserPlugin
from app.services.object_store import object_store
```

- [ ] **Step 2: Collect files while fetching messages**

В `_fetch_messages`, в цикле по сообщениям, после формирования словаря `item`, добавить ключ с файлами. Для корневых сообщений (~строка 344) и для комментариев (~строка 393) одинаково:

```python
                max_bytes = int(get_settings().media_max_bytes)
                file_ref = await _download_media(msg, max_bytes)
                message_item["files"] = (
                    [
                        {
                            "source_ref": file_ref.source_ref,
                            "filename": file_ref.filename,
                            "mime": file_ref.mime,
                            "size": file_ref.size,
                            "sha256": file_ref.sha256,
                        }
                    ]
                    if file_ref
                    else []
                )
```

`message_item` — это словарь, добавляемый в `messages` (в коде он записан литералом внутри `messages.append({...})`; вынести его в переменную перед добавлением).

- [ ] **Step 3: Pass files into ParsedEvent**

В месте создания `ParsedEvent` (после плана 1 там стоит `**normalize_telegram_payload(item)`) добавить:

```python
            events.append(
                ParsedEvent(
                    external_id=external_id,
                    observed_at=observed_at,
                    payload=item,
                    files=[
                        FileRef(
                            source_ref=str(entry.get("source_ref") or ""),
                            filename=entry.get("filename"),
                            mime=entry.get("mime"),
                            size=entry.get("size"),
                            sha256=entry.get("sha256"),
                        )
                        for entry in (item.get("files") or [])
                    ],
                    **normalize_telegram_payload(item),
                )
            )
```

- [ ] **Step 4: Verify end to end**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && docker compose up -d && \
docker compose run --rm api python -m app.cli run-worker --concurrency 1 --queues telegram_backfill
```

Запустить парсинг канала с картинками, затем проверить:

```bash
docker compose exec db psql -U postgres -d aggregator -c \
  "SELECT f.sha256, f.mime, f.size, count(l.raw_event_id) AS events \
   FROM event_files f JOIN raw_event_files l ON l.file_id = f.id \
   GROUP BY f.id ORDER BY events DESC LIMIT 5;"
```
Expected: строки с заполненными `sha256`/`mime`/`size`. Если один файл пересылался — в `events` будет больше единицы, это подтверждает дедупликацию.

- [ ] **Step 5: Commit**

```bash
git add app/plugins/telegram.py
git commit -m "feat: скачивание медиа Telegram в S3"
```

---

### Task 4: Отдача файлов во фронтенд

**Files:**
- Modify: `app/routers/api.py`
- Modify: `frontend/src/pages/DataPage.tsx`, `frontend/src/api.ts`

**Interfaces:**
- Consumes: `object_store.presigned_url` (Task 1)
- Produces: `GET /api/events/{event_id}/files` → `[{id, filename, mime, size, url}]`

- [ ] **Step 1: Add the endpoint**

В `app/routers/api.py`, рядом с другими эндпоинтами событий:

```python
@router.get("/events/{event_id}/files")
def event_files(
    event_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    event = db.get(RawEvent, int(event_id))
    if event is None:
        raise HTTPException(status_code=404, detail="Подію не знайдено")
    if not _is_admin(user) and event.owner_user_id != int(user.id):
        raise HTTPException(status_code=403, detail="Немає доступу")

    rows = db.execute(
        select(EventFile, RawEventFile.position)
        .join(RawEventFile, RawEventFile.file_id == EventFile.id)
        .where(RawEventFile.raw_event_id == int(event_id))
        .order_by(RawEventFile.position)
    ).all()

    return [
        {
            "id": int(file.id),
            "filename": file.filename,
            "mime": file.mime,
            "size": file.size,
            "url": object_store.presigned_url(file.storage_key),
        }
        for file, _position in rows
    ]
```

Проверить импорты `EventFile`, `RawEventFile` из `app.models`.

- [ ] **Step 2: Verify the endpoint**

Run:
```bash
curl -s -b cookies.txt http://127.0.0.1:8000/api/events/1/files | head
```
Expected: JSON-массив; при наличии вложений — с непустым `url`.

- [ ] **Step 3: Show attachments in the UI**

В `frontend/src/api.ts` добавить:

```typescript
export async function fetchEventFiles(eventId: number) {
  const response = await api.get(`/events/${eventId}/files`)
  return response.data as Array<{
    id: number
    filename: string | null
    mime: string | null
    size: number | null
    url: string | null
  }>
}
```

Точное имя экземпляра axios взять из соседних функций файла.

В `frontend/src/pages/DataPage.tsx`, в карточке выбранного события, загрузить список и отрисовать: изображения (`mime` начинается с `image/`) — как `<img>` по `url`, остальные — ссылкой на скачивание с именем и размером.

- [ ] **Step 4: Verify in the browser**

Открыть страницу данных, выбрать событие с вложением. Ожидается: картинка отображается, файл скачивается по ссылке.

- [ ] **Step 5: Commit**

```bash
git add app/routers/api.py frontend/src/
git commit -m "feat: вложения событий отдаются по временным ссылкам"
```

---

## Self-Review

**Покрытие спеки (§5):**

| Требование | Задача |
|---|---|
| `put_bytes` с `head_object` перед заливкой | Task 1 |
| `presigned_url` | Task 1, Task 4 |
| Ключ `media/<sha256>`, единый для источников | Task 1 (константа в коде) |
| Фолбэк на локальный диск удалён | Task 1 (`return None`, не запись на диск) |
| `media_max_bytes` = 50 MB | Task 1, применяется в Task 3 |
| Файл больше лимита: связь не создаётся, метаданные в payload | Task 3 |
| m2m-дедупликация | Task 2 (тест `test_same_file_in_two_events_is_stored_once`) |
| Пропуски не довыкачиваются, помечено `ponytail:` | Task 1 |
| Скачивание медиа Telegram | Task 3 |
| Отдача во фронтенд | Task 4 |

**Согласованность имён:** `StoredFile` возвращается из `put_bytes` (Task 1) и разбирается в Task 3. `FileRef.sha256` — то поле, по которому `_link_files` (Task 2) решает, создавать ли связь; `_download_media` его заполняет (Task 3).

**Зависимость от плана 4:** WhatsApp-мост заливает медиа сам и передаёт готовый `sha256` — `_link_files` из Task 2 работает для него без изменений, потому что не знает, кто загрузил файл.
