# Единая архитектура парсеров

Дата: 2026-09-17

## Проблема

Проект собирает данные из трёх типов источников (Telegram, darknet-форумы,
веб-сайты), и планируется четвёртый — WhatsApp. Текущая архитектура не даёт
добавить источник без правок в десятке мест.

**Payload бесструктурный.** `ParsedEvent` возвращает `payload: dict`
произвольной формы. Из-за этого `search_index.py` разбирает каждый источник
отдельно — ветки `if parser_type == "telegram"`, `elif parser_type ==
"darknet"`, `else: return None` — в трёх функциях: `_build_doc`,
`_payload_from_preview`, `_normalize_payload_for_index`. Новый источник
требует правок во всех трёх.

**`parser_type` — enum в БД.** Значения `telegram | darknet`. Каждый новый
источник требует миграции с `ALTER TYPE`. Расхождение уже произошло:
`job_routing.py:34` маршрутизирует очередь `"web"`, в compose крутится
`worker-web`, но `ParserType.web` не существует и плагина нет — очередь
мёртвая по построению.

**JSON хранится в S3, а читается поштучно.** `object_store.put_json` пишет
payload в MinIO, `raw_events` хранит ссылку. Чтение идёт через
`object_store.get_json` — 13 вызовов в `api.py`/`ui.py`, часть внутри циклов
по событиям. Это N+1 по сети: список из 100 событий даёт 100 запросов
`GetObject`. При этом `put_json` всегда возвращает `payload_inline=None`,
поэтому проверка `event.payload if isinstance(event.payload, dict) else
get_json(event)` никогда не срабатывает в первую ветку — в S3 ходят всегда.

Запись в S3 и запись строки в БД — две операции без общей транзакции.
Рассинхрон возможен, и код это уже знает: `worker.py:342` содержит
self-heal «if the old object was lost».

**Поиск обрезан.** `_build_preview` режет текст до 200 символов
(`text[:200]`). Полный текст есть только в S3. Поскольку `payload_inline`
всегда `None`, в OpenSearch уходит именно превью — **сообщения длиннее 200
символов не находятся по своему тексту**.

**OpenSearch дорог и хрупок.** ~1 GB RAM (heap 512m + оверхед JVM). В
`search_index.py` полсотни строк на борьбу с самим движком:
`_try_clear_cluster_create_block`, `_try_clear_read_only_block`, обход
flood-stage watermark. Требует `vm.max_map_count=262144` на хосте. При
выключенном OpenSearch поиск отдаёт 503 — фолбэка нет.

## Решение

Нормализованный слой поверх сырого payload; Postgres как единственное
хранилище событий и поиска; S3 — только под файлы; WhatsApp через Node-мост
и NATS.

### 1. Контракт плагина

Плагин возвращает сырой JSON источника **и** заполняет общие поля. Сырое
сохраняется всегда — специфика источника не теряется. Нормализация поверх,
а не вместо.

```python
@dataclass(slots=True)
class FileRef:
    source_ref: str              # file_id (TG) / media key (WA) / URL (web)
    filename: str | None = None
    mime: str | None = None
    size: int | None = None

@dataclass(slots=True)
class ParsedEvent:
    external_id: str | None
    observed_at: dt.datetime | None
    payload: dict                        # сырой JSON источника, as is
    text: str | None = None
    author_id: str | None = None
    author_label: str | None = None
    event_kind: str = "message"
    thread_id: str | None = None
    reply_to: str | None = None
    files: list[FileRef] = field(default_factory=list)
```

Решения и обоснования:

- **`author_id` — строка.** Telegram даёт int, WhatsApp — `380xx@c.us`,
  форумы — строковый ник. Текущий `search_index._build_doc` делает
  `int(sender_id)` в `try/except`; на WhatsApp это неверно по типу.
- **`event_kind` — строка, не enum.** Значения на старте:
  `message | comment | post | reply`. Источники дают разные типы; закрытый
  список потребовал бы расширения при каждом новом источнике. Строка
  расширяется без миграции.
- **`files` — общий список.** Медиа есть в Telegram, WhatsApp и как
  вложения на форумах. Один механизм на все источники.

После этого индексация и поиск не знают про источники: читают `text`,
`author_label`, `event_kind`. Три ветки `_build_doc` схлопываются в одну.

### 2. `parser_type` как открытый реестр

Колонка `parser_type` меняет тип с `Enum` на `String`. Источник истины —
`plugin_registry`; валидация входных значений через
`plugin_registry.list_types()`.

Новый источник = файл плагина + строка регистрации. Миграции не требуется.

Мёртвая очередь `web`: `worker-web` удаляется из compose (плагина нет,
чинить нечего). Маршрутизация в `job_routing.py` остаётся — заработает,
когда появится web-плагин.

### 3. Схема БД

`raw_events` становится единственным местом правды по событию. Поля
`storage_type`, `payload_ref`, `payload_sha256`, `payload_size`,
`payload_preview` удаляются — они существовали ради S3-ссылок на JSON.

```python
class RawEvent(Base):
    __tablename__ = "raw_events"
    # без изменений: id, target_id, account_id, owner_user_id,
    #                external_id, observed_at, created_at
    parser_type:   Mapped[str]  = mapped_column(String(32), index=True)
    payload:       Mapped[dict] = mapped_column(JSONB, nullable=False)
    text:          Mapped[str | None]
    author_id:     Mapped[str | None] = mapped_column(String(128), index=True)
    author_label:  Mapped[str | None] = mapped_column(String(255))
    event_kind:    Mapped[str]        = mapped_column(String(32), index=True)
    thread_id:     Mapped[str | None] = mapped_column(String(128), index=True)
    reply_to:      Mapped[str | None] = mapped_column(String(128))
```

`payload` объявлен `NOT NULL`: событие без сырого JSON бессмысленно, а
nullable порождает проверки `if isinstance(payload, dict)` по всему коду
(сейчас таких 13).

`text` вынесен в колонку, хотя дублирует `payload->>'text'`: по нему
строится `tsvector` и он читается в каждом листинге. Цена в байтах мала,
выигрыш в простоте запросов существенный.

### 4. Поиск на Postgres FTS

```sql
ALTER TABLE raw_events ADD COLUMN text_search tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text,''))) STORED;
CREATE INDEX ix_raw_events_text_search ON raw_events USING GIN (text_search);
```

Конфигурация `simple`, не `russian`/`english`: данные многоязычные, стеммер
под один язык портит остальные. Переход на морфологию позже — смена
конфигурации, без переписывания кода.

Generated-колонка вместо триггера: Postgres поддерживает актуальность сам.

`search_messages` переписывается на `websearch_to_tsquery` (синтаксис в
стиле поисковика: кавычки, `or`, `-`), `ts_rank` для ранжирования,
`ts_headline` для подсветки сниппетов — сохраняя то, что сейчас даёт
OpenSearch. Фильтры `owner_user_id` / `parser_type` / `target_id`
становятся обычными `WHERE`.

Поиск идёт по полному тексту, а не по 200-символьному превью.

Удаляются: `opensearch` из compose, `app/services/search_index.py`,
`app/services/search_autosync.py`, `opensearch-py` из requirements,
миграция 0009 (autosync-стейт), переменные окружения OpenSearch.

### 5. Файлы в S3

```python
class EventFile(Base):
    __tablename__ = "event_files"
    id
    sha256:      Mapped[str] = mapped_column(String(64), unique=True, index=True)
    storage_key: Mapped[str]              # media/<sha256>
    mime, size, filename, created_at

class RawEventFile(Base):
    __tablename__ = "raw_event_files"
    raw_event_id, file_id, source_ref, position
```

Связь many-to-many, а не FK на событие: в Telegram и WhatsApp один файл
пересылается многократно. Дедупликация по `sha256` даёт кратную экономию
диска и возможна только при m2m.

`ObjectStore`: `put_json` удаляется, добавляются
`put_bytes(data, sha256, mime) -> storage_key` (с проверкой «объект уже
есть — не заливать») и `presigned_url(key, ttl)` для отдачи во фронтенд.

Скачивание медиа должно иметь лимит размера — иначе одно большое видео
забьёт диск.

### 6. WhatsApp

Библиотека — [whatsapp-web.js](https://github.com/wwebjs/whatsapp-web.js):
Node + Puppeteer, то есть headless Chromium (~400 MB RAM на аккаунт).
Python с ним напрямую не работает, нужен мост.

**Node-сервис `whatsapp-bridge/`** — новый контейнер:

- `whatsapp-web.js` + `LocalAuth`, сессии в volume `wa_sessions` (иначе QR
  при каждом рестарте)
- один процесс держит N клиентов, по одному на `ParserAccount`
- подписка на `message` / `message_create` → публикация в NATS
  `wa.events.<account_id>`
- служебный канал `wa.control.<account_id>`: QR для привязки, статус
  сессии, запрос бэкфилла
- медиа: `msg.downloadMedia()` → файл заливается **в S3 прямо из Node**, в
  NATS уходят только `sha256` + `storage_key`

Отступление от принципа «Node не пишет в хранилище» здесь осознанное: у
NATS дефолтный лимит сообщения 1 MB, гонять через брокер base64 нельзя.
Логики нормализации в Node нет — только `put_object` по посчитанному хешу,
поэтому контракт не размывается.

**Python-воркер `worker-whatsapp`**: читает NATS, собирает `ParsedEvent` с
нормализованными полями, дальше — общий путь записи, тот же что у Telegram.

Маппинг:

| ParsedEvent | WhatsApp |
|---|---|
| `external_id` | `msg.id._serialized` |
| `author_id` | `msg.author \|\| msg.from` |
| `author_label` | pushname / номер |
| `event_kind` | `message`, либо `reply` при `hasQuotedMsg` |
| `thread_id` | `msg.from` (id чата) |
| `reply_to` | `quotedMsg.id._serialized` |
| `files` | `FileRef` из `downloadMedia()` |

Переносится из Telegram-функционала: `generate_jobs` / `run` в плагине,
ротация аккаунтов (`compute_account_load_score` переиспользуется как есть),
лайв-приём, синк участников через `chat.participants` для групп, привязка
аккаунта через UI.

**Отличия от Telegram, принятые как данность:**

1. **Глубокого бэкфилла нет.** Telegram отдаёт всю историю через
   `iter_messages`. WhatsApp — только то, что есть в сессии
   (`chat.fetchMessages({limit})`, обычно последние сотни сообщений).
   Логика `_build_backfill_ranges` / `_full_backfill_state` не переносится.
2. **Привязка через QR** — сканирование с телефона; сессия живёт, пока
   телефон не разлогинит. Нужен экран в UI с QR из `wa.control`.
3. **Риск блокировки.** whatsapp-web.js — неофициальный клиент,
   автоматизация нарушает ToS WhatsApp; аккаунты банят.
4. **Хрупкость.** Библиотека ломается при обновлениях WhatsApp Web.

## Миграция данных

Накопленные данные не переносятся — чистый старт (решение принято при
проектировании). `raw_events` пересоздаётся миграцией `0014`. Backfill-скрипт
и обратная совместимость чтения не требуются, второй ветки чтения в коде не
остаётся.

## Порядок работ

Каждый шаг оставляет систему в рабочем состоянии.

1. **Контракт** — `ParsedEvent` + `FileRef`, `parser_type` → String, реестр
   как источник истины. Telegram/darknet заполняют нормализованные поля.
2. **Схема БД** — миграция `0014`: JSONB, нормализованные колонки,
   `text_search` + GIN, `event_files` / `raw_event_files`; снос S3-полей.
3. **Запись** — `worker.py` и `telegram_listener.py` пишут payload в JSONB;
   удаляются `put_json` и self-heal из `worker.py:342`.
4. **Поиск на Postgres** — переписать `search_messages`, снять OpenSearch.
5. **Файлы** — `put_bytes` / `presigned_url`, скачивание медиа в Telegram,
   отдача во фронтенде.
6. **WhatsApp** — Node-мост, NATS, плагин, воркер, QR-экран.

Шаги 1–3 — фундамент и выполняются подряд. Шаги 4–6 независимы друг от
друга.

## Затрагиваемый код

- 13 вызовов `object_store.get_json(event)` в `api.py` / `ui.py` →
  упрощаются до `event.payload`
- фронтенд: страница поиска (формат `snippet` теперь от `ts_headline`),
  карточка события (`payload_ref` исчезает — `api.py:4587`, `ui.py:542`)
- `app/services/darknet_profiles.py` — читает payload, сверить с новым
  источником
- существующие тесты на запись событий
- `.env.example`, `README.md`, `docker-compose.yml`

## Тестирование

- юнит-тесты маппинга в `ParsedEvent` для каждого источника
- дедупликация файлов по `sha256`
- FTS-запрос: ранжирование и подсветка
- контрактный тест формата NATS-сообщения (без живого WhatsApp)

Живая WhatsApp-сессия автотестами не покрывается — ручная проверка.

## Влияние на инфраструктуру

Снятие OpenSearch освобождает ~1 GB RAM; освободившееся место занимает
Chromium под WhatsApp.

Рекомендация по VPS: **4 vCPU / 8 GB RAM / NVMe**, чего хватает на 2–3
WhatsApp-аккаунта. Больше аккаунтов — 16 GB или вынос моста на отдельный
хост.

Диск определяется объёмом медиа, а не JSON: сообщения — десятки килобайт,
файлы — мегабайты. При большом объёме медиа имеет смысл внешний S3
(Backblaze B2, Hetzner Object Storage) вместо локального MinIO.
