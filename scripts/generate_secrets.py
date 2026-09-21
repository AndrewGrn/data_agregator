#!/usr/bin/env python3
"""Print strong replacement values for the placeholder secrets in .env.

Usage: python scripts/generate_secrets.py
"""
from __future__ import annotations

import secrets


def main() -> None:
    print(f"SECRET_KEY={secrets.token_urlsafe(32)}")
    print(f"DEFAULT_ADMIN_PASSWORD={secrets.token_urlsafe(16)}")
    print(f"S3_ACCESS_KEY={secrets.token_hex(10)}")
    print(f"S3_SECRET_KEY={secrets.token_urlsafe(32)}")


if __name__ == "__main__":
    main()
