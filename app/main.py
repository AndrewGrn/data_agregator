from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routers import api, auth, ui
from app.services.bootstrap import ensure_default_admin, run_migrations
from app.db import session_scope

settings = get_settings()

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=False, same_site="lax")

static_dir = Path("app/static")
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(ui.router)
app.include_router(api.router)


@app.on_event("startup")
def startup() -> None:
    run_migrations()
    with session_scope() as session:
        ensure_default_admin(session)
