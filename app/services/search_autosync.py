from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RawEvent, ServiceState, Target
from app.services.search_index import search_index

STATE_KEY = "opensearch_autosync_v1"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _state_value(state: ServiceState | None) -> dict[str, Any]:
    if not state or not isinstance(state.value, dict):
        return {"mode": "desc", "cursor": 0, "indexed_total": 0, "processed_total": 0, "skipped_total": 0}
    value = dict(state.value or {})
    value.setdefault("mode", "desc")
    value.setdefault("cursor", 0)
    value.setdefault("indexed_total", 0)
    value.setdefault("processed_total", 0)
    value.setdefault("skipped_total", 0)
    return value


def _persist_state(session: Session, value: dict[str, Any]) -> None:
    state = session.get(ServiceState, STATE_KEY)
    if state is None:
        state = ServiceState(key=STATE_KEY, value=value)
        session.add(state)
    else:
        state.value = value
    state.updated_at = dt.datetime.now(dt.UTC)


def get_search_autosync_state(session: Session) -> dict[str, Any]:
    state = session.get(ServiceState, STATE_KEY)
    value = _state_value(state)
    return {
        "mode": str(value.get("mode") or "desc"),
        "cursor": _safe_int(value.get("cursor"), 0),
        "indexed_total": _safe_int(value.get("indexed_total"), 0),
        "processed_total": _safe_int(value.get("processed_total"), 0),
        "skipped_total": _safe_int(value.get("skipped_total"), 0),
        "last_run_at": value.get("last_run_at"),
        "last_error": value.get("last_error"),
    }


def _max_raw_event_id(session: Session) -> int:
    return _safe_int(session.scalar(select(func.max(RawEvent.id))), 0)


def _load_event_payload(event: RawEvent) -> dict[str, Any] | None:
    return event.payload if isinstance(event.payload, dict) and event.payload else None


def autosync_search_index_batch(session: Session, batch_size: int = 1000) -> dict[str, Any]:
    status = search_index.status()
    if not bool(status.get("enabled")):
        return {"ok": False, "reason": "disabled"}
    if not bool(status.get("package_installed")):
        return {"ok": False, "reason": "package_missing"}
    if not bool(status.get("reachable")):
        return {"ok": False, "reason": "unreachable", "error": status.get("error")}

    safe_batch_size = max(100, min(int(batch_size or 1000), 5000))
    value = _state_value(session.get(ServiceState, STATE_KEY))
    mode = str(value.get("mode") or "desc").strip().lower()
    if mode not in {"desc", "tail"}:
        mode = "desc"

    cursor = _safe_int(value.get("cursor"), 0)
    if mode == "desc" and cursor <= 0:
        cursor = _max_raw_event_id(session) + 1

    if mode == "desc":
        events = (
            session.execute(select(RawEvent).where(RawEvent.id < cursor).order_by(RawEvent.id.desc()).limit(safe_batch_size))
            .scalars()
            .all()
        )
    else:
        if cursor <= 0:
            cursor = _max_raw_event_id(session)
        events = (
            session.execute(select(RawEvent).where(RawEvent.id > cursor).order_by(RawEvent.id.asc()).limit(safe_batch_size))
            .scalars()
            .all()
        )

    target_cache: dict[int, Target | None] = {}
    indexed = 0
    skipped = 0

    for event in events:
        target_id = int(event.target_id)
        if target_id not in target_cache:
            target_cache[target_id] = session.get(Target, target_id)
        target = target_cache[target_id]
        if target is None:
            skipped += 1
            continue

        payload = _load_event_payload(event)
        ok = search_index.index_raw_event(event=event, payload=payload if isinstance(payload, dict) else {}, target=target)
        if ok:
            indexed += 1
        else:
            skipped += 1

    processed = len(events)

    if mode == "desc":
        if processed > 0:
            value["cursor"] = min(int(event.id) for event in events)
        else:
            value["mode"] = "tail"
            value["cursor"] = _max_raw_event_id(session)
    else:
        if processed > 0:
            value["cursor"] = max(int(event.id) for event in events)

    value["processed_total"] = _safe_int(value.get("processed_total"), 0) + int(processed)
    value["indexed_total"] = _safe_int(value.get("indexed_total"), 0) + int(indexed)
    value["skipped_total"] = _safe_int(value.get("skipped_total"), 0) + int(skipped)
    value["last_run_at"] = dt.datetime.now(dt.UTC).isoformat()
    value["last_error"] = None
    _persist_state(session, value)

    return {
        "ok": True,
        "mode": str(value.get("mode") or mode),
        "cursor": _safe_int(value.get("cursor"), 0),
        "processed": int(processed),
        "indexed": int(indexed),
        "skipped": int(skipped),
    }
