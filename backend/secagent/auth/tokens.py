import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import jwt
from pydantic import BaseModel, ValidationError


class TokenValidationError(ValueError):
    pass


class AccessClaims(BaseModel):
    sub: str
    role: Literal["admin", "analyst"]
    exp: int
    iat: int
    jti: str


def issue_access_token(user_id: str, role: str, key: str, minutes: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, key, algorithm="HS256")


def decode_access_token(token: str, key: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            options={"require": ["sub", "role", "iat", "exp", "jti"]},
        )
        return AccessClaims.model_validate(payload)
    except (jwt.PyJWTError, ValidationError) as exc:
        raise TokenValidationError("invalid access token") from exc


def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
