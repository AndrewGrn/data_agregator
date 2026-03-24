from __future__ import annotations

import asyncio
import datetime as dt
import re
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession


def normalize_telegram_identifier(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    lower = raw.lower()
    match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)", lower)
    if match:
        return f"@{match.group(1)}"

    if raw.startswith("@"):
        return f"@{raw[1:].strip().lower()}"

    if raw.lstrip("-").isdigit():
        return raw

    return raw


def parse_bulk_targets_input(value: str) -> list[str]:
    parts = re.split(r"[\n,;]+", value or "")
    out: list[str] = []
    for part in parts:
        item = part.strip()
        if not item:
            continue
        if item not in out:
            out.append(item)
    return out


def compute_account_load_score(account, queued_jobs: int, now: dt.datetime) -> float:
    hourly_limit = max(int(getattr(account, "hourly_limit", 1) or 1), 1)
    hour_count = int(getattr(account, "hour_window_count", 0) or 0)
    health = float(getattr(account, "health_score", 100.0) or 0.0)
    cooldown_until = getattr(account, "cooldown_until", None)
    in_cooldown = bool(cooldown_until and cooldown_until > now)

    utilization = min(hour_count / hourly_limit, 2.0)
    health_penalty = max(0.0, (100.0 - health) / 100.0)
    cooldown_penalty = 10.0 if in_cooldown else 0.0
    queue_penalty = min(float(queued_jobs), 20.0) / 4.0
    return utilization * 5.0 + health_penalty * 4.0 + cooldown_penalty + queue_penalty


def account_parallel_limits(
    account,
    default_parallel_jobs: int,
    default_backfill_parallel_jobs: int,
) -> tuple[int, int]:
    creds = dict(getattr(account, "credentials", {}) or {})
    try:
        parallel_jobs = int(creds.get("parallel_jobs", default_parallel_jobs))
    except Exception:
        parallel_jobs = int(default_parallel_jobs)
    try:
        backfill_parallel_jobs = int(creds.get("backfill_parallel_jobs", default_backfill_parallel_jobs))
    except Exception:
        backfill_parallel_jobs = int(default_backfill_parallel_jobs)

    return max(parallel_jobs, 1), max(backfill_parallel_jobs, 1)


def find_dialog_match(entry: str, dialogs: list[dict]) -> dict | None:
    needle = normalize_telegram_identifier(entry)
    if not needle:
        return None

    if needle.startswith("@"):
        for dialog in dialogs:
            if str(dialog.get("identifier") or "").lower() == needle.lower():
                return dialog

    if needle.lstrip("-").isdigit():
        for dialog in dialogs:
            if str(dialog.get("dialog_id")) == needle:
                return dialog

    lowered = needle.lower()
    exact_title = [d for d in dialogs if str(d.get("title") or "").strip().lower() == lowered]
    if exact_title:
        return exact_title[0]

    partial = [d for d in dialogs if lowered in str(d.get("title") or "").strip().lower()]
    if partial:
        return partial[0]

    return None


async def fetch_account_dialogs(credentials: dict, limit: int = 500) -> list[dict[str, Any]]:
    creds = credentials or {}
    api_id = creds.get("api_id")
    api_hash = creds.get("api_hash")
    session_string = creds.get("session_string")
    if not (api_id and api_hash and session_string):
        return []

    client = TelegramClient(StringSession(str(session_string)), int(api_id), str(api_hash))
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return []

    dialogs: list[dict[str, Any]] = []
    async for dialog in client.iter_dialogs(limit=limit):
        entity = dialog.entity
        username = getattr(entity, "username", None)
        identifier = f"@{str(username).lower()}" if username else str(dialog.id)
        if getattr(entity, "broadcast", False):
            kind = "канал"
        elif getattr(entity, "megagroup", False):
            kind = "група"
        else:
            kind = "чат"

        dialogs.append(
            {
                "dialog_id": int(dialog.id),
                "title": dialog.name or identifier,
                "identifier": identifier,
                "username": str(username).lower() if username else None,
                "kind": kind,
            }
        )

    await client.disconnect()
    dialogs.sort(key=lambda d: (str(d.get("title") or "")).lower())
    return dialogs


def refresh_account_dialogs_sync(credentials: dict, limit: int = 500) -> list[dict[str, Any]]:
    return asyncio.run(fetch_account_dialogs(credentials, limit=limit))
