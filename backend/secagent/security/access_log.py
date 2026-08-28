from __future__ import annotations

import logging
from urllib.parse import unquote_plus


TICKET_REDACTION_MARKER = "[REDACTED]"


def _redact_query(path: str) -> str:
    if "?" not in path:
        return path
    base, query = path.split("?", 1)
    parts: list[str] = []
    for parameter in query.split("&"):
        name, separator, value = parameter.partition("=")
        try:
            is_ticket = unquote_plus(name) == "ticket"
        except (UnicodeDecodeError, ValueError):
            is_ticket = False
        if is_ticket:
            del value
            parts.append(f"{name}{separator or '='}{TICKET_REDACTION_MARKER}")
        else:
            parts.append(parameter)
    return f"{base}?{'&'.join(parts)}"


class TicketQueryRedactionFilter(logging.Filter):
    """Redact public event-stream tickets before access-log formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str):
                args[2] = _redact_query(args[2])
                record.args = tuple(args)
        if isinstance(record.msg, str) and "?" in record.msg:
            record.msg = _redact_query(record.msg)
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, TicketQueryRedactionFilter) for item in logger.filters):
        logger.addFilter(TicketQueryRedactionFilter())
    for handler in logger.handlers:
        if not any(
            isinstance(item, TicketQueryRedactionFilter) for item in handler.filters
        ):
            handler.addFilter(TicketQueryRedactionFilter())
