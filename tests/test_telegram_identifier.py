from __future__ import annotations

import pytest

from app.services.telegram_accounts import (
    invite_hash,
    is_invite_identifier,
    normalize_telegram_identifier,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("@Durov", "@durov"),
        ("durov", "durov"),
        ("t.me/durov", "@durov"),
        ("https://t.me/Durov", "@durov"),
        ("https://telegram.me/durov", "@durov"),
        ("-1001234567890", "-1001234567890"),
        ("1234567890", "1234567890"),
        ("https://t.me/+AbCdEf123", "invite:AbCdEf123"),
        ("t.me/+AbCdEf123", "invite:AbCdEf123"),
        ("https://t.me/joinchat/AbCdEf123", "invite:AbCdEf123"),
        ("telegram.me/joinchat/AbCdEf123", "invite:AbCdEf123"),
        ("https://t.me/c/2707984934/1393921", "-1002707984934"),
        ("t.me/c/2707984934", "-1002707984934"),
        ("https://t.me/c/2707984934/1393921?single", "-1002707984934"),
        ("  ", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize_telegram_identifier(raw) == expected


def test_invite_helpers():
    assert is_invite_identifier("invite:AbC") is True
    assert is_invite_identifier("@durov") is False
    assert invite_hash("invite:AbC") == "AbC"
    assert invite_hash("@durov") is None
