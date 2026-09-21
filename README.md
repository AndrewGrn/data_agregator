# Агрегатор Даних (MVP)

Платформа агрегації даних з відкритих джерел з модульними парсерами, планувальником і пулом воркерів.

## Реалізовано

- Web UI українською мовою з локальною авторизацією (без Keycloak)
- React frontend (Vite) для панелі, Telegram-модуля і сторінки даних
- Плагінна архітектура парсерів: `telegram`, `darknet`
- Scheduler, який створює jobs по активних цілях
- Worker pool з retry/backoff
- Telegram hybrid-режим: live listener + polling/backfill
- Профілі Telegram-користувачів і членство в групах (`telegram_users`, `telegram_memberships`, history)
- Зберігання raw payload в S3/MinIO (в Postgres тільки посилання + метадані)
- Повнотекстовий пошук через OpenSearch (індексація Telegram/Darknet повідомлень)
- Health-score і rate-limit для Telegram акаунтів
- Backfill planner по діапазону дат (чанки)
- Alembic-міграції та версіонування схеми

## Архітектура

- `app/plugins/*` — парсери
- `app/services/scheduler.py` — постановка jobs
- `app/services/worker.py` — виконання jobs
- `app/services/telegram_listener.py` — live-події Telegram (тільки по активних цілях)
- `app/services/object_store.py` — S3/MinIO raw storage
- `app/models.py` — ORM-моделі
- `alembic/*` — міграції
- `app/routers/ui.py` / `app/templates/*` — web UI
- `app/routers/api.py` — API
- `frontend/*` — React UI (Vite)

## Docker запуск (рекомендовано)

1. Переконайся, що є `.env`:

```bash
cp .env.example .env
```

2. Підніми сервіси:

```bash
docker compose up --build -d
```

3. Увімкни auto-sync коду:

```bash
docker compose watch
```

4. Відкрий UI:

- React UI: http://localhost:5173/login
- React UI через Traefik-домен: http://aggredata.localhost/login
- Legacy UI: http://localhost:8000/login
- логін/пароль за замовчуванням: `admin` / `admin123`
- Traefik dashboard: http://localhost:8081

Сервіси в compose:

- `api`
- `frontend`
- `scheduler`
- `worker`
- `telegram-listener`
- `db` (PostgreSQL)
- `minio` (S3 API + console `:9001`)
- `opensearch` (`:9200`)
- `torproxy`

## Міграції Alembic

- застосувати міграції:

```bash
python -m app.cli db-upgrade
```

- створити ревізію:

```bash
python -m app.cli db-revision --message "add new field" --autogenerate
```

## Telegram session string

```bash
python -m app.cli generate-telegram-session --api-id <API_ID> --api-hash <API_HASH> --phone <PHONE>
```

## Darknet browser auth helper (captcha-friendly)

Для форумів з капчею (наприклад XenForo) використовуй `browser_state`:

1. У UI `/darknet` створи helper-команду в блоці `Локальний helper авторизації`.
2. Запусти команду локально. Вона відкриє Playwright.
3. Увійди вручну (логін/пароль/капча) і закрий вікно Playwright.
4. Скрипт автоматично завантажить `storageState` (cookies + localStorage) в акаунт.

Локальний скрипт: `scripts/darknet_auth_helper.py`

## Backfill

У формі створення цілі:

- увімкни `Backfill`
- вкажи `from/to` у форматі ISO datetime
- задай `chunk_days`

Scheduler створить backfill-jobs по чанках часу.

## Telegram Hybrid (live + polling)

- `telegram-listener` слухає нові повідомлення в реальному часі.
- Listener зберігає події тільки для `targets`, які:
  - активні (`targets.is_active = true`)
  - прив'язані до акаунта (`target_account_links.is_active = true`)
- Події з інших чатів/каналів акаунта ігноруються.
- `worker + scheduler` залишаються для polling/backfill і добору пропусків.

## Rate-limit / health-score

Для Telegram акаунтів підтримуються:

- `hourly_limit`
- `hour_window_count`
- `health_score`
- `cooldown_until`
- `success_count` / `fail_count`

Після помилок health знижується і акаунт переходить у cooldown.

## API

- `GET /api/health`
- `GET /api/auth/me`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/dashboard`
- `GET /api/modules/telegram`
- `GET /api/targets`
- `POST /api/targets`
- `GET /api/accounts`
- `POST /api/accounts`
- `POST /api/links`
- `POST /api/scheduler/run`
- `POST /api/telegram/sync-memberships`
- `POST /api/modules/{parser_name}/targets/{target_id}/run-now`
- `POST /api/modules/{parser_name}/targets/{target_id}/start`
- `POST /api/modules/{parser_name}/targets/{target_id}/stop`
- `GET /api/telegram/targets-overview`
- `GET /api/telegram/targets/{target_id}/messages`
- `GET /api/telegram/targets/{target_id}/users`
- `GET /api/search/status`
- `GET /api/search/messages`
- `POST /api/search/reindex`
- `GET /api/jobs`
- `GET /api/events`
- `GET /api/events/{event_id}/payload`

## Тести

```bash
pytest -q
```
