from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.deps import get_current_user
from app.db import get_db
from app.models import ParseJob, ParserAccount, ParserType, RawEvent, Target, TargetAccountLink
from app.services.scheduler import schedule_once, sync_telegram_memberships

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    targets = db.execute(select(Target)).scalars().all()
    accounts = db.execute(select(ParserAccount)).scalars().all()
    jobs = db.execute(select(ParseJob).order_by(desc(ParseJob.created_at)).limit(20)).scalars().all()
    events_count = db.query(RawEvent).count()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "targets": targets,
            "accounts": accounts,
            "jobs": jobs,
            "events_count": events_count,
        },
    )


@router.get("/targets", response_class=HTMLResponse)
def targets_page(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    targets = db.execute(select(Target).order_by(desc(Target.created_at))).scalars().all()
    accounts = db.execute(select(ParserAccount).order_by(ParserAccount.label)).scalars().all()
    links = db.execute(select(TargetAccountLink)).scalars().all()
    return templates.TemplateResponse(
        "targets.html",
        {"request": request, "user": user, "targets": targets, "accounts": accounts, "links": links, "parser_types": ParserType},
    )


@router.post("/targets")
def create_target(
    request: Request,
    parser_type: ParserType = Form(...),
    name: str = Form(...),
    identifier: str = Form(...),
    limit: int = Form(200),
    backfill_enabled: bool = Form(False),
    backfill_from: str = Form(""),
    backfill_to: str = Form(""),
    backfill_chunk_days: int = Form(7),
    backfill_limit: int = Form(5000),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    config = {"limit": int(limit), "backfill_limit": int(backfill_limit)}
    config["backfill"] = {
        "enabled": bool(backfill_enabled),
        "from": backfill_from.strip() or None,
        "to": backfill_to.strip() or None,
        "chunk_days": max(int(backfill_chunk_days), 1),
    }
    target = Target(parser_type=parser_type, name=name.strip(), identifier=identifier.strip(), config=config)
    db.add(target)
    db.commit()
    return RedirectResponse(url="/targets", status_code=302)


@router.post("/accounts")
def create_account(
    request: Request,
    parser_type: ParserType = Form(...),
    label: str = Form(...),
    api_id: str = Form(""),
    api_hash: str = Form(""),
    session_string: str = Form(""),
    hourly_limit: int = Form(120),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    credentials = {}
    if parser_type == ParserType.telegram:
        credentials = {
            "api_id": api_id.strip(),
            "api_hash": api_hash.strip(),
            "session_string": session_string.strip(),
        }

    account = ParserAccount(
        parser_type=parser_type,
        label=label.strip(),
        credentials=credentials,
        hourly_limit=max(int(hourly_limit), 1),
    )
    db.add(account)
    db.commit()
    return RedirectResponse(url="/targets", status_code=302)


@router.post("/links")
def create_link(
    request: Request,
    target_id: int = Form(...),
    account_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    existing = db.execute(
        select(TargetAccountLink).where(TargetAccountLink.target_id == target_id, TargetAccountLink.account_id == account_id)
    ).scalar_one_or_none()
    if not existing:
        db.add(TargetAccountLink(target_id=target_id, account_id=account_id, is_active=True, auto_detected=False))
    else:
        existing.is_active = True
    db.commit()
    return RedirectResponse(url="/targets", status_code=302)


@router.post("/scheduler/run")
def run_scheduler_ui(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    schedule_once(db)
    db.commit()
    return RedirectResponse(url="/", status_code=302)


@router.post("/telegram/sync-memberships")
def sync_telegram_ui(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    sync_telegram_memberships(db)
    db.commit()
    return RedirectResponse(url="/targets", status_code=302)
