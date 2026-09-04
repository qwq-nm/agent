import json
from typing import Any

from secagent.providers.base import ProviderErrorCode
from secagent.security.redaction import redact_text


MAX_REPAIR_CONTENT_CHARS = 8_192
MAX_REPAIR_ERRORS = 20
MAX_REPAIR_ERROR_CHARS = 256
MAX_REPAIR_SCHEMA_CHARS = 16_384


class OutputValidationError(ValueError):
    def __init__(self, code: ProviderErrorCode, errors: list[str]) -> None:
        super().__init__(code.value)
        self.code = code
        self.errors = errors


def validate_output(content: str, schema: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OutputValidationError(
            ProviderErrorCode.INVALID_JSON, ["$: invalid JSON"]
        ) from exc
    if not isinstance(value, dict):
        raise OutputValidationError(
            ProviderErrorCode.INVALID_SCHEMA, ["$: expected object"]
        )
    errors: list[str] = []
    _validate(value, schema, schema, "$", errors)
    if errors:
        raise OutputValidationError(
            ProviderErrorCode.INVALID_SCHEMA, errors[:MAX_REPAIR_ERRORS]
        )
    return value


def repair_context(
    invalid_json: str,
    validation_errors: list[str],
    target_schema: dict[str, Any],
) -> dict[str, Any]:
    """Return the only data allowed to cross the repair-call boundary."""
    safe_content = redact_text(
        invalid_json[:MAX_REPAIR_CONTENT_CHARS], include_generic_key=True
    )[:MAX_REPAIR_CONTENT_CHARS]
    safe_errors = [
        redact_text(error, include_generic_key=True)[:MAX_REPAIR_ERROR_CHARS]
        for error in validation_errors[:MAX_REPAIR_ERRORS]
    ]
    return {
        "invalid_json": safe_content,
        "validation_errors": safe_errors,
        "target_schema": _bounded_schema(target_schema),
    }


def _bounded_schema(schema: dict[str, Any]) -> dict[str, Any]:
    safe = _sanitize_schema(schema)
    if not isinstance(safe, dict):
        return {"type": "object"}
    serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= MAX_REPAIR_SCHEMA_CHARS:
        return safe
    properties = safe.get("properties", {})
    property_items = properties.items() if isinstance(properties, dict) else ()
    compact_properties = {
        str(name)[:128]: _compact_rule(rule)
        for name, rule in list(property_items)[:64]
    }
    compact = {
        "title": str(safe.get("title", "response"))[:128],
        "type": "object",
        "required": [str(item)[:128] for item in safe.get("required", [])[:64]],
        "properties": compact_properties,
    }
    serialized = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= MAX_REPAIR_SCHEMA_CHARS:
        return compact
    return {"type": "object", "required": compact["required"][:32]}


def _sanitize_schema(value: Any, depth: int = 0) -> Any:
    if depth >= 16:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key)[:128]: _sanitize_schema(item, depth + 1)
            for key, item in list(value.items())[:128]
            if key not in {"description", "examples", "example", "default", "$comment"}
        }
    if isinstance(value, list):
        return [_sanitize_schema(item, depth + 1) for item in value[:128]]
    if isinstance(value, str):
        return redact_text(value, include_generic_key=True)[:1024]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[UNSUPPORTED]"


def _compact_rule(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, dict):
        return {}
    return {
        key: rule[key]
        for key in ("type", "enum", "const")
        if key in rule
    }


def _validate(
    value: Any,
    rule: dict[str, Any],
    root: dict[str, Any],
    path: str,
    errors: list[str],
) -> None:
    if len(errors) >= MAX_REPAIR_ERRORS:
        return
    resolved = _resolve(rule, root)
    union = resolved.get("anyOf") or resolved.get("oneOf")
    if union is not None:
        if not any(_is_valid(value, item, root) for item in union):
            errors.append(f"{path}: no allowed schema matched")
        return
    if "enum" in resolved and value not in resolved["enum"]:
        errors.append(f"{path}: value is not allowed")
        return
    if "const" in resolved and value != resolved["const"]:
        errors.append(f"{path}: value does not match constant")
        return
    expected = resolved.get("type")
    if expected is not None and not _matches_type(value, expected):
        errors.append(f"{path}: expected {expected}")
        return
    if isinstance(value, dict):
        properties = resolved.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        for field in resolved.get("required", []):
            if field not in value:
                errors.append(f"{path}.{field}: required")
        if resolved.get("additionalProperties") is False:
            for field in value.keys() - properties.keys():
                errors.append(f"{path}.{field}: additional property")
        for field, child in properties.items():
            if field in value and isinstance(child, dict):
                _validate(value[field], child, root, f"{path}.{field}", errors)
    elif isinstance(value, list) and isinstance(resolved.get("items"), dict):
        for index, item in enumerate(value):
            _validate(item, resolved["items"], root, f"{path}[{index}]", errors)
    elif isinstance(value, str):
        if len(value) < resolved.get("minLength", 0):
            errors.append(f"{path}: too short")
        if "maxLength" in resolved and len(value) > resolved["maxLength"]:
            errors.append(f"{path}: too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in resolved and value < resolved["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in resolved and value > resolved["maximum"]:
            errors.append(f"{path}: above maximum")


def _resolve(rule: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = rule.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return rule
    current: Any = root
    for part in reference[2:].split("/"):
        if not isinstance(current, dict):
            return rule
        current = current.get(part.replace("~1", "/").replace("~0", "~"))
    return current if isinstance(current, dict) else rule


def _is_valid(value: Any, rule: dict[str, Any], root: dict[str, Any]) -> bool:
    errors: list[str] = []
    _validate(value, rule, root, "$", errors)
    return not errors


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return checks.get(expected, lambda _: False)(value)
