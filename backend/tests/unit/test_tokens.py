import hashlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from secagent.auth.passwords import hash_password, verify_password
from secagent.auth.tokens import (
    TokenValidationError,
    decode_access_token,
    issue_access_token,
    new_refresh_token,
)


def test_password_hash_uses_argon2_and_verifies():
    encoded = hash_password("Correct-Horse-9")

    assert encoded.startswith("$argon2")
    assert "Correct-Horse-9" not in encoded
    assert verify_password(encoded, "Correct-Horse-9") is True
    assert verify_password(encoded, "wrong") is False
    assert verify_password("not-an-argon2-hash", "Correct-Horse-9") is False


def test_access_token_round_trip():
    key = "unit-test-signing-key-at-least-32-bytes"
    token = issue_access_token("user-1", "analyst", key, minutes=15)

    claims = decode_access_token(token, key)

    assert claims.sub == "user-1"
    assert claims.role == "analyst"
    assert claims.exp > claims.iat
    assert claims.jti


def test_expired_access_token_is_rejected():
    now = datetime.now(timezone.utc)
    key = "unit-test-signing-key-at-least-32-bytes"
    token = jwt.encode(
        {
            "sub": "user-1",
            "role": "analyst",
            "iat": now - timedelta(minutes=2),
            "exp": now - timedelta(minutes=1),
            "jti": "expired-jti",
        },
        key,
        algorithm="HS256",
    )

    with pytest.raises(TokenValidationError):
        decode_access_token(token, key)


def test_refresh_token_returns_only_a_one_way_hash_for_storage():
    raw, digest = new_refresh_token()

    assert raw != digest
    assert digest == hashlib.sha256(raw.encode()).hexdigest()
    assert len(digest) == 64
