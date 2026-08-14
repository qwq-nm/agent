import re
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "external_response",
    "jwt",
    "password",
    "provider_response",
    "raw_response",
    "secret",
    "stack",
    "token",
    "traceback",
}
SECRET_PATTERN = re.compile(
    r"(?:\b(?:sk|api)[-_][A-Za-z0-9_-]{4,}\b|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|"
    r"\bBearer\s+[A-Za-z0-9._~-]+)",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    return SECRET_PATTERN.sub("***REDACTED***", value)


def redact_mapping(value: Any, key: str = "") -> Any:
    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    compact_key = normalized_key.replace("_", "")
    if any(
        sensitive in normalized_key or sensitive.replace("_", "") in compact_key
        for sensitive in SENSITIVE_KEYS
    ):
        return "***REDACTED***"
    if isinstance(value, Mapping):
        return {
            item_key: redact_mapping(item, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_mapping(item, key) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[UNSUPPORTED]"
