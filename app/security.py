import hashlib
import secrets

import pyotp
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def build_totp_uri(secret: str, username: str, issuer: str = "DataAggregator") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp_code(secret: str | None, code: str, valid_window: int = 1) -> bool:
    if not secret:
        return False
    normalized = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(normalized) < 6:
        return False
    return bool(pyotp.TOTP(secret).verify(normalized, valid_window=valid_window))


def generate_registration_token(prefix: str = "inv") -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()
