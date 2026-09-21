from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Target
from app.plugins.whatsapp import map_wa_event
from app.services.event_sink import persist_events
from app.services.whatsapp_bus import consume_events

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


def run_whatsapp_listener() -> None:
    asyncio.run(consume_events(_handle))
