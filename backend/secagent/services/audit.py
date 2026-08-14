import json
from typing import Any

from sqlalchemy.orm import Session

from secagent.db_models import AuditEventRow
from secagent.security.redaction import redact_mapping, redact_text


class AuditService:
    """Append-only writer for security-relevant audit events."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        outcome: str,
        details: dict[str, Any] | None = None,
        *,
        ip_address: str | None = None,
    ) -> AuditEventRow:
        row = AuditEventRow(
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=_safe_identifier(resource_id, 255),
            outcome=outcome,
            ip_address=_safe_identifier(ip_address, 64),
            details_json=json.dumps(
                redact_mapping(details or {}), ensure_ascii=False
            ),
        )
        self.session.add(row)
        self.session.flush()
        return row


def _safe_identifier(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    redacted = redact_text(value)
    if len(redacted) <= max_length:
        return redacted
    return "[IDENTIFIER_TOO_LONG]"
