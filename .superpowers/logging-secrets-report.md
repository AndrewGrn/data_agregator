# Logging + secrets prep pass — report

Branch: `chore/logging-and-secrets`. Scope: Task 1 (logging), Task 2 (placeholder-secret
guard), Task 3 (Postgres loopback binding).

## Task 1 — print() → logging

### Central wiring

- `app/logging_config.py` (new): `configure_logging(level: str = "INFO") -> None`, a thin
  wrapper over `logging.basicConfig` with format `"%(asctime)s %(levelname)s %(name)s: %(message)s"`.
  `basicConfig` only installs handlers once, so calling it from multiple import paths is harmless.
- `app/config.py`: added `log_level: str = "INFO"` to `Settings` (env var `LOG_LEVEL`).
- `app/__init__.py` (was empty) now does, at package-import time:
  ```python
  from app.config import get_settings, validate_settings
  from app.logging_config import configure_logging

  _settings = get_settings()
  configure_logging(_settings.log_level)
  validate_settings(_settings)
  ```
  Because importing any `app.*` submodule first executes `app/__init__.py`, this guarantees
  logging is configured (and secrets validated, see Task 2) for every entry point without
  touching `app/main.py` or `app/cli.py` individually: the FastAPI app, `python -m app.cli`,
  and the worker/listener processes started through the CLI all go through this exactly once.
- Each converted module now does `logger = logging.getLogger(__name__)`, i.e. logger names
  `app.services.worker` / `app.services.telegram_listener`. The old manual `[worker]` /
  `[telegram-listener]` message prefixes were dropped from the message text since `%(name)s`
  in the format string now carries that information (and can be used for per-component
  filtering via `logging.getLogger("app.services.worker").setLevel(...)`).

### Every converted call

`app/services/worker.py`:
| Line (orig) | Message | Level | Why |
|---|---|---|---|
| 302 `[worker] start job#...` | start job#… | `info` | routine progress |
| 379 `[worker] done job#... status=succeeded` | done job#… succeeded | `info` | routine progress |
| 389 `[worker] job#{id} disappeared after error` | job disappeared after error | `error` (+`exc_info=True`) | job vanished mid-processing is a genuine anomaly, not just a retry; traceback of the error that preceded it is kept |
| 398 `[worker] done job#... status=... error=...` | done job#… status=…/error=… | `logger.exception(...)` | always emitted from inside the `except Exception as exc:` block — a real failure, traceback worth keeping |
| 433 `[worker] starting pool ...` | starting pool concurrency=…/queues=… | `info` | routine startup |
| 534 `[worker] starting JetStream consumer ...` | starting JetStream consumer … | `info` | routine startup |

`app/services/telegram_listener.py`:
| Line (orig) | Message | Level | Why |
|---|---|---|---|
| 208 `not authorized; skipping` | account #… is not authorized; skipping | `warning` | explicitly called out as recoverable in the task spec |
| 217 `listening for N route keys` | account #… (…) listening for N route keys | `info` | routine progress |
| 234 `listener stopped` | account #… listener stopped | `info` | routine progress |
| 335 `message handling error` | account #… message handling error: {exc} | `warning` (+`exc_info=True`) | explicitly called out as recoverable; kept the traceback via `exc_info=True` rather than `logger.exception` so the level stays WARNING |
| 361 `disconnected: {err}. restarting` | account #… disconnected: {err}. restarting | `warning` (+`exc_info=err`) | explicitly called out as recoverable |
| 366 `started, refresh interval Ns` | started, refresh interval Ns | `info` | routine startup |

Total: 6 + 6 = 12, matching the count in the task. `grep -rn "print(" app` now returns nothing.

No new dependency was added; `requirements.txt` is untouched; stdlib `logging` only.

### Demonstration (verbatim)

```
$ python3 - <<'EOF'
from app.services.telegram_listener import logger
logger.info("account #42 (demo) listening for 3 route keys.")
logger.warning("account #42 is not authorized; skipping.")
try:
    raise ValueError("boom")
except ValueError:
    logger.warning("account #42 message handling error: boom", exc_info=True)
EOF
2026-09-21 22:53:02,196 WARNING app.config: Using placeholder secret(s) DEFAULT_ADMIN_PASSWORD, S3_ACCESS_KEY, S3_SECRET_KEY — fine for development, but this must be changed before deploying to production (see scripts/generate_secrets.py).
2026-09-21 22:53:02,315 INFO telethon.crypto.aes: libssl detected, it will be used for encryption
2026-09-21 22:53:02,519 INFO app.services.telegram_listener: account #42 (demo) listening for 3 route keys.
2026-09-21 22:53:02,519 WARNING app.services.telegram_listener: account #42 is not authorized; skipping.
2026-09-21 22:53:02,519 WARNING app.services.telegram_listener: account #42 message handling error: boom
Traceback (most recent call last):
  File "<stdin>", line 5, in <module>
ValueError: boom
```
(The first two lines are the development-mode secret warning and telethon's own logger firing
on import — both expected side effects of loading the app package, not part of the demo call.)

## Task 2 — refuse to start with placeholder secrets

- `Settings.environment: str = "development"` (env var `ENVIRONMENT`), plus `log_level` above.
- `app/config.py`: `_check_placeholder_secrets(settings)` compares `secret_key`,
  `default_admin_password`, and (only when `s3_enabled` is true) `s3_access_key`/`s3_secret_key`
  against the exact shipped-default strings. If any match:
  - `environment == "production"` → raises `RuntimeError` naming every offending variable and
    what to set it to, aborting startup (uncaught exception crashes the process).
  - otherwise → `logger.warning(...)` once, naming the same variables; behavior is otherwise
    unchanged, so existing dev workflow/tests are unaffected.
- `validate_settings(settings=None)` is the public entry point (`app/config.py`), called once
  from `app/__init__.py` as described in Task 1 — this is what makes the check run for the
  API, the CLI, and every worker/listener process, since they all import `app.*` before doing
  anything else. No entry point needed a bespoke call.
- S3 keys are only checked when `s3_enabled` is true, since with it false they're inert
  (media-in-S3 isn't built yet); `secret_key`/`default_admin_password` are always checked since
  they're always in effect.
- `scripts/generate_secrets.py` (new, short, stdlib `secrets` only): prints ready-to-paste
  `KEY=value` lines for all four secrets.
- `.env.example` updated: added `ENVIRONMENT=development` and `LOG_LEVEL=INFO`; changed
  `SECRET_KEY` from `change-me-please` (which silently would NOT have tripped the check) to
  `change-me` (the actual default/placeholder, so an unedited copy fails loudly in production);
  added comments pointing at `scripts/generate_secrets.py` next to `SECRET_KEY`,
  `DEFAULT_ADMIN_PASSWORD`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

### Demonstration (verbatim)

**Production + placeholder `SECRET_KEY` → aborts:**
```
$ ENVIRONMENT=production SECRET_KEY=change-me python -c "from app.config import get_settings"
Traceback (most recent call last):
  ...
  File "/Users/andriihrenchyshen/data_agregator/app/__init__.py", line 8, in <module>
    validate_settings(_settings)
  File "/Users/andriihrenchyshen/data_agregator/app/config.py", line 119, in validate_settings
    _check_placeholder_secrets(settings)
  File "/Users/andriihrenchyshen/data_agregator/app/config.py", line 95, in _check_placeholder_secrets
    raise RuntimeError(
RuntimeError: Refusing to start in production with placeholder secret(s) still set: SECRET_KEY, DEFAULT_ADMIN_PASSWORD, S3_ACCESS_KEY, S3_SECRET_KEY.
  - SECRET_KEY: set it to a long random value (see scripts/generate_secrets.py)
  - DEFAULT_ADMIN_PASSWORD: set it to a strong password (see scripts/generate_secrets.py)
  - S3_ACCESS_KEY: set it to your real MinIO/S3 access key
  - S3_SECRET_KEY: set it to your real MinIO/S3 secret key (see scripts/generate_secrets.py)
```
(`DEFAULT_ADMIN_PASSWORD`/S3 keys are also flagged here because they still had their placeholder
values from the real `.env` loaded in this shell — expected, since the check reports every
offender at once.)

**Production + real generated secrets → starts:**
```
$ REAL_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
$ REAL_PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
$ ENVIRONMENT=production SECRET_KEY="$REAL_KEY" DEFAULT_ADMIN_PASSWORD="$REAL_PW" S3_ENABLED=false \
    python -c "from app.config import get_settings; print('startup ok, environment=' + get_settings().environment)"
startup ok, environment=production
```

### Runnable check

`tests/test_config_secrets.py` (pytest, no DB needed): asserts the development case only warns,
the production case with a placeholder raises `RuntimeError` naming the variable, and the
production case with real values (and `s3_enabled=False`) does not raise. All 3 pass.

One gotcha found while writing that test: `Settings()` still loads the real project `.env` file
by default (pydantic-settings' `env_file=".env"`), so a test that only overrides
`secret_key`/`default_admin_password` can still pick up `s3_enabled=True` from the real `.env`
and trip on the (still-placeholder) S3 keys. Fixed by passing `s3_enabled=False` explicitly in
that test case — not a product bug, just a test-isolation note for whoever writes the next test
against `Settings`.

## Task 3 — Postgres loopback binding

`docker-compose.yml`: `db` service port changed from `"5432:5432"` to `"127.0.0.1:5432:5432"`.
No other compose changes made (as scoped).

Verified: `docker compose config --quiet` passes; `docker compose config` shows
`host_ip: 127.0.0.1` for the db service's port mapping. Recreated only the `db` container
(`docker compose up -d db`) to apply the new binding — data volume (`pgdata`) untouched, no SQL
run against it. `docker compose ps db` now shows `127.0.0.1:5432->5432/tcp` (was
`0.0.0.0:5432->5432/tcp`). Confirmed still reachable from the host and dev data intact:

```
$ PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d aggregator -c \
    "select (select count(*) from targets) as targets, (select count(*) from parser_accounts) as accounts, (select count(*) from parse_jobs) as jobs;"
 targets | accounts | jobs
---------+----------+------
      15 |        4 | 8943
(1 row)
```

## Full verification run

```
$ TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/aggregator_test" pytest tests/ -q
...
FAILED tests/test_darknet_adapter.py::test_darknet_adapter_registry_and_suggestion
FAILED tests/test_darknet_incremental.py::test_select_threads_for_parse_without_reparse
FAILED tests/test_scheduler.py::test_scheduler_creates_darknet_job_once - nats...
3 failed, 52 passed, 1 warning in 13.74s
```
Delta against the three pre-existing failures named in the task (adapter-registry naming
mismatch, thread-reparse filter bug, NATS unreachable): zero. The extra passing test is the new
`tests/test_config_secrets.py` (3 tests).

```
$ python -c "from app.main import app; print('ok')"
...
ok
$ python -m app.cli --help
...
Usage: python -m app.cli [OPTIONS] COMMAND [ARGS]...
...
```
Both succeed (the dev-mode placeholder warning appears first, as expected, since the real `.env`
still has `default_admin_password`/S3 placeholders and `environment` defaults to development).

`tests/conftest.py`'s `_test`-suffix guard was not touched.

## Things I was unsure about / judgment calls

- The task's design notes only explicitly require aborting on placeholder `secret_key` or
  `default_admin_password` in production. I also gated `s3_access_key`/`s3_secret_key` behind
  the same abort, but only when `s3_enabled` is true, since the task's intro lists them among
  the placeholder secrets that ship today and the upcoming S3-media feature will make them live.
  If that's more than wanted, it's a one-line removal (drop the two `if settings.s3_enabled...`
  blocks in `_check_placeholder_secrets`).
- Chose `RuntimeError` (uncaught → non-zero exit with traceback) rather than `sys.exit(...)` for
  the production abort — "fail loudly" is satisfied either way; `RuntimeError` keeps the message
  in the traceback verbatim and doesn't require importing `sys` into `config.py`.
- Central wiring lives in `app/__init__.py` rather than being called explicitly from
  `app/main.py`'s `startup()` hook and the top of `app/cli.py`, because every one of those files
  (and worker.py/telegram_listener.py) already does `from app.config import get_settings` at
  module load time, which necessarily runs `app/__init__.py` first. This was the smallest change
  that still guarantees "every entry point" rather than three separate call sites to keep in
  sync.
- `logger.warning(..., exc_info=True)` (not `logger.exception`) is used for the two
  telegram_listener cases the task explicitly labeled "warning" but that also have a live
  exception, so the traceback is kept without bumping the level to ERROR.
