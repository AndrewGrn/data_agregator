from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.plugins.registry import plugin_registry
from app.routers.api import _is_admin
from app.services import clickhouse_store as store

router = APIRouter(prefix="/api/v2", tags=["events-v2"])

MAX_LIMIT = 500


def get_ch_client(request: Request):
    client = getattr(request.app.state, "ch_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="ClickHouse недоступний")
    return client


def _owner_scope(user) -> int | None:
    return None if _is_admin(user) else int(user.id)


class EventSearchRequest(BaseModel):
    """Same filters as the query string, but in a JSON body.

    Cyrillic words, wallet fragments and quoted phrases all have to be
    percent-encoded in a URL, which is where hand-written calls kept breaking.
    """

    q: str | None = None
    contains: str | None = None
    author_id: str | None = None
    nickname: str | None = None
    phone: str | None = None
    target_id: int | None = None
    parser_type: str | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None
    limit: int = Field(default=100, ge=1)
    cursor: str | None = None


async def _run_search(payload: EventSearchRequest, user, ch) -> dict:
    if payload.parser_type and not plugin_registry.is_known(payload.parser_type):
        raise HTTPException(status_code=400, detail="Некоректний parser_type")
    if payload.cursor:
        try:
            store.decode_cursor(payload.cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некоректний cursor")
    limit = min(int(payload.limit), MAX_LIMIT)
    filters = store.EventFilters(
        q=payload.q or None,
        contains=payload.contains or None,
        author_id=payload.author_id or None,
        nickname=payload.nickname or None,
        phone=payload.phone or None,
        target_id=payload.target_id,
        parser_type=payload.parser_type or None,
        owner_user_id=_owner_scope(user),
        since=payload.since,
        until=payload.until,
    )
    items, next_cursor = await store.search_events(ch, filters, limit=limit, cursor=payload.cursor)
    return {"items": items, "next_cursor": next_cursor, "limit": limit}


@router.post("/events/search")
async def search_events_v2_post(
    payload: EventSearchRequest,
    user=Depends(get_current_user),
    ch=Depends(get_ch_client),
):
    """Search events with a JSON body. Preferred over the query-string form."""
    return await _run_search(payload, user, ch)


@router.get("/events")
async def search_events_v2(
    q: str | None = Query(None, description="слова в тексті (усі мають бути присутні)"),
    contains: str | None = Query(None, description="підрядок у тексті без урахування регістру"),
    author_id: str | None = None,
    nickname: str | None = None,
    phone: str | None = None,
    target_id: int | None = None,
    parser_type: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(100, ge=1),
    cursor: str | None = None,
    user=Depends(get_current_user),
    ch=Depends(get_ch_client),
):
    # Kept as a convenience alias; the POST form is the documented one.
    return await _run_search(
        EventSearchRequest(
            q=q, contains=contains, author_id=author_id, nickname=nickname, phone=phone,
            target_id=target_id, parser_type=parser_type, since=since, until=until,
            limit=limit, cursor=cursor,
        ),
        user,
        ch,
    )


@router.get("/events/{parser_type}/{target_id}/{event_key}")
async def get_event_v2(
    parser_type: str, target_id: int, event_key: str,
    user=Depends(get_current_user), ch=Depends(get_ch_client),
):
    row = await store.get_event(ch, parser_type=parser_type, target_id=target_id, event_key=event_key,
                                owner_user_id=_owner_scope(user))
    if row is None:
        raise HTTPException(status_code=404, detail="Подію не знайдено")
    return row
