from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

from yarl import URL


def parse_storage_state(raw: Any) -> dict:
    if raw is None:
        return {"cookies": [], "origins": []}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {"cookies": [], "origins": []}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid storage state JSON: {exc}") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise ValueError("storage_state must be dict or JSON string")

    cookies_raw = data.get("cookies")
    origins_raw = data.get("origins")
    cookies_out: list[dict] = []
    origins_out: list[dict] = []

    if isinstance(cookies_raw, list):
        for item in cookies_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            value = str(item.get("value") or "")
            domain = str(item.get("domain") or "").strip()
            path = str(item.get("path") or "/").strip() or "/"
            expires = item.get("expires")
            try:
                expires_value = float(expires) if expires is not None else None
            except Exception:
                expires_value = None
            cookies_out.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path if path.startswith("/") else f"/{path}",
                    "expires": expires_value,
                    "httpOnly": bool(item.get("httpOnly", False)),
                    "secure": bool(item.get("secure", False)),
                    "sameSite": str(item.get("sameSite") or "").strip() or None,
                }
            )

    if isinstance(origins_raw, list):
        for item in origins_raw:
            if not isinstance(item, dict):
                continue
            origin = str(item.get("origin") or "").strip()
            local_storage_raw = item.get("localStorage")
            local_storage: list[dict] = []
            if isinstance(local_storage_raw, list):
                for local in local_storage_raw:
                    if not isinstance(local, dict):
                        continue
                    key = str(local.get("name") or "").strip()
                    if not key:
                        continue
                    local_storage.append({"name": key, "value": str(local.get("value") or "")})
            origins_out.append({"origin": origin, "localStorage": local_storage})

    return {"cookies": cookies_out, "origins": origins_out}


def summarize_storage_state(storage_state: dict | None) -> dict:
    data = storage_state if isinstance(storage_state, dict) else {}
    cookies = data.get("cookies")
    origins = data.get("origins")
    return {
        "cookies_count": len(cookies) if isinstance(cookies, list) else 0,
        "origins_count": len(origins) if isinstance(origins, list) else 0,
    }


def apply_storage_state_to_cookie_jar(cookie_jar, base_url: str, storage_state: dict | None) -> int:
    if not isinstance(storage_state, dict):
        return 0

    parsed = urlparse(base_url)
    base_host = str(parsed.hostname or "").strip().lower()
    scheme = str(parsed.scheme or "https").strip() or "https"
    now_ts = time.time()
    applied = 0

    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        return 0

    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        expires = item.get("expires")
        if expires is not None:
            try:
                expires_value = float(expires)
                if expires_value > 0 and expires_value <= now_ts:
                    continue
            except Exception:
                pass

        domain = str(item.get("domain") or "").strip().lstrip(".").lower()
        if not domain:
            domain = base_host
        if not domain:
            continue

        if base_host and not (base_host == domain or base_host.endswith(f".{domain}")):
            continue

        value = str(item.get("value") or "")
        path = str(item.get("path") or "/").strip() or "/"
        if not path.startswith("/"):
            path = f"/{path}"

        response_url = URL.build(scheme=scheme, host=domain, path=path)
        cookie_jar.update_cookies({name: value}, response_url=response_url)
        applied += 1

    return applied
