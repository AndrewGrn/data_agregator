from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from nats.aio.client import Client as NATS
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import api as js_api
from nats.js.errors import NotFoundError

from app.config import get_settings
from app.services.execution_queue import nats_servers_list

logger = logging.getLogger(__name__)

settings = get_settings()

# Persisted stream: real chat events, replayed to the listener after a crash.
SUBJECT_EVENTS = "wa.events.*"
# Core NATS, no stream: worthless after the fact (backfill requests, QR/status).
SUBJECT_BACKFILL_PREFIX = "wa.backfill"
SUBJECT_STATUS = "wa.status.*"


async def _connect() -> NATS:
    nc = NATS()
    await nc.connect(servers=nats_servers_list(), connect_timeout=2, max_reconnect_attempts=10, reconnect_time_wait=1)
    return nc


async def ensure_stream() -> None:
    """Create the WA_EVENTS stream if it does not exist yet. Idempotent."""
    nc = await _connect()
    try:
        js = nc.jetstream()
        try:
            await js.stream_info(settings.wa_stream)
            return
        except NotFoundError:
            pass
        await js.add_stream(
            name=settings.wa_stream,
            subjects=[SUBJECT_EVENTS],
            retention=js_api.RetentionPolicy.LIMITS,
            storage=js_api.StorageType.FILE,
            max_age=7 * 24 * 3600,  # seconds
        )
    finally:
        await nc.close()


def request_backfill(account_id: int | None, chat_id: str, limit: int) -> None:
    """Ask the bridge to replay chat history. Fire-and-forget over core NATS."""
    if account_id is None:
        logger.warning("skipping backfill request for chat %s: no account", chat_id)
        return

    async def _publish() -> None:
        nc = await _connect()
        try:
            payload = json.dumps({"chat_id": str(chat_id), "limit": int(limit)}).encode("utf-8")
            await nc.publish(f"{SUBJECT_BACKFILL_PREFIX}.{int(account_id)}", payload)
            await nc.flush()
        finally:
            await nc.close()

    asyncio.run(_publish())


async def consume_events(handler: Callable[[dict], Awaitable[None]]) -> None:
    """Run the durable consumer forever, acking only after handler succeeds.

    Ack happens after the handler returns (i.e. after its Postgres commit),
    never before: if this process dies mid-handler, JetStream redelivers the
    message and persist_events' unique index turns that into a no-op.
    """
    await ensure_stream()
    nc = await _connect()
    try:
        js = nc.jetstream()
        config = js_api.ConsumerConfig(
            durable_name=settings.wa_durable,
            ack_policy=js_api.AckPolicy.EXPLICIT,
            filter_subject=SUBJECT_EVENTS,
        )
        try:
            await js.consumer_info(settings.wa_stream, settings.wa_durable)
        except NotFoundError:
            await js.add_consumer(settings.wa_stream, config=config)
        subscription = await js.pull_subscribe(
            subject=SUBJECT_EVENTS, durable=settings.wa_durable, stream=settings.wa_stream
        )

        while True:
            try:
                messages = await subscription.fetch(batch=10, timeout=5)
            except NatsTimeoutError:
                continue

            for message in messages:
                try:
                    await handler(json.loads(message.data.decode("utf-8")))
                except Exception:
                    logger.exception("wa-bus handler failed, message will redeliver")
                    continue
                await message.ack()
    finally:
        await nc.close()


async def consume_statuses(handler: Callable[[str, dict], Awaitable[None]]) -> None:
    """Mirror bridge session state (QR codes, ready/disconnected) forever.

    Core NATS, no JetStream: a status message missed while this process is
    down is just a stale service_state row until the bridge's next event,
    not lost data worth replaying.
    """
    nc = await _connect()
    try:
        subscription = await nc.subscribe(SUBJECT_STATUS)
        async for message in subscription.messages:
            account_id = message.subject.rsplit(".", 1)[-1]
            try:
                await handler(account_id, json.loads(message.data.decode("utf-8")))
            except Exception:
                logger.exception("wa-status handler failed for account %s", account_id)
    finally:
        await nc.close()
