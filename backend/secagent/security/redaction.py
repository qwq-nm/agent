import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = {"authorization", "api_key", "cookie", "password", "secret", "token"}
SECRET_PATTERN = re.compile(r"\b(?:sk|api)[-_][A-Za-z0-9_-]{4,}\b", re.IGNORECASE)


def redact_text(value: str) -> str:
    return SECRET_PATTERN.sub("***REDACTED***", value)


def redact_mapping(value: Any, key: str = "") -> Any:
    if any(sensitive in key.lower() for sensitive in SENSITIVE_KEYS):
        return "***REDACTED***"
    if isinstance(value, Mapping):
        return {
            item_key: redact_mapping(item, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item, key) for item in value]
    return redact_text(value) if isinstance(value, str) else value
