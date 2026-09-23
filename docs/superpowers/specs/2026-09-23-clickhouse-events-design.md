# События в ClickHouse + асинхронный поисковый API

**Дата:** 2026-09-23. **Статус:** утверждено пользователем в диалоге ("решено, переезжай на clickhouse").

## Цель

Хранить собранные события (сообщения Telegram, посты форумов, WhatsApp) в
ClickHouse и искать по ним при объёме порядка миллиарда строк. Поиск нужен по:
нику отправителя, tg id отправителя, телефону отправителя, словам в тексте,
каналу, периоду. Postgres остаётся источником истины для целей, аккаунтов,
заданий, пользователей и связей.

## Почему ClickHouse, а не Doris

Решение принято после сравнения: один бинарник и штатный одноузловой режим при
одном операторе; стабильный текстовый индекс (`TYPE text`, 2026); `ngrambf_v1`
для подстрок; экосистема. Doris выигрывал только точной дедупликацией при
записи и BM25-ранжированием, оба не критичны для расследовательского поиска
"все упоминания, по времени".

## Стратегия переезда: dual-write

Полный перенос сломал бы дашборд, страницу данных, профили и офсеты, которые
читают `raw_events`. Поэтому первый шаг стандартный:

1. `persist_events` пишет в Postgres как раньше **и** в ClickHouse.
2. Разовый backfill переливает существующие `raw_events` в ClickHouse.
3. Новый поисковый API читает только ClickHouse.
4. Позже (вне этой спеки): остальные читатели переводятся, запись в Postgres
   `raw_events` отключается.

Горячий путь пишет через серверный `async_insert=1, wait_for_async_insert=1`:
listener сохраняет по одному сообщению за вызов, и без этого каждая вставка
становилась бы отдельным куском на диске ("Too many parts"). Сервер копит
мелкие вставки и сбрасывает их пачками; подтверждение приходит после сброса,
поэтому потерь нет. Backfill пишет обычными пачками по 5000 строк.

Запись в ClickHouse не должна валить задание сбора: при недоступности
ClickHouse ошибка логируется уровнем ERROR, а backfill идемпотентен
(ReplacingMergeTree), поэтому пропуск восстанавливается повторным запуском
`python -m app.cli ch-backfill`.

## Схема ClickHouse

База `aggregator` (настройка `CLICKHOUSE_DATABASE`), таблица `events`:

| Колонка | Тип | Источник |
|---|---|---|
| parser_type | LowCardinality(String) | RawEvent.parser_type |
| target_id | UInt32 | RawEvent.target_id |
| account_id | Nullable(UInt32) | RawEvent.account_id |
| owner_user_id | Nullable(UInt32) | RawEvent.owner_user_id |
| external_id | String | RawEvent.external_id или '' |
| event_key | String | external_id, а если пусто — uuid4 hex (иначе ReplacingMergeTree склеил бы все события без id) |
| observed_at | DateTime64(3, 'UTC') | RawEvent.observed_at, при NULL — время вставки |
| event_kind | LowCardinality(String) | RawEvent.event_kind |
| thread_id, reply_to | String | RawEvent.* или '' |
| author_id | String | RawEvent.author_id или '' |
| author_label | String | RawEvent.author_label или '' |
| sender_phone | String | payload.sender.phone (только цифры) или '' |
| text | String | RawEvent.text или '' |
| text_lower | String MATERIALIZED lowerUTF8(text) | служебная, только для индексов и поиска |
| payload | String CODEC(ZSTD(3)) | json.dumps(payload) через `app.db.json_serializer` |
| ingested_at | DateTime64(3, 'UTC') DEFAULT now64(3) | версия для ReplacingMergeTree |

Движок: `ReplacingMergeTree(ingested_at) PARTITION BY toYYYYMM(observed_at)
ORDER BY (parser_type, target_id, event_key)`.

**Почему нужна колонка `text_lower`.** Проверено на ClickHouse 26.9.1:
`hasAllTokens` чувствителен к регистру, а `hasTokenCaseInsensitive` складывает
регистр только для ASCII и на кириллице не находит ничего (`hasAllTokens(text,
'курьеры')` не матчит `'Курьеры'`). Поэтому индексы строятся на
`lowerUTF8(text)`, а запрос приводится тем же `lowerUTF8`. Оригинальный `text`
остаётся нетронутым и отдаётся в ответе.

Индексы пропуска (все на `text_lower`):

- `INDEX idx_text text_lower TYPE text(tokenizer = splitByNonAlpha) GRANULARITY 1` —
  слова в тексте. Если образ не знает тип `text`, таблица создаётся без него, а
  поиск по словам работает через `hasToken` по той же колонке (медленнее, но
  корректно).
- `INDEX idx_text_ngram text_lower TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 4` —
  подстроки (`contains`).
- `INDEX idx_author_id author_id TYPE bloom_filter GRANULARITY 4`.
- `INDEX idx_phone sender_phone TYPE bloom_filter GRANULARITY 4`.

Схема создаётся идемпотентно (`CREATE DATABASE IF NOT EXISTS`, `CREATE TABLE IF
NOT EXISTS`) при старте API и перед backfill. Alembic для ClickHouse не
используется.

## Поисковый API

`GET /api/v2/events` — `async def`, async-клиент `clickhouse-connect`,
созданный один раз в lifespan приложения. Авторизация — существующая сессия
(`get_current_user`); не-админ видит только события со своим `owner_user_id`.

Параметры (все необязательные):

| Параметр | Семантика в SQL |
|---|---|
| `q` | слова в тексте: `hasAllTokens(text_lower, lowerUTF8(x))`; без текстового индекса — `AND hasToken(text_lower, lowerUTF8(tok))` по каждому токену |
| `contains` | подстрока без учёта регистра: `position(text_lower, lowerUTF8(x)) > 0`, ускоряется ngrambf |
| `author_id` | точное равенство |
| `nickname` | `positionCaseInsensitiveUTF8(author_label, x) > 0` |
| `phone` | равенство по цифрам (нецифры отбрасываются) |
| `target_id` | равенство |
| `parser_type` | равенство |
| `since`, `until` | границы `observed_at`, ISO 8601 |
| `limit` | 1..500, по умолчанию 100 |
| `cursor` | непрозрачная строка из предыдущего ответа |

Сортировка `observed_at DESC, event_key DESC`. Пагинация курсором
`base64(observed_at_ms:event_key)`; страница — `limit+1` строк, лишняя даёт
`next_cursor`. Точного `total` нет намеренно: именно он в Postgres уходил в
полный перебор.

Ответ:

```json
{"items": [{"parser_type": "telegram", "target_id": 16, "external_id": "548",
            "observed_at": "2026-09-11T16:04:02.000+00:00", "event_kind": "message",
            "author_id": "1006503122", "author_label": "ID 1006503122",
            "sender_phone": "", "text": "...", "thread_id": "", "reply_to": ""}],
 "next_cursor": "MTc1Nz...", "limit": 100}
```

`payload` в списке не отдаётся (тяжёлый); `GET /api/v2/events/{parser_type}/{target_id}/{event_key}`
возвращает одну запись с `payload` (распарсенный JSON).

Запросы используют серверное связывание параметров `{name:Type}`; строки в SQL
не конкатенируются.

## Инфраструктура

- Сервис `clickhouse` в docker-compose: `clickhouse/clickhouse-server:latest`,
  порт только `127.0.0.1:8123` (HTTP; нативный 9000 занят MinIO и не нужен), том `chdata`, `ulimits
  nofile 262144`, `CLICKHOUSE_USER/PASSWORD` из `.env`.
- Настройки: `CLICKHOUSE_ENABLED=true`, `CLICKHOUSE_HOST=clickhouse`,
  `CLICKHOUSE_PORT=8123`, `CLICKHOUSE_USER=default`, `CLICKHOUSE_PASSWORD`,
  `CLICKHOUSE_DATABASE=aggregator`.
- Зависимость: `clickhouse-connect[async]==1.9.0`.
- Тесты, требующие ClickHouse, помечены `@pytest.mark.clickhouse` и
  пропускаются без `TEST_CLICKHOUSE_URL`; фикстура отказывается работать с базой,
  имя которой не оканчивается на `_test`, и пересоздаёт таблицу.

## Вне объёма

Перевод дашборда/страницы данных/профилей на ClickHouse; отключение записи в
`raw_events`; ранжирование по релевантности; морфология русского языка;
репликация ClickHouse.
