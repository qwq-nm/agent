import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from secagent.db_models import (
    ApprovalRow,
    EvidenceRow,
    ModelCallRow,
    RefreshSessionRow,
    ReportRow,
    TaskRow,
    TaskStepRow,
    ToolCallRow,
    UserRow,
)
from secagent.domain import PlanStep, TaskCreate, TaskRead, TaskScene, TaskStatus


class AuthRepository:
    """SQLAlchemy persistence boundary for local users and refresh sessions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_user_by_username(self, username: str) -> UserRow | None:
        return self.session.scalar(select(UserRow).where(UserRow.username == username))

    def get_user(self, user_id: str) -> UserRow | None:
        return self.session.get(UserRow, user_id)

    def add_user(self, username: str, password_hash: str, role: str) -> UserRow:
        row = UserRow(username=username, password_hash=password_hash, role=role)
        self.session.add(row)
        self.session.flush()
        return row

    def add_refresh_session(
        self, user_id: str, token_hash: str, expires_at: datetime
    ) -> RefreshSessionRow:
        row = RefreshSessionRow(
            user_id=user_id, token_hash=token_hash, expires_at=expires_at
        )
        self.session.add(row)
        self.session.flush()
        return row

    def rotate_refresh_session(
        self,
        token_hash: str,
        replacement_hash: str,
        replacement_expires_at: datetime,
        now: datetime,
    ) -> tuple[RefreshSessionRow, UserRow] | None:
        claimed = self.session.execute(
            update(RefreshSessionRow)
            .where(
                RefreshSessionRow.token_hash == token_hash,
                RefreshSessionRow.revoked_at.is_(None),
                RefreshSessionRow.expires_at > now,
            )
            .values(revoked_at=now)
            .returning(RefreshSessionRow.id, RefreshSessionRow.user_id)
        ).one_or_none()
        if claimed is None:
            return None

        user = self.get_user(claimed.user_id)
        if user is None or not user.is_active:
            return None

        replacement = self.add_refresh_session(
            user.id, replacement_hash, replacement_expires_at
        )
        previous = self.session.get(RefreshSessionRow, claimed.id)
        if previous is None:
            return None
        previous.replaced_by_id = replacement.id
        self.session.flush()
        return replacement, user

    def revoke_refresh_session(self, token_hash: str, now: datetime) -> bool:
        result = self.session.execute(
            update(RefreshSessionRow)
            .where(
                RefreshSessionRow.token_hash == token_hash,
                RefreshSessionRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        return result.rowcount == 1


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

    def delete_task(self, task_id: str) -> None:
        row = self.session.get(TaskRow, task_id)
        if row is not None:
            self.session.delete(row)
            self.session.commit()

    def add_approval(
        self,
        task_id: str,
        *,
        step_id: str,
        tool_name: str,
        risk_level: str,
        params_summary: str,
    ) -> str:
        row = ApprovalRow(
            task_id=task_id,
            step_id=step_id,
            tool_name=tool_name,
            risk_level=risk_level,
            params_summary=params_summary,
        )
        self.session.add(row)
        self.session.commit()
        return row.id

    def decide_latest_approval(
        self, task_id: str, *, approved: bool, reason: str
    ) -> ApprovalRow:
        row = self.session.scalar(
            select(ApprovalRow)
            .where(
                ApprovalRow.task_id == task_id,
                ApprovalRow.status == "pending",
            )
            .order_by(ApprovalRow.created_at.desc())
        )
        if row is None:
            raise KeyError("pending approval not found")
        row.status = "approved" if approved else "rejected"
        row.reason = reason
        row.decided_at = datetime.now(timezone.utc)
        self.session.commit()
        return row

    def is_tool_approved(self, task_id: str, tool_name: str) -> bool:
        row = self.session.scalar(
            select(ApprovalRow).where(
                ApprovalRow.task_id == task_id,
                ApprovalRow.tool_name == tool_name,
                ApprovalRow.status == "approved",
            )
        )
        return row is not None

    def add_step(self, task_id: str, step_index: int, step: PlanStep) -> str:
        existing = self.session.scalar(
            select(TaskStepRow).where(
                TaskStepRow.task_id == task_id,
                TaskStepRow.step_index == step_index,
            )
        )
        if existing is not None:
            return existing.id

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
            "approvals": rows(ApprovalRow),
        }

    @staticmethod
    def _read(row: TaskRow) -> TaskRead:
        return TaskRead.model_validate(row, from_attributes=True)
