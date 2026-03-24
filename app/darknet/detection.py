from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector

from app.config import get_settings
from app.darknet.adapters import suggest_adapter_for_detected

settings = get_settings()


@dataclass(slots=True)
class AdapterDetectionResult:
    detected: str | None
    recommended_adapter: str
    scores: dict[str, int]
    matches: dict[str, list[str]]
    checked_urls: list[str]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "recommended_adapter": self.recommended_adapter,
            "scores": self.scores,
            "matches": self.matches,
            "checked_urls": self.checked_urls,
            "error": self.error,
        }


SIGNATURES: dict[str, list[str]] = {
    "xenforo_like": [
        "data-xf-init",
        "_xftoken",
        "p-title-value",
        "/threads/",
        "xenforo",
    ],
    "phpbb_like": [
        "phpbb",
        "viewtopic.php",
        "ucp.php",
        "posting.php",
        "sid=",
    ],
    "vbulletin_like": [
        "vbulletin",
        "showthread.php",
        "member.php",
        "vbmenu_control",
        "forumdisplay.php",
    ],
}


def _normalize_urls(start_urls: list[str]) -> list[str]:
    unique: list[str] = []
    for raw in start_urls:
        url = str(raw or "").strip()
        if not url:
            continue
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            continue
        if url not in unique:
            unique.append(url)
    return unique


async def _fetch_text(client: aiohttp.ClientSession, url: str) -> str:
    async with client.get(url, allow_redirects=True) as response:
        if response.status >= 400:
            return ""
        return await response.text(errors="ignore")


def _score_html(html: str, matches: dict[str, list[str]]) -> None:
    lower = html.lower()
    for adapter_name, signatures in SIGNATURES.items():
        bucket = matches.setdefault(adapter_name, [])
        for sig in signatures:
            if sig.lower() in lower and sig not in bucket:
                bucket.append(sig)


def _pick_detected(scores: dict[str, int]) -> str | None:
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return None
    top_name, top_score = ranked[0]
    if top_score <= 0:
        return None
    return top_name


async def detect_adapter(start_urls: list[str], max_urls: int = 3) -> AdapterDetectionResult:
    urls = _normalize_urls(start_urls)[: max(max_urls, 1)]
    if not urls:
        return AdapterDetectionResult(
            detected=None,
            recommended_adapter="xenforo_like",
            scores={name: 0 for name in SIGNATURES},
            matches={name: [] for name in SIGNATURES},
            checked_urls=[],
            error="No valid URLs to inspect",
        )

    matches: dict[str, list[str]] = {name: [] for name in SIGNATURES}
    checked_urls: list[str] = []
    first_error: str | None = None

    connector = ProxyConnector.from_url(settings.tor_proxy)
    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": "data-aggregator/detect-adapter/1.0"}

    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as client:
        for url in urls:
            try:
                html = await _fetch_text(client, url)
            except Exception as exc:
                if first_error is None:
                    first_error = str(exc)
                continue
            checked_urls.append(url)
            if html:
                _score_html(html, matches)

    scores = {adapter_name: len(found) for adapter_name, found in matches.items()}
    detected = _pick_detected(scores)
    recommended = suggest_adapter_for_detected(detected)

    return AdapterDetectionResult(
        detected=detected,
        recommended_adapter=recommended,
        scores=scores,
        matches=matches,
        checked_urls=checked_urls,
        error=first_error,
    )
