import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from secagent.db_models import (
    EvidenceRow,
    ModelCallRow,
    ReportRow,
    TaskRow,
    TaskStepRow,
    ToolCallRow,
)
from secagent.domain import PlanStep, TaskCreate, TaskRead, TaskScene, TaskStatus


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_task(self, payload: TaskCreate) -> TaskRead:
        row = TaskRow(**payload.model_dump(mode="json"))
        self.session.add(row)
        self.session.commit()
        return self._read(row)

    def get_task(self, task_id: str) -> TaskRead | None:
        row = self.session.get(TaskRow, task_id)
        return self._read(row) if row else None

    def list_tasks(self) -> list[TaskRead]:
        rows = self.session.scalars(
            select(TaskRow).order_by(TaskRow.created_at.desc())
        ).all()
        return [self._read(row) for row in rows]

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        is_demo: bool | None = None,
    ) -> TaskRead:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        row.status = status.value
        if is_demo is not None:
            row.is_demo = is_demo
        self.session.commit()
        return self._read(row)

    def set_task_scene(self, task_id: str, scene: TaskScene) -> TaskRead:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        row.scene = scene.value
        self.session.commit()
        return self._read(row)

    def add_step(self, task_id: str, step_index: int, step: PlanStep) -> str:
        row = TaskStepRow(
            task_id=task_id,
            step_index=step_index,
            name=step.name,
            purpose=step.purpose,
            tool_name=step.tool_name,
            params_json=json.dumps(step.params, ensure_ascii=False),
            risk_level=step.risk_level.value,
            need_human_confirm=step.need_human_confirm,
        )
        self.session.add(row)
        self.session.commit()
        return row.id

    def update_step(self, step_id: str, status: str, **values: Any) -> None:
        row = self.session.get(TaskStepRow, step_id)
        if row is None:
            raise KeyError(step_id)
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        for key in {"model_provider", "route_reason"} & values.keys():
            setattr(row, key, values[key])
        self.session.commit()

    def add_model_call(self, **values: Any) -> str:
        row = ModelCallRow(**values)
        self.session.add(row)
        self.session.commit()
        return row.id

    def add_tool_call(self, **values: Any) -> str:
        row = ToolCallRow(**values)
        self.session.add(row)
        self.session.commit()
        return row.id

    def add_evidence(self, **values: Any) -> str:
        row = EvidenceRow(**values)
        self.session.add(row)
        self.session.commit()
        return row.id

    def save_report(self, task_id: str, content: str, *, is_demo: bool) -> str:
        row = self.session.scalar(select(ReportRow).where(ReportRow.task_id == task_id))
        if row is None:
            row = ReportRow(task_id=task_id, content=content, is_demo=is_demo)
            self.session.add(row)
        else:
            row.content = content
            row.is_demo = is_demo
        self.session.commit()
        return row.id

    def recover_interrupted_tasks(self) -> int:
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.status == TaskStatus.RUNNING.value)
            .values(status=TaskStatus.FAILED_RETRYABLE.value)
        )
        self.session.commit()
        return result.rowcount

    def ledger_rows(self, task_id: str) -> dict[str, list[Any]]:
        def rows(model):
            return list(
                self.session.scalars(
                    select(model)
                    .where(model.task_id == task_id)
                    .order_by(model.created_at.asc())
                ).all()
            )

        return {
            "steps": rows(TaskStepRow),
            "model_calls": rows(ModelCallRow),
            "tool_calls": rows(ToolCallRow),
            "evidences": rows(EvidenceRow),
            "reports": rows(ReportRow),
        }

    @staticmethod
    def _read(row: TaskRow) -> TaskRead:
        return TaskRead.model_validate(row, from_attributes=True)
