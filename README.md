# Агрегатор Даних (MVP)

Платформа агрегації даних з відкритих джерел з модульними парсерами, планувальником і пулом воркерів.

## Реалізовано

- Web UI українською мовою з локальною авторизацією (без Keycloak)
- Плагінна архітектура парсерів: `telegram`, `darknet`
- Scheduler, який створює jobs по активних цілях
- Worker pool з retry/backoff
- Зберігання raw payload в S3/MinIO (в Postgres тільки посилання + метадані)
- Health-score і rate-limit для Telegram акаунтів
- Backfill planner по діапазону дат (чанки)
- Alembic-міграції та версіонування схеми

## Архітектура

- `app/plugins/*` — парсери
- `app/services/scheduler.py` — постановка jobs
- `app/services/worker.py` — виконання jobs
- `app/services/object_store.py` — S3/MinIO raw storage
- `app/models.py` — ORM-моделі
- `alembic/*` — міграції
- `app/routers/ui.py` / `app/templates/*` — web UI
- `app/routers/api.py` — API

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

- http://localhost:8000/login
- логін/пароль за замовчуванням: `admin` / `admin123`

Сервіси в compose:

- `api`
- `scheduler`
- `worker`
- `db` (PostgreSQL)
- `minio` (S3 API + console `:9001`)
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

## Backfill

У формі створення цілі:

- увімкни `Backfill`
- вкажи `from/to` у форматі ISO datetime
- задай `chunk_days`

Scheduler створить backfill-jobs по чанках часу.

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
- `GET /api/targets`
- `POST /api/targets`
- `GET /api/accounts`
- `POST /api/accounts`
- `POST /api/links`
- `POST /api/scheduler/run`
- `POST /api/telegram/sync-memberships`
- `GET /api/jobs`
- `GET /api/events`
- `GET /api/events/{event_id}/payload`

## Тести

```bash
pytest -q
```

