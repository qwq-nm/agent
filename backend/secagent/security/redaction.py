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
LABELED_SECRET_PATTERN = re.compile(
    r"(?P<label>\b(?:authorization|password|passwd|token|access[_-]?token|"
    r"refresh[_-]?token|cookie|api[\s_-]*key|secret)\b\s*[:=]\s*)"
    r'''(?P<secret>"[^"]*"|'[^']*'|(?:Bearer\s+)?[^\s,;]+)''',
    re.IGNORECASE,
)
GENERIC_KEY_SECRET_PATTERN = re.compile(
    r'''(?P<label>\bkey\b\s*[:=]\s*)(?P<secret>"[^"]*"|'[^']*'|[^\s,;]+)''',
    re.IGNORECASE,
)
AUDIT_ONLY_SENSITIVE_KEYS = {
    "body",
    "client_ip",
    "exception",
    "identifier",
    "ip",
    "key",
    "response_body",
}
APPROVAL_REASON_MAX_LENGTH = 1000


def redact_text(value: str, *, include_generic_key: bool = False) -> str:
    redacted = LABELED_SECRET_PATTERN.sub(
        lambda match: f'{match.group("label")}***REDACTED***', value
    )
    if include_generic_key:
        redacted = GENERIC_KEY_SECRET_PATTERN.sub(
            lambda match: f'{match.group("label")}***REDACTED***', redacted
        )
    return SECRET_PATTERN.sub("***REDACTED***", redacted)


def scrub_approval_reason(value: str) -> str:
    """Keep useful operator context while bounding and removing credential text."""
    bounded = value[:APPROVAL_REASON_MAX_LENGTH]
    return redact_text(bounded, include_generic_key=True)[:APPROVAL_REASON_MAX_LENGTH]


def _normalized_key(key: str) -> tuple[str, str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized, normalized.replace("_", "")


def _contains_sensitive_key(normalized_key: str, compact_key: str) -> bool:
    return any(
        sensitive in normalized_key or sensitive.replace("_", "") in compact_key
        for sensitive in SENSITIVE_KEYS
    )


def redact_mapping(value: Any, key: str = "") -> Any:
    normalized_key, compact_key = _normalized_key(key)
    if _contains_sensitive_key(normalized_key, compact_key):
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


def redact_audit_details(value: Any, key: str = "") -> Any:
    """Redact untrusted audit details using a stricter persistence policy."""
    normalized_key, compact_key = _normalized_key(key)
    strict_compact_keys = {
        sensitive.replace("_", "") for sensitive in AUDIT_ONLY_SENSITIVE_KEYS
    }
    if (
        _contains_sensitive_key(normalized_key, compact_key)
        or normalized_key in AUDIT_ONLY_SENSITIVE_KEYS
        or compact_key in strict_compact_keys
    ):
        return "***REDACTED***"
    if isinstance(value, Mapping):
        return {
            item_key: redact_audit_details(item, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_audit_details(item, key) for item in value]
    if isinstance(value, str):
        return redact_text(value, include_generic_key=True)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[UNSUPPORTED]"
