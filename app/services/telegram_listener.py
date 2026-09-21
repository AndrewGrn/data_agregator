from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.models import ParserAccount, ParserType, Target, TargetAccountLink
from app.plugins.base import ParsedEvent
from app.plugins.telegram import normalize_telegram_payload
from app.services.event_sink import persist_events
from app.services.telegram_offsets import update_offset_from_message
from app.services.telegram_profiles import upsert_telegram_profile_from_event
from app.services.telegram_accounts import normalize_telegram_identifier


def _coerce_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _signed_channel_id(channel_id: int) -> int:
    # Telegram channel entities commonly use -100-prefixed signed chat ids.
    return -1000000000000 + int(channel_id)


def build_route_keys_for_identifier(identifier: str) -> set[str]:
    normalized = normalize_telegram_identifier(identifier)
    if not normalized:
        return set()

    keys = {normalized}
    if normalized.startswith("@"):
        keys.add(normalized.removeprefix("@"))

    if normalized.lstrip("-").isdigit():
        numeric = int(normalized)
        keys.add(str(numeric))

        if normalized.startswith("-100"):
            unsigned = abs(numeric) - 1000000000000
            if unsigned > 0:
                keys.add(str(unsigned))
        elif numeric > 0:
            keys.add(str(_signed_channel_id(numeric)))

    return {item for item in keys if item}


def resolve_target_id(route_map: dict[str, int], candidate_keys: set[str]) -> int | None:
    for key in candidate_keys:
        normalized = normalize_telegram_identifier(key)
        if normalized and normalized in route_map:
            return route_map[normalized]
        if key in route_map:
            return route_map[key]
    return None


def _collect_event_keys(event, chat, message) -> set[str]:
    keys: set[str] = set()

    event_chat_id = getattr(event, "chat_id", None)
    if event_chat_id is not None:
        keys.add(str(event_chat_id))

    chat_id = getattr(chat, "id", None)
    if chat_id is not None:
        keys.add(str(chat_id))
        if int(chat_id) > 0:
            keys.add(str(_signed_channel_id(int(chat_id))))

    username = getattr(chat, "username", None)
    if username:
        keys.add(f"@{str(username).strip().lower()}")

    peer = getattr(message, "peer_id", None)
    channel_id = getattr(peer, "channel_id", None)
    group_id = getattr(peer, "chat_id", None)
    user_id = getattr(peer, "user_id", None)

    if channel_id:
        keys.add(str(channel_id))
        keys.add(str(_signed_channel_id(int(channel_id))))
    if group_id:
        keys.add(str(group_id))
    if user_id:
        keys.add(str(user_id))

    return {item for item in keys if item}


def _extract_message_chat_id(event, message, chat) -> int | None:
    event_chat_id = getattr(event, "chat_id", None)
    if event_chat_id is not None:
        return int(event_chat_id)

    chat_id = getattr(chat, "id", None)
    if chat_id is not None:
        return int(chat_id)

    peer = getattr(message, "peer_id", None)
    channel_id = getattr(peer, "channel_id", None)
    if channel_id:
        return _signed_channel_id(int(channel_id))

    group_id = getattr(peer, "chat_id", None)
    if group_id:
        return int(group_id)

    user_id = getattr(peer, "user_id", None)
    if user_id:
        return int(user_id)

    return None


@dataclass(slots=True)
class AccountSnapshot:
    account_id: int
    label: str
    api_id: str
    api_hash: str
    session_string: str
    route_map: dict[str, int]

    @property
    def signature(self) -> str:
        return f"{self.api_id}:{self.api_hash}:{self.session_string}"


@dataclass(slots=True)
class AccountRuntime:
    snapshot: AccountSnapshot
    client: TelegramClient
    task: asyncio.Task


class TelegramHybridListener:
    def __init__(self, session_factory, refresh_seconds: int = 30):
        self._session_factory = session_factory
        self._refresh_seconds = max(int(refresh_seconds), 5)
        self._states: dict[int, AccountRuntime] = {}

    def _load_snapshots(self) -> dict[int, AccountSnapshot]:
        with self._session_factory() as session:
            accounts = (
                session.execute(
                    select(ParserAccount).where(
                        ParserAccount.parser_type == ParserType.telegram,
                        ParserAccount.is_active.is_(True),
                    )
                )
                .scalars()
                .all()
            )

            rows = session.execute(
                select(TargetAccountLink.account_id, Target.id, Target.identifier)
                .join(Target, TargetAccountLink.target_id == Target.id)
                .where(
                    Target.parser_type == ParserType.telegram,
                    Target.is_active.is_(True),
                    TargetAccountLink.is_active.is_(True),
                )
            ).all()

        route_map_by_account: dict[int, dict[str, int]] = {}
        for account_id, target_id, identifier in rows:
            account_routes = route_map_by_account.setdefault(int(account_id), {})
            for key in build_route_keys_for_identifier(str(identifier or "")):
                account_routes.setdefault(key, int(target_id))

        snapshots: dict[int, AccountSnapshot] = {}
        for account in accounts:
            creds = dict(account.credentials or {})
            api_id = str(creds.get("api_id") or "").strip()
            api_hash = str(creds.get("api_hash") or "").strip()
            session_string = str(creds.get("session_string") or "").strip()
            route_map = route_map_by_account.get(account.id, {})
            if not (api_id and api_hash and session_string):
                continue
            if not route_map:
                continue
            snapshots[account.id] = AccountSnapshot(
                account_id=account.id,
                label=account.label,
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                route_map=route_map,
            )

        return snapshots

    async def _start_account(self, snapshot: AccountSnapshot) -> None:
        client = TelegramClient(StringSession(snapshot.session_string), int(snapshot.api_id), snapshot.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            print(f"[telegram-listener] account #{snapshot.account_id} is not authorized; skipping.")
            return

        @client.on(events.NewMessage(incoming=True))
        async def _on_new_message(event, account_id=snapshot.account_id):
            await self._handle_message(account_id, event)

        task = asyncio.create_task(client.run_until_disconnected())
        self._states[snapshot.account_id] = AccountRuntime(snapshot=snapshot, client=client, task=task)
        print(
            f"[telegram-listener] account #{snapshot.account_id} ({snapshot.label}) listening "
            f"for {len(snapshot.route_map)} route keys."
        )

    async def _stop_account(self, account_id: int) -> None:
        runtime = self._states.pop(account_id, None)
        if not runtime:
            return
        try:
            await runtime.client.disconnect()
        finally:
            if not runtime.task.done():
                try:
                    await asyncio.wait_for(runtime.task, timeout=5)
                except Exception:
                    runtime.task.cancel()
        print(f"[telegram-listener] account #{account_id} listener stopped.")

    def _persist_live_message(
        self,
        target_id: int,
        account_id: int,
        external_id: str,
        observed_at: dt.datetime | None,
        payload: dict[str, Any],
    ) -> None:
        with self._session_factory() as session:
            target = session.get(Target, target_id)
            if target is None:
                return

            persist_events(
                session,
                target=target,
                parser_type=ParserType.telegram.value,
                account_id=account_id,
                owner_user_id=target.owner_user_id,
                events=[
                    ParsedEvent(
                        external_id=external_id,
                        observed_at=observed_at,
                        payload=payload,
                        **normalize_telegram_payload(payload),
                    )
                ],
            )
            upsert_telegram_profile_from_event(
                session=session,
                target_id=target_id,
                account_id=account_id,
                payload=payload,
                observed_at=observed_at,
            )
            try:
                message_id = int(external_id)
                update_offset_from_message(
                    session=session,
                    target_id=target_id,
                    account_id=account_id,
                    message_id=message_id,
                    observed_at=observed_at,
                    source="listener",
                )
            except Exception:
                pass
            session.commit()

    async def _handle_message(self, account_id: int, event) -> None:
        runtime = self._states.get(account_id)
        if not runtime:
            return

        route_map = runtime.snapshot.route_map
        if not route_map:
            return

        message = getattr(event, "message", None)
        message_id = getattr(message, "id", None)
        if not message or not message_id:
            return

        try:
            chat = await event.get_chat()
            keys = _collect_event_keys(event, chat, message)
            target_id = resolve_target_id(route_map, keys)
            if not target_id:
                return

            sender = await message.get_sender()
            chat_id = _extract_message_chat_id(event, message, chat)
            observed_at = _coerce_utc(getattr(message, "date", None))

            payload = {
                "event_type": "telegram_message",
                "message_id": int(message_id),
                "date": observed_at.isoformat() if observed_at else None,
                "text": getattr(message, "message", None),
                "chat_id": chat_id,
                "sender": {
                    "id": getattr(sender, "id", None),
                    "username": getattr(sender, "username", None),
                    "first_name": getattr(sender, "first_name", None),
                    "last_name": getattr(sender, "last_name", None),
                },
                "raw": message.to_dict(),
                "source": "telegram_listener",
            }

            await asyncio.to_thread(
                self._persist_live_message,
                int(target_id),
                int(account_id),
                str(message_id),
                observed_at,
                payload,
            )
        except Exception as exc:
            print(f"[telegram-listener] account #{account_id} message handling error: {exc}")

    async def _sync_accounts(self) -> None:
        desired = self._load_snapshots()

        for account_id in list(self._states.keys()):
            if account_id not in desired:
                await self._stop_account(account_id)

        for account_id, snapshot in desired.items():
            current = self._states.get(account_id)
            if current is None:
                await self._start_account(snapshot)
                continue

            if current.snapshot.signature != snapshot.signature:
                await self._stop_account(account_id)
                await self._start_account(snapshot)
                continue

            current.snapshot = snapshot
            if current.task.done():
                try:
                    err = current.task.exception()
                except Exception:
                    err = None
                print(f"[telegram-listener] account #{account_id} disconnected: {err}. restarting.")
                await self._stop_account(account_id)
                await self._start_account(snapshot)

    async def run_forever(self) -> None:
        print(f"[telegram-listener] started, refresh interval {self._refresh_seconds}s.")
        try:
            while True:
                await self._sync_accounts()
                await asyncio.sleep(self._refresh_seconds)
        finally:
            for account_id in list(self._states.keys()):
                await self._stop_account(account_id)


def run_telegram_listener_forever(session_factory, refresh_seconds: int = 30) -> None:
    listener = TelegramHybridListener(session_factory=session_factory, refresh_seconds=refresh_seconds)
    asyncio.run(listener.run_forever())
