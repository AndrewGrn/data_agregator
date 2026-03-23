from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.deps import get_current_user
from app.db import get_db
from app.models import ParseJob, ParserAccount, RawEvent, Target, TargetAccountLink
from app.schemas import CreateAccountRequest, CreateTargetRequest, LinkAccountRequest
from app.services.object_store import object_store
from app.services.scheduler import schedule_once, sync_telegram_memberships

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/targets")
def list_targets(db: Session = Depends(get_db), user=Depends(get_current_user)):
    targets = db.execute(select(Target).order_by(desc(Target.created_at))).scalars().all()
    return [
        {
            "id": item.id,
            "parser_type": item.parser_type.value,
            "name": item.name,
            "identifier": item.identifier,
            "config": item.config,
            "onboarding_status": item.onboarding_status.value,
            "is_active": item.is_active,
        }
        for item in targets
    ]


@router.post("/targets")
def create_target(payload: CreateTargetRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = Target(
        parser_type=payload.parser_type,
        name=payload.name.strip(),
        identifier=payload.identifier.strip(),
        config=payload.config,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return {"id": target.id}


@router.post("/accounts")
def create_account(payload: CreateAccountRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    account = ParserAccount(
        parser_type=payload.parser_type,
        label=payload.label.strip(),
        credentials=payload.credentials,
        hourly_limit=max(payload.hourly_limit, 1),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"id": account.id}


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db), user=Depends(get_current_user)):
    accounts = db.execute(select(ParserAccount).order_by(ParserAccount.created_at.desc())).scalars().all()
    return [
        {
            "id": a.id,
            "parser_type": a.parser_type.value,
            "label": a.label,
            "is_active": a.is_active,
            "health_score": a.health_score,
            "hourly_limit": a.hourly_limit,
            "hour_window_count": a.hour_window_count,
            "cooldown_until": a.cooldown_until.isoformat() if a.cooldown_until else None,
            "success_count": a.success_count,
            "fail_count": a.fail_count,
            "last_error": a.last_error,
            "last_success_at": a.last_success_at.isoformat() if a.last_success_at else None,
        }
        for a in accounts
    ]


@router.post("/links")
def create_link(payload: LinkAccountRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    target = db.get(Target, payload.target_id)
    account = db.get(ParserAccount, payload.account_id)
    if not target or not account:
        raise HTTPException(status_code=404, detail="target/account not found")
    if target.parser_type != account.parser_type:
        raise HTTPException(status_code=400, detail="parser types mismatch")

    existing = db.execute(
        select(TargetAccountLink).where(
            TargetAccountLink.target_id == payload.target_id,
            TargetAccountLink.account_id == payload.account_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.is_active = True
    else:
        db.add(TargetAccountLink(target_id=payload.target_id, account_id=payload.account_id, is_active=True))
    db.commit()
    return {"ok": True}


@router.post("/scheduler/run")
def run_scheduler(db: Session = Depends(get_db), user=Depends(get_current_user)):
    result = schedule_once(db)
    db.commit()
    return result


@router.post("/telegram/sync-memberships")
def sync_memberships(db: Session = Depends(get_db), user=Depends(get_current_user)):
    result = sync_telegram_memberships(db)
    db.commit()
    return result


@router.get("/jobs")
def list_jobs(limit: int = 100, db: Session = Depends(get_db), user=Depends(get_current_user)):
    jobs = db.execute(select(ParseJob).order_by(desc(ParseJob.created_at)).limit(min(limit, 200))).scalars().all()
    return [
        {
            "id": j.id,
            "parser_type": j.parser_type.value,
            "target_id": j.target_id,
            "account_id": j.account_id,
            "status": j.status.value,
            "job_key": j.job_key,
            "attempt": j.attempt,
            "error": j.last_error,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }
        for j in jobs
    ]


@router.get("/events")
def list_events(limit: int = 100, db: Session = Depends(get_db), user=Depends(get_current_user)):
    events = db.execute(select(RawEvent).order_by(desc(RawEvent.created_at)).limit(min(limit, 200))).scalars().all()
    return [
        {
            "id": e.id,
            "parser_type": e.parser_type.value,
            "target_id": e.target_id,
            "account_id": e.account_id,
            "external_id": e.external_id,
            "storage_type": e.storage_type,
            "payload_ref": e.payload_ref,
            "payload_size": e.payload_size,
            "payload_sha256": e.payload_sha256,
            "payload_preview": e.payload_preview,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


@router.get("/events/{event_id}/payload")
def get_event_payload(event_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    event = db.get(RawEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    payload = object_store.get_json(event)
    if payload is None:
        raise HTTPException(status_code=404, detail="payload not available")
    return {"id": event.id, "payload": payload}
