from __future__ import annotations

import datetime as dt

import aiohttp
from aiohttp_socks import ProxyConnector
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ParseJob, ParserAccount, ParserType, Target
from app.plugins.base import JobSpec, ParsedEvent, ParserPlugin

settings = get_settings()


class DarknetPlugin(ParserPlugin):
    parser_type = ParserType.darknet.value

    def generate_jobs(self, session: Session, target: Target) -> list[JobSpec]:
        return [
            JobSpec(
                parser_type=self.parser_type,
                target_id=target.id,
                account_id=None,
                job_key=f"poll:{target.id}",
                payload={"url": target.identifier},
            )
        ]

    async def _fetch_url(self, url: str) -> dict:
        connector = ProxyConnector.from_url(settings.tor_proxy)
        timeout = aiohttp.ClientTimeout(total=45)
        headers = {"User-Agent": "data-aggregator/0.1"}

        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as client:
            async with client.get(url, allow_redirects=True) as response:
                html = await response.text(errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                title = soup.title.get_text(strip=True) if soup.title else None
                return {
                    "url": str(response.url),
                    "status": response.status,
                    "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
                    "title": title,
                    "html": html,
                }

    def run(self, session: Session, job: ParseJob, target: Target, account: ParserAccount | None) -> list[ParsedEvent]:
        import asyncio

        payload = asyncio.run(self._fetch_url(job.payload.get("url", target.identifier)))
        return [ParsedEvent(external_id=payload.get("url"), observed_at=dt.datetime.now(dt.UTC), payload=payload)]
