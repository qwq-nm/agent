import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from secagent.db_models import (
    ApprovalRow,
    AuditEventRow,
    EvidenceRow,
    JobRunRow,
    ModelCallRow,
    RefreshSessionRow,
    ReportRow,
    TaskRow,
    TaskStepRow,
    ToolCallEvidenceRow,
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
from secagent.security.redaction import redact_mapping, scrub_approval_reason

if TYPE_CHECKING:
    from secagent.auth.dependencies import AuthenticatedUser


class StaleJobLease(RuntimeError):
    pass


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

    def require_job_fence(self, lease: Any, task_id: str) -> JobRunRow:
        now = datetime.now(timezone.utc)
        job = self.session.scalar(
            select(JobRunRow)
            .where(JobRunRow.id == lease.job_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        expires = job.lease_expires_at if job is not None else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if (
            job is None
            or job.task_id != task_id
            or job.worker_id != lease.worker_id
            or job.attempt != lease.attempt
            or job.status != "running"
            or expires is None
            or expires <= now
        ):
            self.session.rollback()
            raise StaleJobLease("job lease was lost")
        task = self.session.scalar(
            select(TaskRow)
            .where(TaskRow.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or task.status != TaskStatus.RUNNING.value:
            self.session.rollback()
            raise StaleJobLease("task execution was invalidated")
        return job

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

    def commit(self) -> None:
        self.session.commit()

    def get_task(self, task_id: str) -> TaskRead | None:
        row = self.session.get(TaskRow, task_id, populate_existing=True)
        return self._read(row) if row else None

    def configure_task_budget(
        self,
        task_id: str,
        *,
        max_model_calls: int,
        max_input_tokens: int,
        max_output_tokens: int,
        max_steps: int,
    ) -> None:
        values = (max_model_calls, max_input_tokens, max_output_tokens, max_steps)
        if any(value < 0 for value in values):
            raise ValueError("task budget limits must be non-negative")
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        (
            row.max_model_calls,
            row.max_input_tokens,
            row.max_output_tokens,
            row.max_steps,
        ) = values
        self.session.flush()

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
        *,
        commit: bool = True,
    ) -> AuditEventRow:
        row = AuditService(self.session).record(
            actor_id, action, resource_type, resource_id, outcome, details
        )
        if commit:
            self.session.commit()
        return row

    def get_job_run(self, command_id: str) -> JobRunRow | None:
        return self.session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == command_id)
        )

    def add_job_run(
        self, task_id: str, command_id: str, *, attempt: int = 1
    ) -> JobRunRow:
        if attempt < 1:
            raise ValueError("job attempt must be positive")
        row = JobRunRow(
            task_id=task_id,
            command_id=command_id,
            attempt=attempt,
            status="pending_publish",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def current_task_attempt(self, task_id: str) -> int:
        value = self.session.scalar(
            select(func.max(JobRunRow.attempt)).where(JobRunRow.task_id == task_id)
        )
        return int(value or 0)

    def task_runtime(self, task_id: str) -> dict[str, Any]:
        job = self.session.scalar(
            select(JobRunRow)
            .where(JobRunRow.task_id == task_id)
            .order_by(JobRunRow.created_at.desc(), JobRunRow.id.desc())
            .limit(1)
        )
        if job is None:
            return {
                "queue_position": None,
                "job_attempt": None,
                "worker_id": None,
                "worker_heartbeat_at": None,
            }
        queue_position = None
        queued_statuses = ("pending_publish", "publishing", "queued")
        if job.status in queued_statuses:
            queue_position = int(
                self.session.scalar(
                    select(func.count())
                    .select_from(JobRunRow)
                    .where(
                        JobRunRow.status.in_(queued_statuses),
                        JobRunRow.created_at <= job.created_at,
                    )
                )
                or 0
            )
        heartbeat = job.heartbeat_at
        if heartbeat is not None and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        return {
            "queue_position": queue_position,
            "job_attempt": job.attempt,
            "worker_id": job.worker_id,
            "worker_heartbeat_at": heartbeat.isoformat().replace("+00:00", "Z") if heartbeat else None,
        }

    def claim_job_republish(self, command_id: str) -> bool:
        claimed = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.command_id == command_id,
                JobRunRow.status == "enqueue_failed",
            )
            .values(status="pending_publish")
            .returning(JobRunRow.id)
        ).scalar_one_or_none()
        self.session.flush()
        return claimed is not None

    def begin_job_publish(self, command_id: str) -> str:
        claimed = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.command_id == command_id,
                JobRunRow.status.in_(("pending_publish", "publishing")),
            )
            .values(status="publishing")
            .returning(JobRunRow.id)
        ).scalar_one_or_none()
        if claimed is not None:
            return "claimed"
        self.session.expire_all()
        row = self.get_job_run(command_id)
        return row.status if row is not None else "missing"

    def mark_job_enqueued(self, command_id: str, broker_id: str) -> None:
        self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.command_id == command_id,
                JobRunRow.status == "publishing",
            )
            .values(status="queued", broker_id=broker_id)
        )
        self.session.commit()

    def mark_job_enqueue_failed(self, task_id: str, command_id: str) -> None:
        claimed = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.command_id == command_id,
                JobRunRow.status == "publishing",
            )
            .values(status="enqueue_failed")
            .returning(JobRunRow.id)
        ).scalar_one_or_none()
        if claimed is None:
            return
        self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                TaskRow.status == TaskStatus.QUEUED.value,
            )
            .values(
                status=TaskStatus.FAILED_RETRYABLE.value,
                status_version=TaskRow.status_version + 1,
            )
        )

    def invalidate_jobs_for_transition(self, task_id: str, action: str) -> None:
        self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.task_id == task_id,
                JobRunRow.status.in_(
                    ("pending_publish", "publishing", "queued", "enqueue_failed")
                ),
            )
            .values(
                status="cancelled",
                finished_at=datetime.now(timezone.utc),
            )
        )
        requested_status = (
            "pause_requested" if action == "pause" else "cancel_requested"
        )
        running_statuses = (
            ("running",)
            if action == "pause"
            else ("running", "pause_requested")
        )
        self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.task_id == task_id,
                JobRunRow.status.in_(running_statuses),
            )
            .values(status=requested_status)
        )

    def has_unsettled_execution(self, task_id: str) -> bool:
        row = self.session.scalar(
            select(JobRunRow)
            .where(
                JobRunRow.task_id == task_id,
                JobRunRow.status.in_(
                    ("running", "pause_requested", "cancel_requested")
                ),
            )
            .order_by(JobRunRow.created_at)
            .with_for_update()
        )
        return row is not None

    def claim_job_execution(self, task_id: str, command_id: str) -> bool:
        job_claimed = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.task_id == task_id,
                JobRunRow.command_id == command_id,
                JobRunRow.status.in_(("publishing", "queued")),
            )
            .values(status="running", started_at=datetime.now(timezone.utc))
            .returning(JobRunRow.id)
        ).scalar_one_or_none()
        if job_claimed is None:
            self.session.rollback()
            return False
        task_claimed = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                TaskRow.status == TaskStatus.QUEUED.value,
            )
            .values(
                status=TaskStatus.RUNNING.value,
                status_version=TaskRow.status_version + 1,
            )
            .returning(TaskRow.id)
        ).scalar_one_or_none()
        if task_claimed is None:
            self.session.execute(
                update(JobRunRow)
                .where(
                    JobRunRow.id == job_claimed,
                    JobRunRow.status == "running",
                )
                .values(
                    status="cancelled",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            self.session.commit()
            return False
        self.session.commit()
        return True

    def finish_job_execution(self, command_id: str, status: str) -> str:
        row = self.session.scalar(
            select(JobRunRow)
            .where(JobRunRow.command_id == command_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            self.session.rollback()
            return "missing"
        task_target: TaskStatus | None = None
        if row.status == "pause_requested":
            row.status = "paused"
            task_target = TaskStatus.PAUSED
        elif row.status == "cancel_requested":
            row.status = "cancelled"
            task_target = TaskStatus.CANCELLED
        elif row.status == "running":
            row.status = status
        row.finished_at = datetime.now(timezone.utc)
        if task_target is not None:
            self.session.execute(
                update(TaskRow)
                .where(TaskRow.id == row.task_id)
                .values(
                    status=task_target.value,
                    status_version=TaskRow.status_version + 1,
                )
            )
        self.session.commit()
        return row.status

    def latest_audit_event(self, action: str) -> AuditEventRow | None:
        return self.session.scalar(
            select(AuditEventRow)
            .where(AuditEventRow.action == action)
            .order_by(AuditEventRow.id.desc())
        )

    def list_audit_events(
        self,
        *,
        limit: int = 100,
        before: int | None = None,
        actor_ids: set[str] | None = None,
        action: str | None = None,
        outcome: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> list[AuditEventRow]:
        query = select(AuditEventRow)
        if before is not None:
            query = query.where(AuditEventRow.id < before)
        if actor_ids is not None:
            if not actor_ids:
                return []
            query = query.where(AuditEventRow.actor_id.in_(actor_ids))
        if action:
            query = query.where(AuditEventRow.action == action)
        if outcome:
            query = query.where(AuditEventRow.outcome == outcome)
        if created_after is not None:
            query = query.where(AuditEventRow.created_at >= created_after)
        if created_before is not None:
            query = query.where(AuditEventRow.created_at <= created_before)
        return list(
            self.session.scalars(
                query.order_by(AuditEventRow.id.desc()).limit(limit)
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
        if row.status != status.value:
            row.status = status.value
            row.status_version += 1
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
            .values(
                status=target.value,
                status_version=TaskRow.status_version + 1,
            )
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

    def set_task_scene(
        self, task_id: str, scene: TaskScene, *, lease: Any | None = None
    ) -> TaskRead:
        if lease is not None:
            self.require_job_fence(lease, task_id)
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        row.scene = scene.value
        self.session.commit()
        return self._read(row)

    def load_orchestration_checkpoint(
        self, task_id: str, *, lease: Any, fingerprint: str
    ) -> dict[str, Any]:
        """Return only checkpoints belonging to the fenced logical attempt."""
        self.require_job_fence(lease, task_id)
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        try:
            checkpoint = json.loads(row.orchestration_json or "{}")
        except (TypeError, json.JSONDecodeError):
            self.session.commit()
            return {}
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("attempt") != lease.attempt
            or checkpoint.get("fingerprint") != fingerprint
            or not isinstance(checkpoint.get("stages", {}), dict)
        ):
            self.session.commit()
            return {}
        valid_stages: dict[str, Any] = {}
        for stage, value in checkpoint["stages"].items():
            if not isinstance(value, dict) or value.get("status") != "success":
                continue
            call_id = value.get("model_call_id")
            call = self.session.get(ModelCallRow, call_id) if call_id else None
            if (
                call is not None
                and call.task_id == task_id
                and call.stage == stage
                and call.status == "completed"
                and call.attempt == lease.attempt
            ):
                valid_stages[stage] = value
        checkpoint["stages"] = valid_stages
        self.session.commit()
        return checkpoint

    def save_orchestration_checkpoint(
        self,
        task_id: str,
        *,
        lease: Any,
        fingerprint: str,
        stage: str,
        data: dict[str, Any],
        model_call_id: str,
    ) -> None:
        self.require_job_fence(lease, task_id)
        row = self.session.get(TaskRow, task_id)
        if row is None:
            self.session.rollback()
            raise KeyError(task_id)
        try:
            checkpoint = json.loads(row.orchestration_json or "{}")
        except (TypeError, json.JSONDecodeError):
            checkpoint = {}
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("attempt") != lease.attempt
            or checkpoint.get("fingerprint") != fingerprint
        ):
            checkpoint = {
                "attempt": lease.attempt,
                "fingerprint": fingerprint,
                "stages": {},
            }
        stages = checkpoint.setdefault("stages", {})
        model_call = self.session.get(ModelCallRow, model_call_id)
        if (
            model_call is None
            or model_call.task_id != task_id
            or model_call.stage != stage
            or model_call.status != "completed"
            or model_call.attempt != lease.attempt
        ):
            self.session.rollback()
            raise ValueError("successful model call is required for checkpoint")
        stages[stage] = {
            "status": "success",
            "model_call_id": model_call_id,
            "data": data,
        }
        row.orchestration_json = json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.session.commit()

    def set_current_step_index(
        self, task_id: str, step_index: int, *, lease: Any
    ) -> None:
        self.require_job_fence(lease, task_id)
        row = self.session.get(TaskRow, task_id)
        if row is None:
            self.session.rollback()
            raise KeyError(task_id)
        row.current_step_index = step_index
        self.session.commit()

    def get_step(self, task_id: str, step_index: int) -> TaskStepRow | None:
        return self.session.scalar(
            select(TaskStepRow).where(
                TaskStepRow.task_id == task_id,
                TaskStepRow.step_index == step_index,
            )
        )

    def budget_state(
        self,
        task_id: str,
        *,
        lease: Any,
        timeout_seconds: int,
        now: datetime,
    ) -> dict[str, Any]:
        self.require_job_fence(lease, task_id)
        row = self.session.get(TaskRow, task_id)
        if row is None:
            self.session.rollback()
            raise KeyError(task_id)
        if row.budget_deadline_at is None:
            row.budget_deadline_at = now + timedelta(seconds=timeout_seconds)
            self.session.commit()
        deadline = row.budget_deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        usage = self.session.execute(
            select(
                func.count(ModelCallRow.id),
                func.coalesce(func.sum(ModelCallRow.prompt_tokens), 0),
                func.coalesce(func.sum(ModelCallRow.completion_tokens), 0),
            ).where(ModelCallRow.task_id == task_id)
        ).one()
        step_count = self.session.scalar(
            select(func.count(TaskStepRow.id)).where(TaskStepRow.task_id == task_id)
        )
        state = {
            "max_calls": row.max_model_calls,
            "max_input_tokens": row.max_input_tokens,
            "max_output_tokens": row.max_output_tokens,
            "max_steps": row.max_steps,
            "deadline": deadline,
            "calls": int(usage[0]),
            "input_tokens": int(usage[1]),
            "output_tokens": int(usage[2]),
            "steps": int(step_count or 0),
        }
        self.session.commit()
        return state

    def fail_budget_exhausted(
        self,
        task_id: str,
        *,
        lease: Any,
        dimension: str,
        active_step_id: str | None,
    ) -> None:
        if dimension not in {
            "model_calls",
            "input_tokens",
            "output_tokens",
            "steps",
            "deadline",
        }:
            raise ValueError("invalid budget dimension")
        job = self.require_job_fence(lease, task_id)
        task = self.session.get(TaskRow, task_id)
        if task is None:
            self.session.rollback()
            raise KeyError(task_id)
        if active_step_id is not None:
            step = self.session.scalar(
                select(TaskStepRow).where(
                    TaskStepRow.id == active_step_id,
                    TaskStepRow.task_id == task_id,
                )
            )
            if step is not None and step.status not in {"success", "failed"}:
                step.status = "failed"
                step.result_json = json.dumps(
                    {"error_code": "budget_exceeded", "dimension": dimension},
                    separators=(",", ":"),
                )
                step.updated_at = datetime.now(timezone.utc)
        task.status = TaskStatus.FAILED.value
        task.status_version += 1
        job.status = "failed"
        job.finished_at = datetime.now(timezone.utc)
        job.lease_expires_at = None
        from secagent.services.task_events import TaskEventService

        TaskEventService(self.session).append(
            task_id,
            "task.budget_exhausted",
            {"dimension": dimension},
            commit=False,
        )
        self.session.commit()

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
        lease: Any | None = None,
        commit: bool = True,
    ) -> str:
        if lease is not None:
            self.require_job_fence(lease, task_id)
        row = ApprovalRow(
            task_id=task_id,
            step_id=step_id,
            tool_name=tool_name,
            risk_level=risk_level,
            params_summary=params_summary,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        self.session.add(row)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
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

    def is_tool_approved(
        self, task_id: str, tool_name: str, *, step_id: str
    ) -> bool:
        row = self.session.scalar(
            select(ApprovalRow).where(
                ApprovalRow.task_id == task_id,
                ApprovalRow.step_id == step_id,
                ApprovalRow.tool_name == tool_name,
                ApprovalRow.status == "approved",
            )
        )
        return row is not None

    @staticmethod
    def step_idempotency_key(
        task_id: str, step_index: int, step: PlanStep
    ) -> str:
        canonical = json.dumps(
            {
                "task_id": task_id,
                "step_index": step_index,
                "step": step.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def completed_step_result(
        self, task_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = self.session.scalar(
            select(TaskStepRow).where(
                TaskStepRow.task_id == task_id,
                TaskStepRow.idempotency_key == idempotency_key,
                TaskStepRow.status == "success",
                TaskStepRow.result_json.is_not(None),
            )
        )
        if row is None or row.result_json is None:
            return None
        try:
            stored = json.loads(row.result_json)
        except (TypeError, json.JSONDecodeError):
            return None
        hashes = stored.get("evidence_hashes")
        result = stored.get("result")
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or not isinstance(hashes, list)
            or not hashes
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hashes
            )
        ):
            return None
        expected_hashes = set(hashes)
        calls = list(
            self.session.scalars(
                select(ToolCallRow).where(
                    ToolCallRow.task_id == task_id,
                    ToolCallRow.step_id == row.id,
                    ToolCallRow.status == "completed",
                    ToolCallRow.attempt == row.attempt,
                )
            ).all()
        )
        call_ids = [call.id for call in calls]
        associated_ids = set(
            self.session.scalars(
                select(ToolCallEvidenceRow.evidence_id).where(
                    ToolCallEvidenceRow.tool_call_id.in_(call_ids),
                    ToolCallEvidenceRow.step_attempt == row.attempt,
                )
            ).all()
        ) if call_ids else set()
        persisted_evidence = list(
            self.session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.task_id == task_id,
                    EvidenceRow.sha256.in_(expected_hashes),
                    or_(
                        EvidenceRow.id.in_(associated_ids),
                        EvidenceRow.tool_call_id.in_(call_ids),
                    ),
                )
            ).all()
        )
        verified_hashes = set()
        for evidence in persisted_evidence:
            legacy = hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()
            canonical = hashlib.sha256(
                json.dumps(
                    evidence.content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if evidence.file_ref is None and evidence.sha256 in {legacy, canonical}:
                verified_hashes.add(evidence.sha256)
        if verified_hashes != expected_hashes:
            return None
        return stored

    def add_step(
        self,
        task_id: str,
        step_index: int,
        step: PlanStep,
        *,
        idempotency_key: str | None = None,
        lease: Any | None = None,
    ) -> str:
        if lease is not None:
            self.require_job_fence(lease, task_id)
        key = idempotency_key or self.step_idempotency_key(
            task_id, step_index, step
        )
        existing = self.session.scalar(
            select(TaskStepRow).where(
                TaskStepRow.task_id == task_id,
                TaskStepRow.step_index == step_index,
            )
        )
        if existing is not None:
            if (
                existing.idempotency_key == key
                and self.completed_step_result(task_id, key) is not None
            ):
                return existing.id
            self.session.execute(
                update(ApprovalRow)
                .where(
                    ApprovalRow.task_id == task_id,
                    ApprovalRow.step_id == existing.id,
                    ApprovalRow.status.in_(("pending", "approved")),
                )
                .values(status="superseded")
            )
            existing.idempotency_key = key
            existing.attempt += 1
            existing.name = step.name
            existing.purpose = step.purpose
            existing.tool_name = step.tool_name
            existing.params_json = json.dumps(
                redact_mapping(step.params), ensure_ascii=False
            )
            existing.risk_level = step.risk_level.value
            existing.need_human_confirm = step.need_human_confirm
            existing.status = "pending"
            existing.result_json = None
            existing.updated_at = datetime.now(timezone.utc)
            self.session.commit()
            return existing.id

        row = TaskStepRow(
            task_id=task_id,
            step_index=step_index,
            idempotency_key=key,
            name=step.name,
            purpose=step.purpose,
            tool_name=step.tool_name,
            params_json=json.dumps(redact_mapping(step.params), ensure_ascii=False),
            risk_level=step.risk_level.value,
            need_human_confirm=step.need_human_confirm,
        )
        self.session.add(row)
        self.session.commit()
        return row.id

    def persist_step_result(
        self,
        lease: Any,
        *,
        step_id: str,
        tool_name: str,
        params: dict[str, Any],
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> str:
        self.require_job_fence(lease, lease.task_id)
        step = self.session.scalar(
            select(TaskStepRow)
            .where(TaskStepRow.id == step_id, TaskStepRow.task_id == lease.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if step is None:
            self.session.rollback()
            raise KeyError(step_id)
        params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        tool_call = None
        for candidate in self.session.scalars(
            select(ToolCallRow).where(
                ToolCallRow.task_id == lease.task_id,
                ToolCallRow.step_id == step_id,
                ToolCallRow.tool_name == tool_name,
                ToolCallRow.status == "completed",
                ToolCallRow.attempt == step.attempt,
            )
        ).all():
            try:
                same_params = json.loads(candidate.params_json) == params
                same_result = json.loads(candidate.result_json) == result
            except json.JSONDecodeError:
                continue
            if same_params and same_result:
                tool_call = candidate
                break
        if tool_call is None:
            tool_call = ToolCallRow(
                task_id=lease.task_id,
                step_id=step_id,
                tool_name=tool_name,
                params_json=params_json,
                result_json=result_json,
                status="completed",
                attempt=step.attempt,
            )
            self.session.add(tool_call)
            self.session.flush()

        evidence_hashes: list[str] = []
        for item in evidence:
            content = str(item["content"])
            digest = str(item.get("sha256", ""))
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                self.session.rollback()
                raise ValueError("invalid canonical evidence hash")
            evidence_hashes.append(digest)
            evidence_row = self.session.scalar(
                select(EvidenceRow).where(
                    EvidenceRow.task_id == lease.task_id,
                    EvidenceRow.sha256 == digest,
                    EvidenceRow.source == str(item["source"]),
                )
            )
            if evidence_row is None:
                evidence_row = EvidenceRow(
                    task_id=lease.task_id,
                    tool_call_id=tool_call.id,
                    evidence_type=str(item["evidence_type"]),
                    source=str(item["source"]),
                    content=content,
                    sha256=digest,
                    confidence=float(item["confidence"]),
                    file_ref=item.get("file_ref"),
                    metadata_json=json.dumps(item["metadata"], ensure_ascii=False),
                )
                self.session.add(evidence_row)
                self.session.flush()
            elif (
                evidence_row.content != content
                or evidence_row.file_ref != item.get("file_ref")
            ):
                self.session.rollback()
                raise ValueError("conflicting canonical evidence")
            binding = self.session.get(
                ToolCallEvidenceRow, (tool_call.id, evidence_row.id)
            )
            if binding is None:
                self.session.add(
                    ToolCallEvidenceRow(
                        tool_call_id=tool_call.id,
                        evidence_id=evidence_row.id,
                        step_attempt=step.attempt,
                    )
                )
        step.status = "success" if result.get("success") is True else "failed"
        step.result_json = json.dumps(
            {"result": result, "evidence_hashes": evidence_hashes},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        step.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return tool_call.id

    def update_step(self, step_id: str, status: str, **values: Any) -> None:
        row = self.session.get(TaskStepRow, step_id)
        if row is None:
            raise KeyError(step_id)
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        for key in {"model_provider", "route_reason", "result_json"} & values.keys():
            setattr(row, key, values[key])
        self.session.commit()

    def complete_step(
        self,
        step_id: str,
        result: dict[str, Any],
        evidence_hashes: list[str],
    ) -> None:
        row = self.session.get(TaskStepRow, step_id)
        if row is None:
            raise KeyError(step_id)
        row.status = "success"
        row.result_json = json.dumps(
            {"result": result, "evidence_hashes": evidence_hashes},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        row.updated_at = datetime.now(timezone.utc)
        self.session.commit()

    def add_model_call(self, *, lease: Any | None = None, **values: Any) -> str:
        if lease is not None:
            self.require_job_fence(lease, values["task_id"])
            supplied_attempt = values.get("attempt", lease.attempt)
            if supplied_attempt != lease.attempt:
                self.session.rollback()
                raise ValueError("model call attempt does not match lease")
            values["attempt"] = lease.attempt
        else:
            values.setdefault("attempt", 1)
        row = ModelCallRow(**values)
        self.session.add(row)
        self.session.commit()
        return row.id

    def add_tool_call(self, **values: Any) -> str:
        row = ToolCallRow(**values)
        self.session.add(row)
        self.session.commit()
        return row.id

    def add_evidence(self, *, lease: Any | None = None, **values: Any) -> str:
        if lease is not None:
            self.require_job_fence(lease, values["task_id"])
        row = EvidenceRow(**values)
        self.session.add(row)
        self.session.commit()
        return row.id

    def add_error_evidence(self, lease: Any, **values: Any) -> str:
        """Reuse a canonical runtime error without weakening its uniqueness."""
        values = dict(values)
        task_id = str(values["task_id"])
        source = str(values["source"])
        content = str(values["content"])
        digest = str(values["sha256"])
        values.update(
            task_id=task_id,
            source=source,
            content=content,
            sha256=digest,
        )
        self.require_job_fence(lease, task_id)
        if (
            values.get("evidence_type") != "runtime_error"
            or source != "agent_runner"
            or digest
            not in {
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                hashlib.sha256(
                    json.dumps(
                        content,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        ):
            self.session.rollback()
            raise ValueError("invalid canonical runtime error")

        dialect_name = self.session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = postgresql_insert(EvidenceRow).values(**values)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(EvidenceRow).values(**values)
        else:
            existing = self.session.scalar(
                select(EvidenceRow)
                .where(
                    EvidenceRow.task_id == task_id,
                    EvidenceRow.sha256 == digest,
                    EvidenceRow.source == source,
                )
                .with_for_update()
            )
            if existing is None:
                self.session.add(EvidenceRow(**values))
                self.session.flush()
        if dialect_name in {"postgresql", "sqlite"}:
            self.session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=["task_id", "sha256", "source"]
                )
            )

        row = self.session.scalar(
            select(EvidenceRow)
            .where(
                EvidenceRow.task_id == task_id,
                EvidenceRow.sha256 == digest,
                EvidenceRow.source == source,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.evidence_type != "runtime_error"
            or row.content != content
            or row.source != source
            or digest
            not in {
                hashlib.sha256(row.content.encode("utf-8")).hexdigest(),
                hashlib.sha256(
                    json.dumps(
                        row.content,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        ):
            self.session.rollback()
            raise ValueError("conflicting canonical runtime error")
        self.session.commit()
        return row.id

    def save_report(
        self,
        task_id: str,
        content: str,
        *,
        is_demo: bool,
        evidence_ids: list[str] | None = None,
        lease: Any | None = None,
    ) -> str:
        if lease is not None:
            self.require_job_fence(lease, task_id)
        task = self.session.get(TaskRow, task_id)
        if task is None:
            self.session.rollback()
            raise KeyError(task_id)
        cited_ids = list(evidence_ids or [])
        if len(cited_ids) != len(set(cited_ids)):
            self.session.rollback()
            raise ValueError("invalid evidence citation for current task")
        existing_ids = set(
            self.session.scalars(
                select(EvidenceRow.id).where(
                    EvidenceRow.task_id == task_id,
                    EvidenceRow.id.in_(cited_ids),
                )
            ).all()
        ) if cited_ids else set()
        if existing_ids != set(cited_ids):
            self.session.rollback()
            raise ValueError("invalid evidence citation for current task")
        task.is_demo = is_demo
        row = self.session.scalar(select(ReportRow).where(ReportRow.task_id == task_id))
        if row is None:
            row = ReportRow(
                task_id=task_id,
                content=content,
                evidence_ids_json=json.dumps(cited_ids, separators=(",", ":")),
                is_demo=is_demo,
            )
            self.session.add(row)
        else:
            row.content = content
            row.evidence_ids_json = json.dumps(cited_ids, separators=(",", ":"))
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
