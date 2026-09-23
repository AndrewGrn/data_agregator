"""Bearer tokens for machine access.

The raw token exists exactly once, in the response that mints it. Everything
stored is derived: a SHA-256 for lookup and a short prefix so a token can be
recognised in a list without revealing it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiToken, User

TOKEN_PREFIX = "agg_"
_SECRET_BYTES = 32  # 256 bits of entropy; brute force is not a concern


def hash_token(raw: str) -> str:
    """SHA-256, not bcrypt: a 256-bit random token has no guessable structure,
    so key stretching buys nothing and would cost a hash on every request."""
    return hashlib.sha256(raw.strip().encode()).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Return (raw, hash, prefix). The raw value is never persisted."""
    raw = TOKEN_PREFIX + secrets.token_hex(_SECRET_BYTES)
    return raw, hash_token(raw), raw[: len(TOKEN_PREFIX) + 6]


def create_token(
    session: Session,
    *,
    owner_user_id: int,
    name: str,
    expires_at: dt.datetime | None = None,
) -> tuple[ApiToken, str]:
    """Mint a token. expires_at=None means it never expires."""
    label = " ".join(str(name or "").split())[:64] or "token"
    raw, digest, prefix = generate_token()
    token = ApiToken(
        name=label,
        token_hash=digest,
        prefix=prefix,
        owner_user_id=int(owner_user_id),
        expires_at=expires_at,
    )
    session.add(token)
    session.flush()
    return token, raw


def resolve_token(session: Session, raw: str, *, now: dt.datetime | None = None) -> User | None:
    """Return the owner of a usable token, or None.

    None covers every failure the caller must treat identically — unknown,
    revoked, expired, or owned by a deactivated user — so a caller cannot tell
    them apart and probe for valid values.
    """
    value = str(raw or "").strip()
    if not value:
        return None
    now = now or dt.datetime.now(dt.UTC)

    token = session.execute(
        select(ApiToken).where(ApiToken.token_hash == hash_token(value))
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None

    expires_at = token.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:  # SQLite drops tzinfo; Postgres keeps it
            expires_at = expires_at.replace(tzinfo=dt.UTC)
        if expires_at <= now:
            return None

    user = session.get(User, token.owner_user_id)
    if user is None or not bool(user.is_active):
        return None

    # Cheap audit trail; skipped when it would not change the stored minute, so a
    # busy integration does not write on every single request.
    last = token.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=dt.UTC)
    if last is None or (now - last).total_seconds() > 60:
        token.last_used_at = now

    return user


def revoke_token(session: Session, token: ApiToken, *, now: dt.datetime | None = None) -> None:
    token.revoked_at = now or dt.datetime.now(dt.UTC)


def list_tokens(session: Session, *, owner_user_id: int | None) -> list[ApiToken]:
    stmt = select(ApiToken).order_by(ApiToken.created_at.desc())
    if owner_user_id is not None:
        stmt = stmt.where(ApiToken.owner_user_id == int(owner_user_id))
    return list(session.execute(stmt).scalars())
