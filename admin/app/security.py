import hashlib
import secrets
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import get_settings
from app.timeutil import utcnow

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_ACCESS_HOURS = 12
_REFRESH_DAYS = 14
_MEMBER_DAYS = 30


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_access_token(*, member_id: int, role: str, tenant_id: int | None) -> str:
    now = utcnow()
    payload = {
        "sub": str(member_id),
        "role": role,
        "tenant_id": tenant_id,
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(hours=_ACCESS_HOURS),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def new_member_token(*, member_id: int, role: str, tenant_id: int | None) -> str:
    now = utcnow()
    payload = {
        "sub": str(member_id),
        "role": role,
        "tenant_id": tenant_id,
        "typ": "member",
        "iat": now,
        "exp": now + timedelta(days=_MEMBER_DAYS),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])


def new_refresh_value() -> str:
    return secrets.token_urlsafe(32)


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)
