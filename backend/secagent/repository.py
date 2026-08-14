import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from secagent.db_models import (
    ApprovalRow,
    AuditEventRow,
    EvidenceRow,
    ModelCallRow,
    RefreshSessionRow,
    ReportRow,
    TaskRow,
    TaskStepRow,
    ToolCallRow,
    UserRow,
)
from secagent.api.errors import ApprovalExpired, ForbiddenResource
from secagent.domain import (
    PlanStep,
    TaskCreate,
    TaskRead,
    TaskScene,
    TaskStatus,
    UserRole,
)
from secagent.services.audit import AuditService
from secagent.security.redaction import scrub_approval_reason

if TYPE_CHECKING:
    from secagent.auth.dependencies import AuthenticatedUser


class AuthRepository:
    """SQLAlchemy persistence boundary for local users and refresh sessions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_user_by_username(self, username: str) -> UserRow | None:
        return self.session.scalar(select(UserRow).where(UserRow.username == username))

    def get_user(self, user_id: str) -> UserRow | None:
        return self.session.get(UserRow, user_id)

    def get_user_for_update(self, user_id: str) -> UserRow | None:
        return self.session.scalar(
            select(UserRow).where(UserRow.id == user_id).with_for_update()
        )

    def add_user(self, username: str, password_hash: str, role: str) -> UserRow:
        row = UserRow(username=username, password_hash=password_hash, role=role)
        self.session.add(row)
        self.session.flush()
        return row

    def list_users(self) -> list[UserRow]:
        return list(
            self.session.scalars(select(UserRow).order_by(UserRow.created_at)).all()
        )

    def lock_users_for_team_limit(self) -> None:
        self.session.scalars(
            select(UserRow).order_by(UserRow.id).with_for_update()
        ).all()

    def count_users(self) -> int:
        return self.session.scalar(select(func.count()).select_from(UserRow)) or 0

    def active_admins_for_update(self) -> list[UserRow]:
        return list(
            self.session.scalars(
                select(UserRow)
                .where(
                    UserRow.role == UserRole.ADMIN.value,
                    UserRow.is_active.is_(True),
                )
                .order_by(UserRow.id)
                .with_for_update()
            ).all()
        )

    def revoke_all_active_sessions(self, user_id: str, now: datetime) -> int:
        result = self.session.execute(
            update(RefreshSessionRow)
            .where(
                RefreshSessionRow.user_id == user_id,
                RefreshSessionRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        return result.rowcount

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
        user_id = self.session.scalar(
            select(RefreshSessionRow.user_id).where(
                RefreshSessionRow.token_hash == token_hash
            )
        )
        if user_id is None:
            return None
        user = self.get_user_for_update(user_id)
        if user is None or not user.is_active:
            return None

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

        replacement = self.add_refresh_session(
            user.id, replacement_hash, replacement_expires_at
        )
        previous = self.session.get(RefreshSessionRow, claimed.id)
        if previous is None:
            return None
        previous.replaced_by_id = replacement.id
        self.session.flush()
        return replacement, user

    def revoke_refresh_lineage(self, token_hash: str, now: datetime) -> bool:
        """Revoke a refresh token and every committed replacement atomically.

        Updating even an already-revoked row acquires the same database row lock
        used by rotation. The winner therefore establishes the order; logout
        either revokes before rotation can claim the row, or observes and follows
        the replacement committed by rotation.
        """
        revoked_at = case(
            (RefreshSessionRow.revoked_at.is_(None), now),
            else_=RefreshSessionRow.revoked_at,
        )
        current = self.session.execute(
            update(RefreshSessionRow)
            .where(RefreshSessionRow.token_hash == token_hash)
            .values(revoked_at=revoked_at)
            .returning(RefreshSessionRow.id, RefreshSessionRow.replaced_by_id)
        ).one_or_none()
        found = current is not None
        visited: set[str] = set()

        while current is not None and current.replaced_by_id is not None:
            if current.id in visited:
                break
            visited.add(current.id)
            current = self.session.execute(
                update(RefreshSessionRow)
                .where(RefreshSessionRow.id == current.replaced_by_id)
                .values(revoked_at=revoked_at)
                .returning(RefreshSessionRow.id, RefreshSessionRow.replaced_by_id)
            ).one_or_none()
        return found


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_task(
        self,
        payload: TaskCreate,
        owner_id: str | None = None,
        *,
        commit: bool = True,
    ) -> TaskRead:
        row = TaskRow(owner_id=owner_id, **payload.model_dump(mode="json"))
        self.session.add(row)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return self._read(row)

    def rollback(self) -> None:
        self.session.rollback()

    def get_task(self, task_id: str) -> TaskRead | None:
        row = self.session.get(TaskRow, task_id)
        return self._read(row) if row else None

    def list_tasks(self) -> list[TaskRead]:
        rows = self.session.scalars(
            select(TaskRow).order_by(TaskRow.created_at.desc())
        ).all()
        return [self._read(row) for row in rows]

    def get_authorized(
        self, task_id: str, actor: "AuthenticatedUser"
    ) -> TaskRead | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        if not can_access_task(actor, task.owner_id):
            AuditService(self.session).record(
                actor.id,
                "task.access_denied",
                "task",
                task_id,
                "denied",
                {"actor_role": actor.role.value},
            )
            self.session.commit()
            raise ForbiddenResource("task")
        return task

    def list_authorized(self, actor: "AuthenticatedUser") -> list[TaskRead]:
        query = select(TaskRow)
        if actor.role is not UserRole.ADMIN:
            query = query.where(TaskRow.owner_id == actor.id)
        rows = self.session.scalars(query.order_by(TaskRow.created_at.desc())).all()
        return [self._read(row) for row in rows]

    def record_audit(
        self,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEventRow:
        row = AuditService(self.session).record(
            actor_id, action, resource_type, resource_id, outcome, details
        )
        self.session.commit()
        return row

    def latest_audit_event(self, action: str) -> AuditEventRow | None:
        return self.session.scalar(
            select(AuditEventRow)
            .where(AuditEventRow.action == action)
            .order_by(AuditEventRow.id.desc())
        )

    def list_audit_events(self, *, limit: int = 100) -> list[AuditEventRow]:
        return list(
            self.session.scalars(
                select(AuditEventRow)
                .order_by(AuditEventRow.id.desc())
                .limit(limit)
            ).all()
        )

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        is_demo: bool | None = None,
        commit: bool = True,
    ) -> TaskRead:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        row.status = status.value
        if is_demo is not None:
            row.is_demo = is_demo
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return self._read(row)

    def transition_task_status(
        self,
        task_id: str,
        current: TaskStatus,
        target: TaskStatus,
        *,
        commit: bool = True,
    ) -> TaskRead:
        claimed = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                TaskRow.status == current.value,
            )
            .values(status=target.value)
            .returning(TaskRow.id)
        ).scalar_one_or_none()
        if claimed is None:
            raise ValueError("task state changed concurrently")
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        row = self.session.get(TaskRow, claimed)
        if row is None:
            raise KeyError(task_id)
        self.session.refresh(row)
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
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        self.session.add(row)
        self.session.commit()
        return row.id

    def decide_latest_approval(
        self,
        task_id: str,
        *,
        approved: bool,
        reason: str,
        decided_by: str | None = None,
        commit: bool = True,
    ) -> ApprovalRow:
        safe_reason = scrub_approval_reason(reason)
        row = self.session.scalar(
            select(ApprovalRow)
            .where(
                ApprovalRow.task_id == task_id,
                ApprovalRow.status == "pending",
            )
            .order_by(ApprovalRow.created_at.desc())
        )
        if row is None:
            raise ValueError("pending approval not found")
        now = datetime.now(timezone.utc)
        legacy_cutoff = now - timedelta(hours=24)
        claimed = self.session.execute(
            update(ApprovalRow)
            .where(
                ApprovalRow.id == row.id,
                ApprovalRow.status == "pending",
                or_(
                    ApprovalRow.expires_at > now,
                    and_(
                        ApprovalRow.expires_at.is_(None),
                        ApprovalRow.created_at > legacy_cutoff,
                    ),
                ),
            )
            .values(
                status="approved" if approved else "rejected",
                reason=safe_reason,
                decided_by=decided_by,
                decided_at=now,
            )
            .returning(ApprovalRow.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if claimed is None:
            expired = self.session.execute(
                update(ApprovalRow)
                .where(
                    ApprovalRow.id == row.id,
                    ApprovalRow.status == "pending",
                    or_(
                        ApprovalRow.expires_at <= now,
                        and_(
                            ApprovalRow.expires_at.is_(None),
                            ApprovalRow.created_at <= legacy_cutoff,
                        ),
                    ),
                )
                .values(status="expired")
                .returning(ApprovalRow.id)
                .execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            if expired is not None:
                if commit:
                    self.session.commit()
                else:
                    self.session.flush()
                raise ApprovalExpired(row.id)
            raise ValueError("pending approval not found")
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        decided = self.session.get(ApprovalRow, claimed)
        if decided is None:
            raise ValueError("pending approval not found")
        self.session.refresh(decided)
        return decided

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


def can_access_task(actor: "AuthenticatedUser", owner_id: str | None) -> bool:
    return actor.role is UserRole.ADMIN or actor.id == owner_id
