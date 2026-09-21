# Единая архитектура парсеров

Дата: 2026-09-17, ревизия 2026-09-21

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

**`parser_type` — enum в БД.** Значения `telegram | darknet`, колонка типа
`Enum` в четырёх таблицах: `targets`, `parser_accounts`, `parse_jobs`,
`raw_events`. Каждый новый источник требует миграции с `ALTER TYPE`.
Расхождение уже произошло: `job_routing.py:34` маршрутизирует очередь
`"web"`, в compose крутится `worker-web`, но `ParserType.web` не существует
и плагина нет — очередь мёртвая по построению.

**JSON хранится в S3, а читается поштучно.** `object_store.put_json` пишет
payload в MinIO, `raw_events` хранит ссылку. Чтение идёт через
`object_store.get_json` — 13 вызовов в `api.py`/`ui.py`, часть внутри циклов
по событиям. Это N+1 по сети: список из 100 событий даёт 100 запросов
`GetObject`. При этом `put_json` всегда возвращает `payload_inline=None`,
поэтому проверка `event.payload if isinstance(event.payload, dict) else
get_json(event)` никогда не срабатывает в первую ветку — в S3 ходят всегда.

Запись в S3 и запись строки в БД — две операции без общей транзакции.
Рассинхрон возможен, и код это уже знает: `worker.py:342` содержит
self-heal «if the old object was lost». При недоступном MinIO `put_json`
молча пишет в `data/raw/` на диск — данные расползаются по двум местам.

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
и NATS JetStream.

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
    sha256: str | None = None    # заполнен, если файл уже лежит в S3 (WA-мост)

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
  вложения на форумах. Один механизм на все источники. `FileRef.sha256`
  заполняется, когда файл уже загружен в S3 до попадания в Python (случай
  WhatsApp-моста, см. §6); иначе Python скачивает по `source_ref` сам.

После этого индексация и поиск не знают про источники: читают `text`,
`author_label`, `event_kind`. Три ветки `_build_doc` схлопываются в одну.

Контракт `ParserPlugin` (`generate_jobs` / `run` / `sync_memberships`) не
меняется. Источники с push-моделью (Telegram live, WhatsApp) дополнительно
имеют долгоживущий listener-процесс, который собирает `ParsedEvent` и
отдаёт их в общую функцию записи — ту же, что вызывает `worker.py` после
`run`. Запись событий выносится из `worker.py` в одну функцию
`persist_events(session, target, account, events)`, чтобы у воркера и
листенеров был один путь.

### 2. `parser_type` как открытый реестр

Все четыре колонки `parser_type` (`targets`, `parser_accounts`,
`parse_jobs`, `raw_events`) меняют тип с `Enum` на `String(32)`. Источник
истины — `plugin_registry`; валидация входных значений через
`plugin_registry.list_types()` на границах API.

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
    text:          Mapped[str | None] = mapped_column(Text)
    author_id:     Mapped[str | None] = mapped_column(String(128), index=True)
    author_label:  Mapped[str | None] = mapped_column(String(255))
    event_kind:    Mapped[str]        = mapped_column(String(32), index=True)
    thread_id:     Mapped[str | None] = mapped_column(String(128), index=True)
    reply_to:      Mapped[str | None] = mapped_column(String(128))
```

Уникальный индекс `uq_raw_events_parser_target_external`
(`parser_type, target_id, external_id`) сохраняется — это и есть
дедупликация событий. Все `external_id` новых источников должны быть
уникальны в пределах таргета (для WhatsApp это `msg.id._serialized`).

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
под один язык портит остальные. Generated-колонка вместо триггера: Postgres
поддерживает актуальность сам.

**Известный потолок `simple`:** нет морфологии — запрос «сообщение» не
найдёт «сообщения». Поиск по точным словам и фразам работает, по
словоформам — нет. Путь апгрейда, когда понадобится: `to_tsvector('russian',
…) || to_tsvector('english', …)` в той же generated-колонке, либо `pg_trgm`
для подстрок и опечаток. Оба — смена одного выражения в миграции, код
поиска не меняется.

`search_messages` переписывается на `websearch_to_tsquery` (синтаксис в
стиле поисковика: кавычки, `or`, `-`), `ts_rank` для ранжирования,
`ts_headline` для подсветки сниппетов. `ts_headline` дорогая — вызывается
только над уже отобранными `LIMIT N` строками (подзапрос), не над всеми
совпадениями. Фильтры `owner_user_id` / `parser_type` / `target_id` — обычные
`WHERE`.

Поиск идёт по полному тексту, а не по 200-символьному превью.

Удаляются: `opensearch` из compose, `app/services/search_index.py`,
`app/services/search_autosync.py`, `opensearch-py` из requirements,
переменные окружения OpenSearch. Миграция `0009_service_state_autosync`
**остаётся в дереве** — на неё ссылается `down_revision` миграции 0010,
удаление файла сломает цепочку Alembic. Строку `opensearch_autosync_v1` из
`service_state` удаляет миграция 0014.

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
    # PK (raw_event_id, file_id)
```

Связь many-to-many, а не FK на событие: в Telegram и WhatsApp один файл
пересылается многократно. Дедупликация по `sha256` даёт кратную экономию
диска и возможна только при m2m.

`ObjectStore`: `put_json` / `get_json` удаляются, добавляются
`put_bytes(data, sha256, mime) -> storage_key` (с `head_object` перед
загрузкой — если объект уже есть, не заливать) и `presigned_url(key, ttl)`
для отдачи во фронтенд.

**Фолбэк на локальный диск удаляется.** Сейчас при недоступном MinIO
`put_json` молча пишет в `data/raw/` — так данные расползаются по двум
местам. Новое поведение: MinIO недоступен → файл не сохраняется, событие
сохраняется без связи с файлом, ошибка в лог. Пропущенные файлы не
довыкачиваются автоматически (ponytail: добавить ретрай-очередь, когда
пропуски станут заметны).

**Лимит размера:** настройка `media_max_bytes`, по умолчанию 50 MB. Файл
больше лимита не скачивается; в `raw_event_files` связь не создаётся;
`payload` сохраняет исходные метаданные (размер, имя) — видно, что файл был.

Ключ `media/<sha256>` един для всех источников: Telegram-медиа заливает
Python-воркер, WhatsApp-медиа — Node-мост (§6). Оба считают sha256 по
байтам, оба пишут по одному и тому же ключу, дедупликация работает поверх
источников.

### 6. WhatsApp

Библиотека — [whatsapp-web.js](https://github.com/wwebjs/whatsapp-web.js):
Node + Puppeteer, то есть headless Chromium (~400 MB RAM на аккаунт).
Python с ним напрямую не работает, нужен мост.

#### Модель данных

- **`ParserAccount`** с `parser_type="whatsapp"`. `credentials =
  {"session_id": "<uuid>"}`. `session_id` — это `clientId` для `LocalAuth`;
  директория сессии `wa_sessions/<session_id>/` в volume. Ротация аккаунтов
  через существующий `compute_account_load_score`.
- **`Target`** с `parser_type="whatsapp"`. `identifier` — id чата WhatsApp:
  `<digits>@g.us` для групп, `<digits>@c.us` для личных. Аналог
  `@channel` / chat id в Telegram.
- Привязка таргета к аккаунту — существующая `TargetAccountLink`.

#### Node-сервис `whatsapp-bridge/` (новый контейнер)

- `whatsapp-web.js` + `LocalAuth`, сессии в volume `wa_sessions`
- один процесс держит N клиентов, по одному на активный WhatsApp-аккаунт;
  список аккаунтов получает из Postgres при старте и по сигналу
  `wa.control.reload`
- события `message` и `message_create` → публикация в JetStream-стрим
  `WA_EVENTS`, subject `wa.events.<account_id>`. `message_create` даёт
  исходящие сообщения самого аккаунта; они нужны (в Telegram сохраняются
  все сообщения чата). Одно сообщение может прийти обоими событиями —
  дедупликация по `external_id` на стороне Python это гасит.
- статус сессии (`qr` / `authenticated` / `ready` / `disconnected`) и
  QR-код → subject `wa.status.<account_id>`
- медиа: `msg.downloadMedia()` → sha256 → `put_object` в MinIO по ключу
  `media/<sha256>` (с проверкой `head_object`, лимит `media_max_bytes`) → в
  событие уходят только `sha256`, `mime`, `size`, `filename`
- бэкфилл: request/reply на `wa.backfill.<account_id>` с `{chat_id,
  limit}` → мост вызывает `chat.fetchMessages({limit})` и публикует
  результат в тот же `WA_EVENTS`

Отступление от принципа «Node не пишет в хранилище» — заливка медиа прямо
из Node — осознанное: у NATS лимит сообщения 1 MB по умолчанию, гонять
base64 через брокер нельзя. Логики нормализации в Node нет — только
`put_object` по посчитанному хешу, поэтому контракт не размывается.

#### Почему JetStream, а не core NATS

Core NATS — fire-and-forget: если Python-потребитель лежит, сообщения
теряются. JetStream хранит стрим на диске (`-js -sd /data` уже в compose) и
отдаёт durable-консьюмеру после рестарта. Ack сообщения — **после** коммита
в Postgres; упавший между чтением и коммитом процесс получит сообщение
повторно, и уникальный индекс `raw_events` погасит дубль.

Стрим `WA_EVENTS`: subjects `wa.events.*`, retention `limits`, max_age 7
дней. Статусы (`wa.status.*`) — core NATS, они не нужны после факта.

#### Контракт NATS-сообщения `wa.events.<account_id>`

```json
{
  "v": 1,
  "account_id": 12,
  "chat_id": "120363012345678901@g.us",
  "message_id": "true_120363012345678901@g.us_3EB0…",
  "timestamp": 1758400000,
  "from": "380671234567@c.us",
  "author": "380671234567@c.us",
  "author_name": "Andrii",
  "body": "текст сообщения",
  "type": "chat",
  "has_quoted": false,
  "quoted_message_id": null,
  "media": {
    "sha256": "…", "mime": "image/jpeg", "size": 123456, "filename": null
  },
  "raw": { "…полный объект Message из whatsapp-web.js, сериализованный…" }
}
```

`raw` — сырой payload для `RawEvent.payload`; остальные поля — то, что
Python маппит в нормализованный `ParsedEvent`, не разбирая `raw`. Поле `v`
— версия контракта; контрактный тест проверяет, что Python-маппер и
Node-сериализатор согласны по этой схеме.

#### Python-сторона

- `app/plugins/whatsapp.py` — `WhatsAppPlugin(ParserPlugin)`:
  `generate_jobs` создаёт задания бэкфилла для новых таргетов; `run`
  отправляет request на `wa.backfill.<account_id>` и завершается (события
  придут через стрим); `sync_memberships` через `chat.participants` (запрос
  в мост на `wa.participants.<account_id>`).
- `app/services/whatsapp_listener.py` — durable-консьюмер `WA_EVENTS`,
  маппит сообщение в `ParsedEvent`, вызывает `persist_events`, ack после
  коммита. Зеркало `telegram_listener.py`.
- Статус сессии и QR: listener подписан на `wa.status.*` и пишет в
  существующую таблицу `service_state` ключ `wa_session_<account_id>` =
  `{status, qr_data_url, updated_at}`. API отдаёт это поле, фронтенд
  опрашивает раз в 2 с на экране привязки. Новых транспортов между API и
  мостом не заводим.
- Контейнер в compose: `whatsapp-listener` (по аналогии с
  `telegram-listener`).

Маппинг в `ParsedEvent`:

| ParsedEvent | из NATS-сообщения |
|---|---|
| `external_id` | `message_id` |
| `observed_at` | `timestamp` |
| `payload` | `raw` |
| `text` | `body` |
| `author_id` | `author` или `from` |
| `author_label` | `author_name` или номер из `author` |
| `event_kind` | `reply` если `has_quoted`, иначе `message` |
| `thread_id` | `chat_id` |
| `reply_to` | `quoted_message_id` |
| `files` | `[FileRef(source_ref=message_id, sha256=…, mime, size, filename)]` если `media` |

#### Отличия от Telegram, принятые как данность

1. **Глубокого бэкфилла нет.** Telegram отдаёт всю историю через
   `iter_messages`. WhatsApp — только то, что есть в сессии
   (`chat.fetchMessages({limit})`, обычно последние сотни сообщений).
   Логика `_build_backfill_ranges` / `_full_backfill_state` не переносится.
2. **Привязка через QR** — сканирование с телефона; сессия живёт, пока
   телефон не разлогинит.
3. **Риск блокировки.** whatsapp-web.js — неофициальный клиент,
   автоматизация нарушает ToS WhatsApp; аккаунты банят.
4. **Хрупкость.** Библиотека ломается при обновлениях WhatsApp Web.

## Миграция данных

Накопленные данные не переносятся — чистый старт (решение принято при
проектировании). `raw_events` пересоздаётся миграцией `0014`. Backfill-скрипт
и обратная совместимость чтения не требуются, второй ветки чтения в коде не
остаётся.

## Порядок работ

1. **Ядро** — один этап, между его частями система неработоспособна, поэтому
   выполняется целиком до первого запуска:
   - `ParsedEvent` + `FileRef`; `parser_type` → String в четырёх таблицах;
     реестр как источник истины
   - миграция `0014`: JSONB, нормализованные колонки, `text_search` + GIN,
     `event_files` / `raw_event_files`; снос S3-полей; чистка
     `service_state`
   - `persist_events` как единая функция записи; `worker.py` и
     `telegram_listener.py` переходят на неё; Telegram и darknet заполняют
     нормализованные поля
   - удаление `put_json` / `get_json`, self-heal в `worker.py:342`, 13
     вызовов `get_json` → `event.payload`
2. **Поиск на Postgres** — переписать `search_messages`, снять OpenSearch.
3. **Файлы** — `put_bytes` / `presigned_url`, скачивание медиа в Telegram,
   отдача во фронтенде.
4. **WhatsApp** — Node-мост, JetStream-стрим, плагин, listener, QR-экран.

Этап 1 — фундамент. Этапы 2–4 независимы друг от друга и выполняются в
любом порядке после него.

## Затрагиваемый код

- 13 вызовов `object_store.get_json(event)` в `api.py` / `ui.py` →
  `event.payload`
- фронтенд: страница поиска (формат `snippet` теперь от `ts_headline`),
  карточка события (`payload_ref` исчезает — `api.py:4587`, `ui.py:542`),
  новый экран привязки WhatsApp с QR
- `app/services/darknet_profiles.py` — читает payload, сверить с новым
  источником
- существующие тесты на запись событий
- `.env.example`, `README.md`, `docker-compose.yml` (минус `opensearch`,
  `worker-web`; плюс `whatsapp-bridge`, `whatsapp-listener`, volume
  `wa_sessions`)

## Тестирование

- юнит-тесты маппинга в `ParsedEvent` для каждого источника
- `persist_events`: дедупликация по уникальному индексу, повторная доставка
  того же события не создаёт дубль
- дедупликация файлов по `sha256`: два события с одним файлом → одна строка
  `event_files`, две в `raw_event_files`
- FTS-запрос: ранжирование и подсветка
- контрактный тест NATS-сообщения: фикстура JSON по схеме `v: 1`,
  Python-маппер даёт ожидаемый `ParsedEvent`; на стороне Node — тест, что
  сериализатор выдаёт объект по той же схеме (без живого WhatsApp)

Живая WhatsApp-сессия автотестами не покрывается — ручная проверка по
чек-листу: привязка по QR, приём входящего, приём исходящего, медиа в S3,
рестарт listener без потери событий из стрима.

## Влияние на инфраструктуру

Снятие OpenSearch освобождает ~1 GB RAM; освободившееся место занимает
Chromium под WhatsApp.

Рекомендация по VPS: **4 vCPU / 8 GB RAM / NVMe**, чего хватает на 2–3
WhatsApp-аккаунта. Больше аккаунтов — 16 GB или вынос моста на отдельный
хост.

Диск определяется объёмом медиа, а не JSON: сообщения — десятки килобайт,
файлы — мегабайты. При большом объёме медиа имеет смысл внешний S3
(Backblaze B2, Hetzner Object Storage) вместо локального MinIO.
