#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            return json.loads(text) if text else {}
    except urlerror.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {details}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Local helper for darknet browser auth state upload")
    parser.add_argument("--api-base", required=True, help="API base, e.g. http://aggredata.localhost")
    parser.add_argument("--auth-token", required=True, help="One-time helper token from UI")
    parser.add_argument("--forum-url", required=True, help="Forum URL to open in browser")
    parser.add_argument("--proxy-url", default="default", help="Proxy URL for parser account (default/direct/socks5://...)")
    parser.add_argument("--browser-user-agent", default="", help="Optional browser user-agent to store")
    parser.add_argument("--fallback-form-login", action="store_true", help="Allow fallback to form login in parser")
    parser.add_argument("--ensure-chromium", action="store_true", help="Run `npx playwright install chromium` before auth")
    parser.add_argument("--playwright-bin", default="npx", help="Playwright launcher binary (default: npx)")
    args = parser.parse_args()

    if shutil.which(args.playwright_bin) is None:
        print(f"[error] `{args.playwright_bin}` not found in PATH", file=sys.stderr)
        return 2

    api_base = str(args.api_base).rstrip("/")
    upload_url = f"{api_base}/api/modules/darknet/auth-helper/complete"

    with tempfile.TemporaryDirectory(prefix="darknet-auth-") as tmp_dir:
        state_path = Path(tmp_dir) / "storage-state.json"

        if args.ensure_chromium:
            print("[info] Installing Chromium for Playwright...")
            _run([args.playwright_bin, "playwright", "install", "chromium"])

        cmd = [
            args.playwright_bin,
            "playwright",
            "codegen",
            "--browser",
            "chromium",
            "--save-storage",
            str(state_path),
            args.forum_url,
        ]
        proxy_raw = str(args.proxy_url or "").strip()
        proxy_lower = proxy_raw.lower()
        if proxy_raw and proxy_lower not in {"default", "direct", "none", "off"}:
            cmd.extend(["--proxy-server", proxy_raw])

        print("[info] Opened Playwright codegen.")
        print("[info] Log in manually (including captcha), then close the codegen window.")
        print("[info] Running:", " ".join(cmd))
        try:
            _run(cmd)
        except subprocess.CalledProcessError:
            print("[warn] Playwright codegen failed. Trying to install Chromium and retry once...")
            _run([args.playwright_bin, "playwright", "install", "chromium"])
            _run(cmd)

        if not state_path.exists():
            print("[error] storage-state.json was not created", file=sys.stderr)
            return 3

        storage_state_json = state_path.read_text(encoding="utf-8")
        payload = {
            "auth_token": args.auth_token,
            "storage_state_json": storage_state_json,
            "proxy_url": proxy_raw or "default",
            "browser_user_agent": str(args.browser_user_agent or "").strip(),
            "fallback_form_login": bool(args.fallback_form_login),
        }
        print("[info] Uploading storage state to API...")
        response = _post_json(upload_url, payload=payload, timeout=90)
        print("[ok] Uploaded.")
        print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
