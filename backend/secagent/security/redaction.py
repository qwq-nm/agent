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
EXACT_SENSITIVE_KEYS = {"body", "request_body"}
SECRET_PATTERN = re.compile(
    r"(?:\b(?:sk|api)[-_][A-Za-z0-9_-]{4,}\b|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|"
    r"\bBearer\s+[A-Za-z0-9._~-]+)",
    re.IGNORECASE,
)
_OPTIONAL_LABEL_QUOTE = r'''(?:\\["']|["'])?'''
LABELED_SECRET_PREFIX_PATTERN = re.compile(
    rf"(?P<label>{_OPTIONAL_LABEL_QUOTE}\b(?P<name>authorization|password|passwd|"
    rf"token|access[_-]?token|refresh[_-]?token|cookie|api[\s_-]*key|secret|"
    rf"key)\b{_OPTIONAL_LABEL_QUOTE}\s*[:=]\s*)",
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


def redact_text(
    value: str,
    *,
    include_generic_key: bool = False,
    redact_cookie: bool = True,
) -> str:
    redacted = _scrub_labeled_secrets(
        value,
        include_generic_key=include_generic_key,
        redact_cookie=redact_cookie,
    )
    return SECRET_PATTERN.sub("***REDACTED***", redacted)


def _scrub_labeled_secrets(
    value: str, *, include_generic_key: bool, redact_cookie: bool
) -> str:
    """Scrub assignment values in one forward pass over each matched value."""
    parts: list[str] = []
    copied_through = 0
    search_from = 0
    while match := LABELED_SECRET_PREFIX_PATTERN.search(value, search_from):
        normalized_name = re.sub(r"[\s_-]+", "", match.group("name").lower())
        if normalized_name == "key" and not include_generic_key:
            search_from = match.end()
            continue
        if normalized_name == "cookie" and not redact_cookie:
            search_from = match.end()
            continue
        scanned = _scan_secret_value(value, match.end())
        if scanned is None:
            search_from = match.end()
            continue
        value_end, replacement, truncate_tail = scanned
        parts.append(value[copied_through : match.end()])
        parts.append(replacement)
        copied_through = value_end
        search_from = value_end
        if truncate_tail:
            copied_through = len(value)
            break
    parts.append(value[copied_through:])
    return "".join(parts)


def _scan_secret_value(
    value: str, start: int
) -> tuple[int, str, bool] | None:
    if start >= len(value) or value[start] in ",;}]\r\n":
        return None
    if value[start] == "\\" and start + 1 < len(value):
        quote = value[start + 1]
        if quote in {'"', "'"}:
            end = _find_quoted_end(value, start + 2, quote, escaped_wrapper=True)
            if end is None:
                return len(value), "***REDACTED***", True
            wrapper = f"\\{quote}"
            return end, f"{wrapper}***REDACTED***{wrapper}", False
    if value[start] in {'"', "'"}:
        quote = value[start]
        end = _find_quoted_end(value, start + 1, quote, escaped_wrapper=False)
        if end is None:
            return len(value), "***REDACTED***", True
        return end, f"{quote}***REDACTED***{quote}", False
    end = start
    while end < len(value) and value[end] not in ",;}]\r\n":
        end += 1
    return end, "***REDACTED***", False


def _find_quoted_end(
    value: str, start: int, quote: str, *, escaped_wrapper: bool
) -> int | None:
    backslash_run = 0
    for index in range(start, len(value)):
        character = value[index]
        if character == "\\":
            backslash_run += 1
            continue
        if character == quote:
            if escaped_wrapper:
                closes_value = backslash_run % 4 == 1
            else:
                closes_value = backslash_run % 2 == 0
            if closes_value:
                return index + 1
        backslash_run = 0
    return None


def scrub_approval_reason(value: str) -> str:
    """Keep useful operator context while bounding and removing credential text."""
    bounded = value[:APPROVAL_REASON_MAX_LENGTH]
    return redact_text(bounded, include_generic_key=True)[:APPROVAL_REASON_MAX_LENGTH]


def _normalized_key(key: str) -> tuple[str, str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized, normalized.replace("_", "")


def _contains_sensitive_key(normalized_key: str, compact_key: str) -> bool:
    return normalized_key in EXACT_SENSITIVE_KEYS or compact_key in {
        value.replace("_", "") for value in EXACT_SENSITIVE_KEYS
    } or any(
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
