from __future__ import annotations

import asyncio
import json
from typing import Any

from nats.aio.client import Client as NATS
from nats.errors import TimeoutError
from nats.js import api as js_api
from nats.js.errors import NotFoundError, ServerError

from app.config import get_settings

settings = get_settings()


def nats_servers_list() -> list[str]:
    raw = str(settings.nats_servers or "").strip()
    if not raw:
        return ["nats://nats:4222"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def subject_for_queue(queue: str) -> str:
    normalized = str(queue or "").strip().lower()
    safe = normalized.replace(".", "_")
    return f"{settings.nats_subject_prefix}.{safe}"


def queue_from_subject(subject: str) -> str:
    prefix = f"{settings.nats_subject_prefix}."
    raw = str(subject or "")
    if raw.startswith(prefix):
        return raw[len(prefix) :]
    return raw


async def _connect() -> tuple[NATS, Any]:
    nc = NATS()
    await nc.connect(servers=nats_servers_list(), connect_timeout=2, max_reconnect_attempts=10, reconnect_time_wait=1)
    js = nc.jetstream()
    return nc, js


async def ensure_stream(js) -> None:
    stream_name = str(settings.nats_stream_name or "AGG_JOBS").strip() or "AGG_JOBS"
    subject_pattern = f"{settings.nats_subject_prefix}.*"
    try:
        stream_info = await js.stream_info(stream_name)
        current = stream_info.config
        needs_update = False

        if list(current.subjects or []) != [subject_pattern]:
            current.subjects = [subject_pattern]
            needs_update = True

        if needs_update:
            try:
                await js.update_stream(config=current)
            except ServerError:
                # Some stream options (e.g., retention mode) are immutable on existing streams.
                # Keep current stream config and continue so workers stay operational.
                pass
        return
    except NotFoundError:
        pass
    await js.add_stream(
        name=stream_name,
        subjects=[subject_pattern],
        retention=js_api.RetentionPolicy.WORK_QUEUE,
        max_msgs=-1,
        max_bytes=-1,
    )


async def publish_jobs_async(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not settings.nats_enabled:
        return [{"ok": False, "error": "nats disabled"} for _ in items]
    if not items:
        return []

    results: list[dict[str, Any]] = []
    nc, js = await _connect()
    try:
        await ensure_stream(js)
        for item in items:
            queue = str(item.get("queue") or "").strip().lower()
            if not queue:
                results.append({"ok": False, "error": "missing queue"})
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            try:
                await js.publish(
                    subject_for_queue(queue),
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=max(int(settings.nats_publish_timeout_seconds), 1),
                )
                results.append({"ok": True, "error": None})
            except Exception as exc:
                results.append({"ok": False, "error": str(exc)})
    finally:
        await nc.close()
    return results


def publish_jobs_sync(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not settings.nats_enabled or not items:
        return [{"ok": False, "error": "nats disabled"} for _ in items]
    return asyncio.run(publish_jobs_async(items))


async def pull_subscribe_queue(js, queue: str, durable: str):
    await ensure_stream(js)
    stream_name = str(settings.nats_stream_name or "AGG_JOBS").strip() or "AGG_JOBS"
    subject = subject_for_queue(queue)
    config = js_api.ConsumerConfig(
        durable_name=durable,
        ack_policy=js_api.AckPolicy.EXPLICIT,
        filter_subject=subject,
        ack_wait=max(int(settings.nats_consumer_ack_wait_seconds), 5),
        max_deliver=max(int(settings.nats_consumer_max_deliver), 1),
    )
    try:
        await js.consumer_info(stream_name, durable)
    except NotFoundError:
        await js.add_consumer(stream_name, config=config)
    return await js.pull_subscribe(subject=subject, durable=durable, stream=stream_name)


async def fetch_messages(subscription, batch: int, timeout_seconds: int):
    try:
        return await subscription.fetch(batch=batch, timeout=timeout_seconds)
    except TimeoutError:
        return []
