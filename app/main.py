from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routers import api, auth, events_v2, ui
from app.services import clickhouse_store
from app.services.bootstrap import ensure_default_admin, run_migrations
from app.db import session_scope

settings = get_settings()

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=False, same_site="lax")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path("app/static")
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(ui.router)
app.include_router(api.router)
app.include_router(events_v2.router)


@app.on_event("startup")
def startup() -> None:
    run_migrations()
    with session_scope() as session:
        ensure_default_admin(session)


@app.on_event("startup")
async def startup_clickhouse() -> None:
    app.state.ch_client = None
    if not settings.clickhouse_enabled:
        return
    try:
        client = await clickhouse_store.get_async_client()
        await clickhouse_store.ensure_schema(client)
        app.state.ch_client = client
    except Exception as exc:  # noqa: BLE001 - API must still serve everything else
        import logging
        logging.getLogger(__name__).error("clickhouse unavailable at startup: %s", exc)


@app.on_event("shutdown")
async def shutdown_clickhouse() -> None:
    client = getattr(app.state, "ch_client", None)
    if client is not None:
        await client.close()
