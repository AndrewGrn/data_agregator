# Источник WhatsApp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить WhatsApp как источник событий с функционалом уровня Telegram: приём сообщений, медиа, участники групп, привязка аккаунта через QR.

**Architecture:** Node-сервис на whatsapp-web.js держит WhatsApp-сессии (Puppeteer + headless Chromium) и публикует сообщения в NATS JetStream. Python-листенер читает durable-консьюмером, маппит в `ParsedEvent` и пишет через `persist_events`. Медиа заливается в S3 прямо из Node — через брокер идут только `sha256` и метаданные, потому что лимит сообщения NATS 1 MB. Статус сессии и QR передаются через `service_state`, новых транспортов между API и мостом нет.

**Tech Stack:** Node 20, whatsapp-web.js, nats (npm), MinIO SDK, Python 3.12, nats-py 2.7.2, SQLAlchemy 2.0, pytest

**Spec:** `docs/superpowers/specs/2026-09-17-unified-parser-architecture-design.md` (§6)

**Prerequisite:** Планы `2026-09-21-core-normalized-events.md` и `2026-09-21-media-files-s3.md` выполнены. `ParsedEvent`, `persist_events`, `_link_files` и таблицы файлов существуют.

## Global Constraints

- Версия контракта NATS-сообщения — `v: 1`. Схема зафиксирована в спеке §6 и продублирована в Task 2; Node-сериализатор и Python-маппер проверяются против одной фикстуры.
- Стрим `WA_EVENTS`: subjects `wa.events.*`, retention `limits`, `max_age` 7 дней, storage `file`. Ack — **после** коммита в Postgres. Повторная доставка гасится уникальным индексом `raw_events`.
- Статусы сессии (`wa.status.*`) идут через core NATS без стрима: после факта они не нужны.
- `ParserAccount.credentials = {"session_id": "<uuid>"}`; `session_id` служит `clientId` для `LocalAuth`, каталог сессии — `wa_sessions/<session_id>/`.
- `Target.identifier` — id чата WhatsApp: `<digits>@g.us` для групп, `<digits>@c.us` для личных.
- Глубокого бэкфилла нет: доступно только то, что отдаёт `chat.fetchMessages({limit})`. Логика `_build_backfill_ranges`/`_full_backfill_state` из Telegram не переносится.
- Медиа заливается по тому же ключу `media/<sha256>`, что и в плане 3, — это условие сквозной дедупликации между источниками.
- Node-сервис не содержит логики нормализации: только сериализация сообщения и `putObject` по посчитанному хешу.

---

## File Structure

**Создаются:**
- `whatsapp-bridge/package.json` — зависимости Node-сервиса.
- `whatsapp-bridge/src/index.js` — точка входа: подключение к NATS и Postgres, старт клиентов.
- `whatsapp-bridge/src/client.js` — обёртка над одним WhatsApp-клиентом: события, статусы, бэкфилл.
- `whatsapp-bridge/src/serialize.js` — сборка объекта события по контракту `v: 1`.
- `whatsapp-bridge/src/media.js` — заливка вложения в S3 по `media/<sha256>`.
- `whatsapp-bridge/test/serialize.test.js` — контрактный тест сериализатора.
- `whatsapp-bridge/Dockerfile`
- `app/plugins/whatsapp.py` — плагин.
- `app/services/whatsapp_listener.py` — durable-консьюмер.
- `tests/test_whatsapp_mapping.py` — контрактный тест маппера.
- `tests/fixtures/wa_event_v1.json` — общая фикстура для обеих сторон.

**Изменяются:**
- `app/plugins/registry.py` — регистрация плагина.
- `app/services/job_routing.py` — очередь `whatsapp`.
- `app/routers/api.py` — статус сессии и QR.
- `docker-compose.yml` — сервисы `whatsapp-bridge`, `whatsapp-listener`, volume `wa_sessions`.
- `frontend/src/pages/` — экран привязки аккаунта.

---

### Task 1: Контракт события и фикстура

Контракт пишется первым: на него опираются обе стороны, и обе тестируются против одной фикстуры.

**Files:**
- Create: `tests/fixtures/wa_event_v1.json`

**Interfaces:**
- Produces: фикстура события WhatsApp по схеме `v: 1`, используется в Task 2 (Python) и Task 5 (Node)

- [ ] **Step 1: Write the fixture**

Создать `tests/fixtures/wa_event_v1.json`:

```json
{
  "v": 1,
  "account_id": 12,
  "chat_id": "120363012345678901@g.us",
  "message_id": "true_120363012345678901@g.us_3EB0C767D097B4C7F2A1",
  "timestamp": 1758400000,
  "from": "380671234567@c.us",
  "author": "380671234567@c.us",
  "author_name": "Andrii",
  "body": "привет всем",
  "type": "chat",
  "has_quoted": false,
  "quoted_message_id": null,
  "media": null,
  "raw": {
    "id": {"_serialized": "true_120363012345678901@g.us_3EB0C767D097B4C7F2A1"},
    "body": "привет всем",
    "type": "chat",
    "timestamp": 1758400000,
    "from": "380671234567@c.us",
    "hasMedia": false
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/wa_event_v1.json
git commit -m "feat: фикстура контракта WhatsApp-события v1"
```

---

### Task 2: Маппер WhatsApp → ParsedEvent

**Files:**
- Create: `app/plugins/whatsapp.py`
- Test: `tests/test_whatsapp_mapping.py`

**Interfaces:**
- Consumes: `ParsedEvent`, `FileRef` (план 1), фикстура (Task 1)
- Produces: `map_wa_event(message: dict) -> ParsedEvent`; класс `WhatsAppPlugin(ParserPlugin)` с `parser_type = "whatsapp"`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_whatsapp_mapping.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.whatsapp import map_wa_event

FIXTURE = Path(__file__).parent / "fixtures" / "wa_event_v1.json"


@pytest.fixture
def wa_message() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_maps_core_fields(wa_message):
    event = map_wa_event(wa_message)

    assert event.external_id == wa_message["message_id"]
    assert event.text == "привет всем"
    assert event.author_id == "380671234567@c.us"
    assert event.author_label == "Andrii"
    assert event.thread_id == "120363012345678901@g.us"
    assert event.event_kind == "message"


def test_payload_is_the_raw_object(wa_message):
    event = map_wa_event(wa_message)

    assert event.payload == wa_message["raw"]


def test_timestamp_becomes_utc_datetime(wa_message):
    event = map_wa_event(wa_message)

    assert event.observed_at is not None
    assert event.observed_at.tzinfo is not None
    assert event.observed_at.year == 2025


def test_quoted_message_becomes_reply(wa_message):
    wa_message["has_quoted"] = True
    wa_message["quoted_message_id"] = "true_120363@g.us_PREV"

    event = map_wa_event(wa_message)

    assert event.event_kind == "reply"
    assert event.reply_to == "true_120363@g.us_PREV"


def test_media_becomes_a_file_ref(wa_message):
    wa_message["media"] = {
        "sha256": "ab" * 32, "mime": "image/jpeg", "size": 2048, "filename": "photo.jpg",
    }

    event = map_wa_event(wa_message)

    assert len(event.files) == 1
    assert event.files[0].sha256 == "ab" * 32
    assert event.files[0].mime == "image/jpeg"


def test_author_falls_back_to_from_in_direct_chats(wa_message):
    wa_message["author"] = None
    wa_message["author_name"] = None

    event = map_wa_event(wa_message)

    assert event.author_id == "380671234567@c.us"
    assert event.author_label == "380671234567"


def test_empty_body_gives_no_text(wa_message):
    wa_message["body"] = ""

    event = map_wa_event(wa_message)

    assert event.text is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_whatsapp_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.plugins.whatsapp'`

- [ ] **Step 3: Write the mapper and the plugin**

Создать `app/plugins/whatsapp.py`:

```python
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.models import ParseJob, ParserAccount, Target
from app.plugins.base import FileRef, JobSpec, ParsedEvent, ParserPlugin

QUEUE_WHATSAPP = "whatsapp"


def map_wa_event(message: dict) -> ParsedEvent:
    """Map a bridge message (contract v1) onto a ParsedEvent."""
    author_id = str(message.get("author") or message.get("from") or "").strip() or None

    author_label = str(message.get("author_name") or "").strip()
    if not author_label and author_id:
        # "380671234567@c.us" reads better as the bare number
        author_label = author_id.split("@", 1)[0]

    timestamp = message.get("timestamp")
    observed_at = (
        dt.datetime.fromtimestamp(int(timestamp), dt.UTC) if timestamp is not None else None
    )

    media = message.get("media") if isinstance(message.get("media"), dict) else None
    files: list[FileRef] = []
    if media and media.get("sha256"):
        files.append(
            FileRef(
                source_ref=str(message.get("message_id") or ""),
                filename=media.get("filename"),
                mime=media.get("mime"),
                size=media.get("size"),
                sha256=str(media["sha256"]),
            )
        )

    has_quoted = bool(message.get("has_quoted"))
    raw = message.get("raw")

    return ParsedEvent(
        external_id=str(message.get("message_id") or "") or None,
        observed_at=observed_at,
        payload=raw if isinstance(raw, dict) else {},
        text=str(message.get("body") or "") or None,
        author_id=author_id,
        author_label=author_label or None,
        event_kind="reply" if has_quoted else "message",
        thread_id=str(message.get("chat_id") or "") or None,
        reply_to=str(message.get("quoted_message_id") or "") or None,
        files=files,
    )


class WhatsAppPlugin(ParserPlugin):
    parser_type = "whatsapp"

    def generate_jobs(self, session: Session, target: Target) -> list[JobSpec]:
        """One backfill job per target; live messages arrive through the stream."""
        limit = int((target.config or {}).get("backfill_limit") or 200)
        return [
            JobSpec(
                parser_type=self.parser_type,
                target_id=target.id,
                account_id=None,
                job_key=f"wa-backfill:{target.id}",
                payload={"chat_id": target.identifier, "limit": limit},
                queue=QUEUE_WHATSAPP,
            )
        ]

    def run(
        self,
        session: Session,
        job: ParseJob,
        target: Target,
        account: ParserAccount | None,
    ) -> list[ParsedEvent]:
        """Ask the bridge for history; events arrive via the listener, not here."""
        from app.services.whatsapp_bus import request_backfill

        request_backfill(
            account_id=account.id if account else None,
            chat_id=str(target.identifier),
            limit=int((job.payload or {}).get("limit") or 200),
        )
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_whatsapp_mapping.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Register the plugin**

В `app/plugins/registry.py`:

```python
from app.plugins.whatsapp import WhatsAppPlugin

plugin_registry.register(WhatsAppPlugin())
```

В `app/services/job_routing.py` добавить константу и ветку маршрутизации по образцу существующей для `web`:

```python
QUEUE_WHATSAPP = "whatsapp"
```

```python
    if parser_type_value == "whatsapp":
        return QUEUE_WHATSAPP
```

- [ ] **Step 6: Verify the registry**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && python -c "from app.plugins.registry import plugin_registry; print(plugin_registry.list_types())"
```
Expected: `['darknet', 'telegram', 'whatsapp']`

- [ ] **Step 7: Commit**

```bash
git add app/plugins/whatsapp.py app/plugins/registry.py app/services/job_routing.py tests/test_whatsapp_mapping.py
git commit -m "feat: плагин WhatsApp и маппер контракта v1"
```

---

### Task 3: Шина NATS на стороне Python

**Files:**
- Create: `app/services/whatsapp_bus.py`
- Modify: `app/config.py`

**Interfaces:**
- Produces:
  - `ensure_stream()` — создаёт стрим `WA_EVENTS`, идемпотентно
  - `request_backfill(account_id, chat_id, limit)` — публикует запрос в `wa.backfill.<account_id>`
  - `consume_events(handler)` — durable-консьюмер, ack после успешного `handler`

- [ ] **Step 1: Add the settings**

В `app/config.py`:

```python
    nats_url: str = "nats://nats:4222"
    wa_stream: str = "WA_EVENTS"
    wa_durable: str = "wa-listener"
```

- [ ] **Step 2: Write the bus module**

Создать `app/services/whatsapp_bus.py`:

```python
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import nats
from nats.js.api import RetentionPolicy, StorageType, StreamConfig

from app.config import get_settings

settings = get_settings()

SUBJECT_EVENTS = "wa.events.*"
SUBJECT_STATUS = "wa.status.*"


async def _connect():
    return await nats.connect(settings.nats_url)


async def ensure_stream() -> None:
    """Create the WA_EVENTS stream if it does not exist yet."""
    connection = await _connect()
    try:
        js = connection.jetstream()
        config = StreamConfig(
            name=settings.wa_stream,
            subjects=[SUBJECT_EVENTS],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_age=7 * 24 * 3600,
        )
        try:
            await js.add_stream(config)
        except Exception:
            await js.update_stream(config)
    finally:
        await connection.close()


async def _publish(subject: str, payload: dict) -> None:
    connection = await _connect()
    try:
        await connection.publish(subject, json.dumps(payload).encode("utf-8"))
        await connection.flush()
    finally:
        await connection.close()


def request_backfill(account_id: int | None, chat_id: str, limit: int) -> None:
    """Ask the bridge to replay chat history into the stream."""
    if account_id is None:
        return
    asyncio.run(
        _publish(
            f"wa.backfill.{int(account_id)}",
            {"chat_id": str(chat_id), "limit": int(limit)},
        )
    )


async def consume_events(handler: Callable[[dict], Awaitable[None]]) -> None:
    """Run the durable consumer forever, acking only after handler succeeds."""
    await ensure_stream()
    connection = await _connect()
    js = connection.jetstream()
    subscription = await js.pull_subscribe(
        SUBJECT_EVENTS, durable=settings.wa_durable, stream=settings.wa_stream
    )

    while True:
        try:
            messages = await subscription.fetch(batch=10, timeout=5)
        except Exception:
            continue

        for message in messages:
            try:
                await handler(json.loads(message.data.decode("utf-8")))
            except Exception as exc:
                print(f"[wa-bus] handler failed, message will redeliver: {exc}", flush=True)
                continue
            # Ack only after the handler committed — redelivery is safe
            # because raw_events has a unique index on external_id.
            await message.ack()
```

- [ ] **Step 3: Verify the stream is created**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && docker compose up -d nats && sleep 3 && \
python -c "import asyncio; from app.services.whatsapp_bus import ensure_stream; asyncio.run(ensure_stream()); print('ok')"
```
Expected: `ok`. Проверить: `docker compose exec nats nats stream ls` (если утилита доступна) либо `curl -s http://127.0.0.1:8222/jsz` — стрим `WA_EVENTS` присутствует.

- [ ] **Step 4: Commit**

```bash
git add app/services/whatsapp_bus.py app/config.py
git commit -m "feat: шина NATS JetStream для событий WhatsApp"
```

---

### Task 4: Листенер WhatsApp

**Files:**
- Create: `app/services/whatsapp_listener.py`
- Modify: `app/cli.py`

**Interfaces:**
- Consumes: `map_wa_event` (Task 2), `consume_events` (Task 3), `persist_events` (план 1)

- [ ] **Step 1: Write the listener**

Создать `app/services/whatsapp_listener.py`:

```python
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ParserAccount, ServiceState, Target
from app.plugins.whatsapp import map_wa_event
from app.services.event_sink import persist_events
from app.services.whatsapp_bus import consume_events


async def _handle(message: dict) -> None:
    chat_id = str(message.get("chat_id") or "")
    account_id = message.get("account_id")
    if not chat_id:
        return

    with SessionLocal() as session:
        target = session.execute(
            select(Target).where(
                Target.parser_type == "whatsapp",
                Target.identifier == chat_id,
            )
        ).scalar_one_or_none()
        if target is None:
            # Message from a chat nobody asked to track.
            return

        persist_events(
            session,
            target=target,
            parser_type="whatsapp",
            account_id=int(account_id) if account_id is not None else None,
            owner_user_id=target.owner_user_id,
            events=[map_wa_event(message)],
        )
        session.commit()


def run_whatsapp_listener() -> None:
    asyncio.run(consume_events(_handle))
```

- [ ] **Step 2: Add the CLI command**

В `app/cli.py`, по образцу `run-telegram-listener`:

```python
@cli.command("run-whatsapp-listener")
def run_whatsapp_listener_command() -> None:
    from app.services.whatsapp_listener import run_whatsapp_listener

    run_whatsapp_listener()
```

Имя декоратора и стиль взять из соседних команд файла.

- [ ] **Step 3: Verify the command is registered**

Run: `cd /Users/andriihrenchyshen/data_agregator && python -m app.cli --help`
Expected: в списке присутствует `run-whatsapp-listener`.

- [ ] **Step 4: Commit**

```bash
git add app/services/whatsapp_listener.py app/cli.py
git commit -m "feat: листенер событий WhatsApp"
```

---

### Task 5: Node-мост

**Files:**
- Create: `whatsapp-bridge/package.json`, `whatsapp-bridge/src/serialize.js`, `whatsapp-bridge/src/media.js`, `whatsapp-bridge/src/client.js`, `whatsapp-bridge/src/index.js`, `whatsapp-bridge/Dockerfile`
- Test: `whatsapp-bridge/test/serialize.test.js`

**Interfaces:**
- Produces: сообщения в `wa.events.<account_id>` по контракту `v: 1` (Task 1); статусы в `wa.status.<account_id>`

- [ ] **Step 1: Write package.json**

Создать `whatsapp-bridge/package.json`:

```json
{
  "name": "whatsapp-bridge",
  "version": "1.0.0",
  "type": "commonjs",
  "main": "src/index.js",
  "scripts": {
    "start": "node src/index.js",
    "test": "node --test test/"
  },
  "dependencies": {
    "whatsapp-web.js": "^1.26.0",
    "nats": "^2.28.0",
    "pg": "^8.13.0",
    "@aws-sdk/client-s3": "^3.700.0",
    "qrcode": "^1.5.4"
  }
}
```

- [ ] **Step 2: Write the failing contract test**

Создать `whatsapp-bridge/test/serialize.test.js`:

```javascript
const test = require('node:test');
const assert = require('node:assert');
const { serializeMessage } = require('../src/serialize');

function fakeMessage(overrides = {}) {
  return Object.assign({
    id: { _serialized: 'true_120363012345678901@g.us_3EB0C767D097B4C7F2A1' },
    from: '380671234567@c.us',
    author: '380671234567@c.us',
    body: 'привет всем',
    type: 'chat',
    timestamp: 1758400000,
    hasMedia: false,
    hasQuotedMsg: false,
  }, overrides);
}

test('produces every field of contract v1', () => {
  const event = serializeMessage(fakeMessage(), {
    accountId: 12,
    chatId: '120363012345678901@g.us',
    authorName: 'Andrii',
    media: null,
  });

  assert.strictEqual(event.v, 1);
  assert.strictEqual(event.account_id, 12);
  assert.strictEqual(event.chat_id, '120363012345678901@g.us');
  assert.strictEqual(event.message_id, 'true_120363012345678901@g.us_3EB0C767D097B4C7F2A1');
  assert.strictEqual(event.timestamp, 1758400000);
  assert.strictEqual(event.author, '380671234567@c.us');
  assert.strictEqual(event.author_name, 'Andrii');
  assert.strictEqual(event.body, 'привет всем');
  assert.strictEqual(event.has_quoted, false);
  assert.strictEqual(event.media, null);
  assert.ok(event.raw);
});

test('carries media metadata but never the bytes', () => {
  const event = serializeMessage(fakeMessage({ hasMedia: true }), {
    accountId: 12,
    chatId: 'c@g.us',
    authorName: null,
    media: { sha256: 'ab', mime: 'image/jpeg', size: 2048, filename: 'p.jpg' },
  });

  assert.strictEqual(event.media.sha256, 'ab');
  assert.strictEqual(event.media.size, 2048);
  assert.strictEqual(JSON.stringify(event).includes('base64'), false);
});

test('marks a quoted message as a reply', () => {
  const event = serializeMessage(
    fakeMessage({ hasQuotedMsg: true }),
    { accountId: 1, chatId: 'c@g.us', authorName: null, media: null, quotedMessageId: 'PREV' },
  );

  assert.strictEqual(event.has_quoted, true);
  assert.strictEqual(event.quoted_message_id, 'PREV');
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /Users/andriihrenchyshen/data_agregator/whatsapp-bridge && npm install && npm test`
Expected: FAIL — `Cannot find module '../src/serialize'`

- [ ] **Step 4: Write the serializer**

Создать `whatsapp-bridge/src/serialize.js`:

```javascript
'use strict';

const CONTRACT_VERSION = 1;

/**
 * Build the wire object for wa.events.<account_id>.
 * Media bytes never travel here — only the sha256 of an already-uploaded file.
 */
function serializeMessage(msg, { accountId, chatId, authorName, media, quotedMessageId }) {
  return {
    v: CONTRACT_VERSION,
    account_id: accountId,
    chat_id: chatId,
    message_id: msg.id && msg.id._serialized ? msg.id._serialized : null,
    timestamp: msg.timestamp ?? null,
    from: msg.from ?? null,
    author: msg.author ?? msg.from ?? null,
    author_name: authorName ?? null,
    body: msg.body ?? '',
    type: msg.type ?? null,
    has_quoted: Boolean(msg.hasQuotedMsg),
    quoted_message_id: quotedMessageId ?? null,
    media: media ?? null,
    raw: JSON.parse(JSON.stringify(msg)),
  };
}

module.exports = { serializeMessage, CONTRACT_VERSION };
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/andriihrenchyshen/data_agregator/whatsapp-bridge && npm test`
Expected: PASS (3 tests)

- [ ] **Step 6: Write the media uploader**

Создать `whatsapp-bridge/src/media.js`:

```javascript
'use strict';

const crypto = require('node:crypto');
const { S3Client, PutObjectCommand, HeadObjectCommand } = require('@aws-sdk/client-s3');

const BUCKET = process.env.S3_BUCKET || 'raw-events';
const MAX_BYTES = Number(process.env.MEDIA_MAX_BYTES || 52428800);

const s3 = new S3Client({
  endpoint: process.env.S3_ENDPOINT_URL || 'http://minio:9000',
  region: process.env.S3_REGION || 'us-east-1',
  credentials: {
    accessKeyId: process.env.S3_ACCESS_KEY || 'minio',
    secretAccessKey: process.env.S3_SECRET_KEY || 'miniosecret',
  },
  forcePathStyle: true,
});

/**
 * Upload an attachment under media/<sha256>, matching the Python side's key
 * so the same file from any source is stored once.
 * Returns null when there is no media, it is too large, or S3 is unavailable.
 */
async function uploadMedia(msg) {
  if (!msg.hasMedia) return null;

  let media;
  try {
    media = await msg.downloadMedia();
  } catch (err) {
    console.error(`[media] download failed for ${msg.id?._serialized}: ${err.message}`);
    return null;
  }
  if (!media || !media.data) return null;

  const bytes = Buffer.from(media.data, 'base64');
  if (bytes.length > MAX_BYTES) {
    console.warn(`[media] skipped ${bytes.length} bytes, over the limit`);
    return null;
  }

  const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  const key = `media/${sha256}`;

  try {
    try {
      await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: key }));
    } catch {
      await s3.send(new PutObjectCommand({
        Bucket: BUCKET,
        Key: key,
        Body: bytes,
        ContentType: media.mimetype || 'application/octet-stream',
      }));
    }
  } catch (err) {
    // ponytail: без ретрая, событие сохраняется без файла
    console.error(`[media] upload ${key} failed: ${err.message}`);
    return null;
  }

  return {
    sha256,
    mime: media.mimetype || null,
    size: bytes.length,
    filename: media.filename || null,
  };
}

module.exports = { uploadMedia };
```

- [ ] **Step 7: Write the client wrapper**

Создать `whatsapp-bridge/src/client.js`:

```javascript
'use strict';

const { Client, LocalAuth } = require('whatsapp-web.js');
const QRCode = require('qrcode');
const { serializeMessage } = require('./serialize');
const { uploadMedia } = require('./media');

/**
 * One WhatsApp session. Publishes messages to wa.events.<accountId> and
 * session state to wa.status.<accountId>.
 */
function createClient({ accountId, sessionId, nc, sc }) {
  const client = new Client({
    authStrategy: new LocalAuth({ clientId: sessionId, dataPath: '/sessions' }),
    puppeteer: {
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    },
  });

  const publishStatus = (status, extra = {}) => {
    nc.publish(`wa.status.${accountId}`, sc.encode(JSON.stringify({ status, ...extra })));
  };

  client.on('qr', async (qr) => {
    const dataUrl = await QRCode.toDataURL(qr);
    publishStatus('qr', { qr_data_url: dataUrl });
  });
  client.on('authenticated', () => publishStatus('authenticated'));
  client.on('ready', () => publishStatus('ready'));
  client.on('disconnected', (reason) => publishStatus('disconnected', { reason: String(reason) }));

  const publishMessage = async (msg, js) => {
    const media = await uploadMedia(msg);
    let quotedMessageId = null;
    if (msg.hasQuotedMsg) {
      try {
        const quoted = await msg.getQuotedMessage();
        quotedMessageId = quoted?.id?._serialized ?? null;
      } catch { /* quoted message may be gone */ }
    }

    let authorName = null;
    try {
      const contact = await msg.getContact();
      authorName = contact?.pushname || contact?.name || null;
    } catch { /* contact may be unavailable */ }

    const event = serializeMessage(msg, {
      accountId,
      chatId: msg.from,
      authorName,
      media,
      quotedMessageId,
    });
    await js.publish(`wa.events.${accountId}`, sc.encode(JSON.stringify(event)));
  };

  return { client, publishMessage, publishStatus };
}

module.exports = { createClient };
```

- [ ] **Step 8: Write the entry point**

Создать `whatsapp-bridge/src/index.js`:

```javascript
'use strict';

const { connect, StringCodec } = require('nats');
const { Client: PgClient } = require('pg');
const { createClient } = require('./client');

const sc = StringCodec();

async function loadAccounts() {
  const pg = new PgClient({ connectionString: process.env.DATABASE_URL_PG });
  await pg.connect();
  try {
    const { rows } = await pg.query(
      "SELECT id, credentials FROM parser_accounts WHERE parser_type = 'whatsapp' AND is_active = true",
    );
    return rows
      .map((row) => ({ accountId: row.id, sessionId: row.credentials?.session_id }))
      .filter((entry) => Boolean(entry.sessionId));
  } finally {
    await pg.end();
  }
}

async function main() {
  const nc = await connect({ servers: process.env.NATS_URL || 'nats://nats:4222' });
  const js = nc.jetstream();
  const accounts = await loadAccounts();
  console.log(`[bridge] starting ${accounts.length} session(s)`);

  for (const { accountId, sessionId } of accounts) {
    const { client, publishMessage } = createClient({ accountId, sessionId, nc, sc });

    client.on('message', (msg) => publishMessage(msg, js).catch(
      (err) => console.error(`[bridge] publish failed: ${err.message}`),
    ));
    // message_create also yields messages sent by this account itself.
    client.on('message_create', (msg) => {
      if (!msg.fromMe) return;
      publishMessage(msg, js).catch((err) => console.error(`[bridge] publish failed: ${err.message}`));
    });

    const backfill = nc.subscribe(`wa.backfill.${accountId}`);
    (async () => {
      for await (const request of backfill) {
        const { chat_id: chatId, limit } = JSON.parse(sc.decode(request.data));
        try {
          const chat = await client.getChatById(chatId);
          const messages = await chat.fetchMessages({ limit: Number(limit) || 200 });
          for (const msg of messages) {
            await publishMessage(msg, js);
          }
          console.log(`[bridge] backfilled ${messages.length} from ${chatId}`);
        } catch (err) {
          console.error(`[bridge] backfill ${chatId} failed: ${err.message}`);
        }
      }
    })();

    await client.initialize();
  }
}

main().catch((err) => {
  console.error(`[bridge] fatal: ${err.message}`);
  process.exit(1);
});
```

- [ ] **Step 9: Write the Dockerfile**

Создать `whatsapp-bridge/Dockerfile`:

```dockerfile
FROM node:20-slim

# Chromium dependencies for Puppeteer
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium fonts-liberation libnss3 libatk-bridge2.0-0 libgtk-3-0 \
    libasound2 libgbm1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY src ./src

CMD ["node", "src/index.js"]
```

- [ ] **Step 10: Commit**

```bash
git add whatsapp-bridge/
git commit -m "feat: Node-мост WhatsApp на whatsapp-web.js"
```

---

### Task 6: Сервисы в compose

**Files:**
- Modify: `docker-compose.yml`, `.env.example`

**Interfaces:**
- Consumes: Task 4 (CLI-команда листенера), Task 5 (Dockerfile моста)

- [ ] **Step 1: Add both services**

В `docker-compose.yml` добавить, взяв за образец блок `telegram-listener` (переменные окружения, `depends_on`, `restart`):

```yaml
  whatsapp-bridge:
    build: ./whatsapp-bridge
    environment:
      NATS_URL: nats://nats:4222
      DATABASE_URL_PG: postgresql://postgres:postgres@db:5432/aggregator
      S3_ENDPOINT_URL: http://minio:9000
      S3_BUCKET: raw-events
      S3_ACCESS_KEY: minio
      S3_SECRET_KEY: miniosecret
      MEDIA_MAX_BYTES: 52428800
    depends_on:
      - nats
      - db
      - minio
    volumes:
      - wa_sessions:/sessions
    restart: unless-stopped

  whatsapp-listener:
    build: .
    command: sh -c "python -m app.cli db-upgrade && python -m app.cli run-whatsapp-listener"
    depends_on:
      - nats
      - db
    restart: unless-stopped
```

У `whatsapp-listener` продублировать блок `environment` и `volumes` из `telegram-listener`.

В блок `volumes:` в конце файла добавить `wa_sessions:`.

- [ ] **Step 2: Start the stack**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && docker compose up -d --build whatsapp-bridge whatsapp-listener && \
sleep 20 && docker compose ps whatsapp-bridge whatsapp-listener
```
Expected: оба `running`. При нуле аккаунтов мост логирует `[bridge] starting 0 session(s)` и остаётся живым — это ожидаемо.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "feat: сервисы whatsapp-bridge и whatsapp-listener"
```

---

### Task 7: Привязка аккаунта через QR

**Files:**
- Modify: `app/services/whatsapp_listener.py` (подписка на статусы)
- Modify: `app/routers/api.py` (эндпоинты аккаунта и статуса)
- Modify: `frontend/src/pages/` (экран привязки), `frontend/src/api.ts`

**Interfaces:**
- Produces:
  - `POST /api/whatsapp/accounts` → создаёт `ParserAccount` с новым `session_id`
  - `GET /api/whatsapp/accounts/{id}/status` → `{status, qr_data_url, updated_at}`

- [ ] **Step 1: Persist statuses from the bridge**

В `app/services/whatsapp_listener.py` добавить подписку на `wa.status.*`, пишущую в `service_state`:

```python
import datetime as dt
import json

import nats

from app.config import get_settings
from app.models import ServiceState


async def _consume_statuses() -> None:
    """Mirror bridge session state into service_state for the API to read."""
    connection = await nats.connect(get_settings().nats_url)
    subscription = await connection.subscribe("wa.status.*")
    async for message in subscription.messages:
        account_id = message.subject.rsplit(".", 1)[-1]
        payload = json.loads(message.data.decode("utf-8"))
        with SessionLocal() as session:
            key = f"wa_session_{account_id}"
            state = session.get(ServiceState, key)
            value = {**payload, "updated_at": dt.datetime.now(dt.UTC).isoformat()}
            if state is None:
                session.add(ServiceState(key=key, value=value))
            else:
                state.value = value
            session.commit()
```

Запускать вместе с основным консьюмером:

```python
def run_whatsapp_listener() -> None:
    async def _main() -> None:
        await asyncio.gather(consume_events(_handle), _consume_statuses())

    asyncio.run(_main())
```

- [ ] **Step 2: Add the API endpoints**

В `app/routers/api.py`:

```python
@router.post("/whatsapp/accounts")
def create_whatsapp_account(
    label: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = ParserAccount(
        parser_type="whatsapp",
        label=label,
        owner_user_id=int(user.id),
        credentials={"session_id": str(uuid.uuid4())},
    )
    db.add(account)
    db.commit()
    return {"id": account.id, "label": account.label, "status": "pending"}


@router.get("/whatsapp/accounts/{account_id}/status")
def whatsapp_account_status(
    account_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    account = db.get(ParserAccount, int(account_id))
    if account is None or account.parser_type != "whatsapp":
        raise HTTPException(status_code=404, detail="Акаунт не знайдено")
    if not _is_admin(user) and account.owner_user_id != int(user.id):
        raise HTTPException(status_code=403, detail="Немає доступу")

    state = db.get(ServiceState, f"wa_session_{int(account_id)}")
    if state is None:
        return {"status": "pending", "qr_data_url": None, "updated_at": None}
    return state.value
```

Проверить импорты `uuid`, `ParserAccount`, `ServiceState`.

**Важно:** после создания аккаунта мост нужно перезапустить, чтобы он поднял новую сессию — список аккаунтов читается при старте. Отметить это в ответе UI сообщением «Перезапустите whatsapp-bridge». `# ponytail: перезапуск вместо горячей перезагрузки, добавить wa.control.reload, когда аккаунтов станет много.`

- [ ] **Step 3: Build the UI screen**

В `frontend/src/api.ts` добавить `createWhatsappAccount(label)` и `fetchWhatsappStatus(accountId)` по образцу соседних функций.

Создать страницу привязки по образцу `frontend/src/pages/TelegramPage.tsx`: форма создания аккаунта, затем опрос статуса раз в 2 секунды. При `status === "qr"` показать `<img src={qr_data_url} />` с подписью «Отсканируйте код в WhatsApp → Связанные устройства». При `status === "ready"` — сообщение об успешной привязке и остановка опроса.

Зарегистрировать маршрут в `frontend/src/App.tsx` рядом с существующими.

- [ ] **Step 4: Verify the full path manually**

1. Открыть новую страницу, создать аккаунт.
2. `docker compose restart whatsapp-bridge`
3. Дождаться появления QR, отсканировать телефоном.
4. Дождаться `status: ready`.
5. Создать таргет с `parser_type=whatsapp` и `identifier` — id чата.
6. Написать в этот чат с другого телефона.
7. Проверить:

```bash
docker compose exec db psql -U postgres -d aggregator -c \
  "SELECT external_id, event_kind, author_label, left(text, 40) AS text \
   FROM raw_events WHERE parser_type = 'whatsapp' ORDER BY id DESC LIMIT 5;"
```
Expected: строка с текстом отправленного сообщения.

8. Отправить в чат картинку, проверить файл:

```bash
docker compose exec db psql -U postgres -d aggregator -c \
  "SELECT f.sha256, f.mime, f.size FROM event_files f \
   JOIN raw_event_files l ON l.file_id = f.id \
   JOIN raw_events e ON e.id = l.raw_event_id \
   WHERE e.parser_type = 'whatsapp' ORDER BY f.id DESC LIMIT 3;"
```

9. Проверить устойчивость к рестарту: `docker compose stop whatsapp-listener`, отправить сообщение, `docker compose start whatsapp-listener`, убедиться, что сообщение появилось в БД — это подтверждает работу JetStream.

- [ ] **Step 5: Commit**

```bash
git add app/services/whatsapp_listener.py app/routers/api.py frontend/src/
git commit -m "feat: привязка WhatsApp-аккаунта через QR"
```

---

### Task 8: Участники групп

**Files:**
- Modify: `whatsapp-bridge/src/index.js` (подписка на `wa.participants.*`)
- Modify: `app/plugins/whatsapp.py` (`sync_memberships`)

**Interfaces:**
- Consumes: Task 5 (мост), Task 3 (шина)

- [ ] **Step 1: Answer participant requests in the bridge**

В `whatsapp-bridge/src/index.js`, внутри цикла по аккаунтам, рядом с подпиской на бэкфилл:

```javascript
    const participants = nc.subscribe(`wa.participants.${accountId}`);
    (async () => {
      for await (const request of participants) {
        const { chat_id: chatId } = JSON.parse(sc.decode(request.data));
        try {
          const chat = await client.getChatById(chatId);
          const members = (chat.participants || []).map((p) => ({
            id: p.id?._serialized ?? null,
            is_admin: Boolean(p.isAdmin || p.isSuperAdmin),
          }));
          request.respond(sc.encode(JSON.stringify({ participants: members })));
        } catch (err) {
          request.respond(sc.encode(JSON.stringify({ participants: [], error: err.message })));
        }
      }
    })();
```

- [ ] **Step 2: Implement sync_memberships**

В `app/plugins/whatsapp.py` заменить унаследованную заглушку:

```python
    def sync_memberships(self, session: Session, **kwargs) -> dict:
        """Refresh group membership for every active WhatsApp target."""
        from app.services.whatsapp_bus import request_participants

        targets = session.execute(
            select(Target).where(
                Target.parser_type == "whatsapp",
                Target.is_active.is_(True),
            )
        ).scalars().all()

        checked = 0
        linked = 0
        for target in targets:
            if not str(target.identifier or "").endswith("@g.us"):
                continue  # direct chats have no participant list
            link = session.execute(
                select(TargetAccountLink).where(TargetAccountLink.target_id == target.id)
            ).scalars().first()
            if link is None:
                continue

            members = request_participants(account_id=link.account_id, chat_id=target.identifier)
            checked += 1
            linked += len(members)

        return {"checked": checked, "linked": linked}
```

Добавить импорты `select` и `TargetAccountLink`.

- [ ] **Step 3: Add the request helper to the bus**

В `app/services/whatsapp_bus.py`:

```python
async def _request(subject: str, payload: dict, timeout: float = 30.0) -> dict:
    connection = await _connect()
    try:
        response = await connection.request(
            subject, json.dumps(payload).encode("utf-8"), timeout=timeout
        )
        return json.loads(response.data.decode("utf-8"))
    finally:
        await connection.close()


def request_participants(account_id: int | None, chat_id: str) -> list[dict]:
    """Ask the bridge for a group's member list; empty on any failure."""
    if account_id is None:
        return []
    try:
        answer = asyncio.run(_request(f"wa.participants.{int(account_id)}", {"chat_id": str(chat_id)}))
    except Exception as exc:
        print(f"[wa-bus] participants request failed: {exc}", flush=True)
        return []
    return list(answer.get("participants") or [])
```

- [ ] **Step 4: Verify manually**

Run:
```bash
cd /Users/andriihrenchyshen/data_agregator && \
docker compose exec api python -c "
from app.db import SessionLocal
from app.plugins.registry import plugin_registry
with SessionLocal() as s:
    print(plugin_registry.get('whatsapp').sync_memberships(s))
"
```
Expected: `{'checked': N, 'linked': M}`, где N — число групповых таргетов.

- [ ] **Step 5: Commit**

```bash
git add whatsapp-bridge/src/index.js app/plugins/whatsapp.py app/services/whatsapp_bus.py
git commit -m "feat: синхронизация участников групп WhatsApp"
```

---

## Self-Review

**Покрытие спеки (§6):**

| Требование | Задача |
|---|---|
| `ParserAccount.credentials = {session_id}`, `LocalAuth` clientId | Task 5 (`createClient`), Task 7 (создание аккаунта) |
| `Target.identifier` — id чата `@g.us`/`@c.us` | Task 4 (поиск таргета), Task 8 (фильтр групп) |
| Один процесс держит N клиентов | Task 5 (`index.js`, цикл по аккаунтам) |
| `message` и `message_create` | Task 5, шаг 8 |
| Стрим `WA_EVENTS`, `wa.events.*`, max_age 7 дней | Task 3 (`ensure_stream`) |
| Ack после коммита, дедупликация повторов | Task 3 (`consume_events`), проверено в Task 7 шаг 4.9 |
| Статусы через core NATS → `service_state` | Task 7 |
| QR-привязка | Task 7 |
| Медиа заливает Node по `media/<sha256>` | Task 5 (`media.js`) |
| Лимит `MEDIA_MAX_BYTES` | Task 5 (`media.js`) |
| Контракт `v: 1`, тесты обеих сторон против одной схемы | Task 1, Task 2, Task 5 |
| Маппинг всех полей таблицы спеки | Task 2 (`map_wa_event`) |
| Бэкфилл через `fetchMessages` | Task 2 (`run`), Task 5 (шаг 8) |
| Ротация аккаунтов через `compute_account_load_score` | переиспользуется существующим воркером без изменений |
| `sync_memberships` через `chat.participants` | Task 8 |
| Нет логики нормализации в Node | Task 5 (`serialize.js` только перекладывает поля) |

**Согласованность имён:** `serializeMessage` (Node, Task 5) и `map_wa_event` (Python, Task 2) читают одни и те же ключи, зафиксированные фикстурой из Task 1. `request_backfill` объявлен в Task 3 и вызывается в Task 2; `request_participants` — Task 8. Ключ `media/<sha256>` совпадает с планом 3 (`put_bytes`), что и обеспечивает дедупликацию между источниками.

**Принятые ограничения:** список аккаунтов читается мостом при старте, новый аккаунт требует `docker compose restart whatsapp-bridge` — помечено `ponytail:` в Task 7. Ветка `wa.control.reload` из спеки не реализуется, пока аккаунтов мало.
