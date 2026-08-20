import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from secagent.db_models import TaskEventRow
from secagent.security.redaction import redact_mapping

MAX_EVENT_PAYLOAD_BYTES = 16 * 1024


class TaskEventService:
    """Durable, ordered task events backed by the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        task_id: str,
        event_type: str,
        payload: Any,
        *,
        commit: bool = True,
    ) -> int:
        payload_json = json.dumps(
            redact_mapping(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload_json.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError("task event payload exceeds 16 KiB")
        row = TaskEventRow(
            task_id=task_id,
            event_type=event_type,
            payload_json=payload_json,
        )
        self.session.add(row)
        self.session.flush()
        if commit:
            self.session.commit()
        return row.id

    def after(self, task_id: str, event_id: int) -> list[TaskEventRow]:
        return list(
            self.session.scalars(
                select(TaskEventRow)
                .where(
                    TaskEventRow.task_id == task_id,
                    TaskEventRow.id > event_id,
                )
                .order_by(TaskEventRow.id)
            ).all()
        )
