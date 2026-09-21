from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.darknet.adapters import get_darknet_adapter, normalize_darknet_adapter, suggest_adapter_for_detected
from app.darknet.browser_state import apply_storage_state_to_cookie_jar, parse_storage_state
from app.models import JobStatus, OnboardingStatus, ParseJob, ParserAccount, ParserType, RawEvent, Target, TargetAccountLink
from app.plugins.base import JobSpec, ParsedEvent, ParserPlugin
from app.services.job_routing import QUEUE_DARKNET, default_max_attempts_for_queue

settings = get_settings()


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    for item in values:
        if item and item not in out:
            out.append(item)
    return out


class DarknetPlugin(ParserPlugin):
    parser_type = ParserType.darknet.value

    def _collect_maximum(self, target: Target) -> bool:
        config = target.config or {}
        return bool(config.get("collect_maximum", True))

    def _resolve_start_urls(self, target: Target) -> list[str]:
        config = target.config or {}
        start_urls = config.get("start_urls")
        if isinstance(start_urls, list):
            parsed = [str(x).strip() for x in start_urls if str(x).strip()]
            if parsed:
                return _unique(parsed)

        identifier = target.identifier.strip()
        if identifier:
            return [identifier]
        return []

    def _resolve_adapter_name(self, target: Target) -> str:
        config = target.config or {}
        manual = str(config.get("adapter", "")).strip()
        if manual:
            try:
                return normalize_darknet_adapter(manual)
            except ValueError:
                pass
        detected = str(config.get("adapter_detected", "")).strip()
        if detected:
            return suggest_adapter_for_detected(detected)
        return "xenforo"

    def _requires_account(self, target: Target) -> bool:
        config = target.config or {}
        return bool(config.get("login_required", False))

    def _reparse_existing_threads(self, target: Target) -> bool:
        config = target.config or {}
        return bool(config.get("reparse_existing_threads", True))

    def _thread_reparse_interval_minutes(self, target: Target) -> int:
        config = target.config or {}
        return max(int(config.get("thread_reparse_interval_minutes", 15)), 1)

    def _discover_interval_seconds(self, target: Target) -> int:
        config = target.config or {}
        return max(int(config.get("discover_interval_seconds", 60)), 10)

    def _linked_accounts(self, session: Session, target: Target) -> list[ParserAccount]:
        rows = (
            session.execute(
                select(ParserAccount)
                .join(TargetAccountLink, TargetAccountLink.account_id == ParserAccount.id)
                .where(
                    TargetAccountLink.target_id == target.id,
                    TargetAccountLink.is_active.is_(True),
                    ParserAccount.is_active.is_(True),
                    ParserAccount.parser_type == ParserType.darknet,
                )
                .order_by(ParserAccount.id.asc())
            )
            .scalars()
            .all()
        )
        return rows

    def _load_last_thread_seen_map(self, session: Session, target: Target, thread_urls: list[str]) -> dict[str, dt.datetime]:
        if not thread_urls:
            return {}
        summary_ids = [f"thread_summary:{url}" for url in thread_urls]
        rows = (
            session.execute(
                select(RawEvent.external_id, func.max(RawEvent.created_at))
                .where(
                    RawEvent.parser_type == ParserType.darknet,
                    RawEvent.target_id == target.id,
                    RawEvent.external_id.in_(summary_ids),
                )
                .group_by(RawEvent.external_id)
            )
            .all()
        )
        out: dict[str, dt.datetime] = {}
        for external_id, last_seen in rows:
            if not external_id or not last_seen:
                continue
            if external_id.startswith("thread_summary:"):
                out[external_id.removeprefix("thread_summary:")] = last_seen
        return out

    def _select_threads_for_parse(
        self,
        target: Target,
        thread_urls: list[str],
        last_seen_map: dict[str, dt.datetime],
        now: dt.datetime,
    ) -> tuple[list[str], dict[str, int]]:
        reparse_enabled = self._reparse_existing_threads(target) or self._collect_maximum(target)
        interval_minutes = self._thread_reparse_interval_minutes(target)
        due_before = now - dt.timedelta(minutes=interval_minutes)

        selected: list[str] = []
        stats = {
            "new_threads": 0,
            "reparse_threads": 0,
            "skipped_recent_threads": 0,
        }

        for thread_url in thread_urls:
            last_seen = last_seen_map.get(thread_url)
            if not last_seen:
                selected.append(thread_url)
                stats["new_threads"] += 1
                continue
            if not reparse_enabled:
                stats["skipped_recent_threads"] += 1
                continue
            if last_seen <= due_before:
                selected.append(thread_url)
                stats["reparse_threads"] += 1
            else:
                stats["skipped_recent_threads"] += 1

        return selected, stats

    def _load_existing_external_ids(self, session: Session, target: Target, external_ids: list[str]) -> set[str]:
        if not external_ids:
            return set()
        rows = (
            session.execute(
                select(RawEvent.external_id).where(
                    RawEvent.parser_type == ParserType.darknet,
                    RawEvent.target_id == target.id,
                    RawEvent.external_id.in_(external_ids),
                )
            )
            .scalars()
            .all()
        )
        return {str(item) for item in rows if item}

    def generate_jobs(self, session: Session, target: Target) -> list[JobSpec]:
        start_urls = self._resolve_start_urls(target)
        if not start_urls:
            target.onboarding_status = OnboardingStatus.blocked
            return []

        adapter_name = self._resolve_adapter_name(target)
        login_required = self._requires_account(target)
        discover_interval_seconds = self._discover_interval_seconds(target)

        if login_required:
            accounts = self._linked_accounts(session, target)
            if not accounts:
                target.onboarding_status = OnboardingStatus.needs_account
                return []

            target.onboarding_status = OnboardingStatus.ready
            return [
                JobSpec(
                    parser_type=self.parser_type,
                    target_id=target.id,
                    account_id=account.id,
                    job_key=f"darknet:discover:{target.id}:{account.id}",
                    payload={
                        "stage": "discover_threads",
                        "adapter": adapter_name,
                        "start_urls": start_urls,
                        "min_interval_seconds": discover_interval_seconds,
                    },
                    queue=QUEUE_DARKNET,
                    max_attempts=default_max_attempts_for_queue(QUEUE_DARKNET),
                )
                for account in accounts
            ]

        target.onboarding_status = OnboardingStatus.ready
        return [
            JobSpec(
                parser_type=self.parser_type,
                target_id=target.id,
                account_id=None,
                job_key=f"darknet:discover:{target.id}",
                payload={
                    "stage": "discover_threads",
                    "adapter": adapter_name,
                    "start_urls": start_urls,
                    "min_interval_seconds": discover_interval_seconds,
                },
                queue=QUEUE_DARKNET,
                max_attempts=default_max_attempts_for_queue(QUEUE_DARKNET),
            )
        ]

    def _resolve_proxy_url(self, account: ParserAccount | None) -> str | None:
        if account:
            creds = account.credentials if isinstance(account.credentials, dict) else {}
            raw = str(creds.get("proxy_url") or "").strip()
            if raw:
                lowered = raw.lower()
                if lowered in {"none", "direct", "off"}:
                    return None
                if lowered != "default":
                    return raw
        default_proxy = str(settings.tor_proxy or "").strip()
        return default_proxy or None

    async def _open_client(self, account: ParserAccount | None = None, base_url: str | None = None):
        proxy_url = self._resolve_proxy_url(account)
        connector = ProxyConnector.from_url(proxy_url) if proxy_url else aiohttp.TCPConnector()
        timeout = aiohttp.ClientTimeout(total=max(int(settings.darknet_http_timeout_seconds), 30))
        headers = {"User-Agent": "data-aggregator/0.2"}
        storage_state: dict | None = None
        if account and isinstance(account.credentials, dict):
            browser_user_agent = str(account.credentials.get("browser_user_agent") or "").strip()
            if browser_user_agent:
                headers["User-Agent"] = browser_user_agent
            auth_mode = str(account.credentials.get("auth_mode") or "").strip().lower()
            raw_state = account.credentials.get("storage_state")
            if raw_state is not None:
                if auth_mode in {"browser_state", "storage_state", "cookies"}:
                    storage_state = parse_storage_state(raw_state)
                else:
                    try:
                        storage_state = parse_storage_state(raw_state)
                    except Exception:
                        storage_state = None

        cookie_jar = aiohttp.CookieJar(unsafe=True)
        client = aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers, cookie_jar=cookie_jar)
        if base_url and storage_state:
            apply_storage_state_to_cookie_jar(client.cookie_jar, base_url=base_url, storage_state=storage_state)
        return client

    def _build_base_url(self, target: Target, start_urls: list[str]) -> str:
        if start_urls:
            parsed = urlparse(start_urls[0])
            return f"{parsed.scheme}://{parsed.netloc}"
        parsed = urlparse(target.identifier)
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _discover_stage(
        self,
        session: Session,
        job: ParseJob,
        target: Target,
        account: ParserAccount | None,
    ) -> list[ParsedEvent]:
        payload = job.payload or {}
        config = target.config or {}
        start_urls = payload.get("start_urls") or self._resolve_start_urls(target)
        adapter = get_darknet_adapter(str(payload.get("adapter") or self._resolve_adapter_name(target)))
        base_url = self._build_base_url(target, start_urls)

        async with await self._open_client(account=account, base_url=base_url) as client:
            if self._requires_account(target):
                if not account:
                    raise ValueError("Darknet target requires account authentication")
                ok = await adapter.login(client, base_url, account.credentials or {})
                if not ok:
                    raise ValueError(f"Darknet login failed for account '{account.label}'")

            thread_urls = await adapter.discover_thread_urls(client, start_urls, config)

        now = _now_utc()
        last_seen_map = self._load_last_thread_seen_map(session, target, thread_urls)
        candidate_threads, selection_stats = self._select_threads_for_parse(target, thread_urls, last_seen_map, now)

        collect_maximum = self._collect_maximum(target)
        if collect_maximum:
            thread_urls_to_schedule = candidate_threads
        else:
            try:
                max_threads = int(config.get("max_threads_per_cycle", 30))
            except Exception:
                max_threads = 30
            max_threads = max(max_threads, 1)
            thread_urls_to_schedule = candidate_threads[:max_threads]

        created = 0
        skipped = 0
        for thread_url in thread_urls_to_schedule:
            url_hash = hashlib.sha1(thread_url.encode("utf-8")).hexdigest()[:16]
            job_key = f"darknet:thread:{target.id}:{account.id if account else 'anon'}:{url_hash}"
            existing = session.execute(
                select(ParseJob).where(
                    ParseJob.job_key == job_key,
                    ParseJob.status.in_([JobStatus.pending, JobStatus.running, JobStatus.retry]),
                )
            ).scalar_one_or_none()
            if existing:
                skipped += 1
                continue

            session.add(
                ParseJob(
                    parser_type=ParserType.darknet,
                    target_id=target.id,
                    account_id=account.id if account else None,
                    owner_user_id=target.owner_user_id,
                    job_key=job_key,
                    queue=QUEUE_DARKNET,
                    max_attempts=default_max_attempts_for_queue(QUEUE_DARKNET),
                    status=JobStatus.pending,
                    run_after=_now_utc(),
                    payload={
                        "stage": "parse_thread",
                        "adapter": adapter.name,
                        "thread_url": thread_url,
                        "discovered_at": now.isoformat(),
                    },
                )
            )
            created += 1

        deferred_by_limit = max(0, len(candidate_threads) - len(thread_urls_to_schedule))
        return [
            ParsedEvent(
                external_id=f"discover:{target.id}:{now.isoformat()}",
                observed_at=now,
                payload={
                    "event_type": "darknet_discovery",
                    "target_id": target.id,
                    "account_id": account.id if account else None,
                    "thread_urls_found": len(thread_urls),
                    "thread_urls_selected": len(candidate_threads),
                    "jobs_created": created,
                    "jobs_skipped": skipped,
                    "deferred_by_limit": deferred_by_limit,
                    **selection_stats,
                    "threads": thread_urls_to_schedule,
                },
            )
        ]

    async def _parse_thread_stage(
        self,
        session: Session,
        target: Target,
        account: ParserAccount | None,
        payload: dict,
    ) -> list[ParsedEvent]:
        config = target.config or {}
        adapter = get_darknet_adapter(str(payload.get("adapter") or self._resolve_adapter_name(target)))
        thread_url = str(payload.get("thread_url") or "").strip()
        if not thread_url:
            raise ValueError("Missing thread_url for darknet parse_thread stage")
        base_url = self._build_base_url(target, [thread_url])

        async with await self._open_client(account=account, base_url=base_url) as client:
            if self._requires_account(target):
                if not account:
                    raise ValueError("Darknet thread parsing requires account")
                ok = await adapter.login(client, base_url, account.credentials or {})
                if not ok:
                    raise ValueError(f"Darknet login failed for account '{account.label}'")

            result = await adapter.parse_thread(client, thread_url, config)

        events: list[ParsedEvent] = []
        post_external_ids = [f"{result.thread_url}#post:{post.post_id}" for post in result.posts]
        user_external_ids = [f"{result.thread_url}#user:{str(user.get('username') or 'unknown')}" for user in result.users]
        existing_post_ids = self._load_existing_external_ids(session, target, post_external_ids)
        existing_user_ids = self._load_existing_external_ids(session, target, user_external_ids)

        new_posts_count = 0
        skipped_existing_posts = 0
        for post in result.posts:
            external_id = f"{result.thread_url}#post:{post.post_id}"
            if external_id in existing_post_ids:
                skipped_existing_posts += 1
                continue
            events.append(
                ParsedEvent(
                    external_id=external_id,
                    observed_at=_now_utc(),
                    payload={
                        "event_type": "forum_post",
                        "thread_url": result.thread_url,
                        "thread_title": result.thread_title,
                        "post_id": post.post_id,
                        "author": post.author,
                        "posted_at": post.posted_at,
                        "text": post.text,
                        "raw": post.raw,
                    },
                )
            )
            new_posts_count += 1

        new_users_count = 0
        skipped_existing_users = 0
        for user in result.users:
            username = str(user.get("username") or "unknown")
            external_id = f"{result.thread_url}#user:{username}"
            if external_id in existing_user_ids:
                skipped_existing_users += 1
                continue
            events.append(
                ParsedEvent(
                    external_id=external_id,
                    observed_at=_now_utc(),
                    payload={
                        "event_type": "forum_user",
                        "thread_url": result.thread_url,
                        "thread_title": result.thread_title,
                        "user": user,
                    },
                )
            )
            new_users_count += 1

        events.append(
            ParsedEvent(
                external_id=f"thread_summary:{result.thread_url}",
                observed_at=_now_utc(),
                payload={
                    "event_type": "thread_summary",
                    "thread_url": result.thread_url,
                    "thread_title": result.thread_title,
                    "posts_count": len(result.posts),
                    "users_count": len(result.users),
                    "new_posts_count": new_posts_count,
                    "new_users_count": new_users_count,
                    "skipped_existing_posts": skipped_existing_posts,
                    "skipped_existing_users": skipped_existing_users,
                },
            )
        )

        return events

    def run(self, session: Session, job: ParseJob, target: Target, account: ParserAccount | None) -> list[ParsedEvent]:
        payload = job.payload or {}
        stage = str(payload.get("stage") or "discover_threads")

        if stage == "discover_threads":
            return asyncio.run(self._discover_stage(session, job, target, account))
        if stage == "parse_thread":
            return asyncio.run(self._parse_thread_stage(session, target, account, payload))

        raise ValueError(f"Unknown darknet job stage: {stage}")
