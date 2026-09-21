from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ServiceState, Target
from app.plugins.whatsapp import map_wa_event
from app.services.event_sink import persist_events
from app.services.whatsapp_bus import consume_events, consume_statuses

logger = logging.getLogger(__name__)


async def _handle(message: dict) -> None:
    chat_id = str(message.get("chat_id") or "")
    account_id = message.get("account_id")
    if not chat_id:
        logger.warning("dropping wa event with no chat_id")
        return

    with SessionLocal() as session:
        target = session.execute(
            select(Target).where(
                Target.parser_type == "whatsapp",
                Target.identifier == chat_id,
            )
        ).scalar_one_or_none()
        if target is None:
            # Chat nobody tracks. Not an error — ack and move on, or the
            # stream would redeliver it forever.
            logger.info("dropping wa event for untracked chat %s", chat_id)
            return

        persist_events(
            session,
            target=target,
            parser_type="whatsapp",
            account_id=int(account_id) if account_id is not None else None,
            owner_user_id=target.owner_user_id,
            events=[map_wa_event(message)],
        )
        session.commit()


async def _handle_status(account_id: str, payload: dict) -> None:
    """Mirror one wa.status.<account_id> message into service_state."""
    with SessionLocal() as session:
        key = f"wa_session_{account_id}"
        state = session.get(ServiceState, key)
        value = {**payload, "updated_at": dt.datetime.now(dt.UTC).isoformat()}
        if state is None:
            session.add(ServiceState(key=key, value=value))
        else:
            state.value = value
        session.commit()


def run_whatsapp_listener() -> None:
    async def _main() -> None:
        await asyncio.gather(consume_events(_handle), consume_statuses(_handle_status))

    asyncio.run(_main())
