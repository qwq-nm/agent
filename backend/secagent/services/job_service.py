from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update

from secagent.db_models import JobRunRow, TaskRow
from secagent.domain import TaskStatus
from secagent.queue.base import JobQueue
from secagent.repository import TaskRepository
from secagent.services.task_events import TaskEventService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive DateTime round trips to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class JobLease:
    job_run_id: str
    task_id: str
    worker_id: str
    attempt: int
    lease_expires_at: datetime


class JobService:
    def __init__(
        self,
        repository: TaskRepository,
        queue: JobQueue | None,
        *,
        lease_seconds: int = 90,
        max_auto_retries: int = 1,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.repository = repository
        self.session = repository.session
        self.queue = queue
        self.lease_seconds = lease_seconds
        self.max_auto_retries = max_auto_retries
        self.now = now

    def claim(
        self, task_id: str, command_id: str, worker_id: str
    ) -> JobLease | None:
        current = self.now()
        expires = current + timedelta(seconds=self.lease_seconds)
        job_id = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.task_id == task_id,
                JobRunRow.command_id == command_id,
                JobRunRow.status.in_(("publishing", "queued")),
            )
            .values(
                status="running",
                worker_id=worker_id,
                lease_expires_at=expires,
                heartbeat_at=current,
                started_at=current,
            )
            .returning(JobRunRow.id)
        ).scalar_one_or_none()
        if job_id is None:
            self.session.rollback()
            return None

        task = self.session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        if task is None:
            self.session.rollback()
            return None
        claimed_task = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                TaskRow.status == TaskStatus.QUEUED.value,
                TaskRow.status_version == task.status_version,
            )
            .values(
                status=TaskStatus.RUNNING.value,
                status_version=task.status_version + 1,
            )
            .returning(TaskRow.id)
        ).scalar_one_or_none()
        if claimed_task is None:
            self.session.execute(
                update(JobRunRow)
                .where(JobRunRow.id == job_id, JobRunRow.worker_id == worker_id)
                .values(status="cancelled", finished_at=current)
            )
            self.session.commit()
            self.session.expire_all()
            return None
        job = self.session.get(JobRunRow, job_id)
        TaskEventService(self.session).append(
            task_id,
            "task.running",
            {"attempt": job.attempt, "worker_id": worker_id},
            commit=False,
        )
        self.session.commit()
        return JobLease(job.id, task_id, worker_id, job.attempt, expires)

    def heartbeat(self, job_run_id: str, worker_id: str) -> JobLease | None:
        current = self.now()
        expires = current + timedelta(seconds=self.lease_seconds)
        row = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.id == job_run_id,
                JobRunRow.status == "running",
                JobRunRow.worker_id == worker_id,
                JobRunRow.lease_expires_at > current,
            )
            .values(heartbeat_at=current, lease_expires_at=expires)
            .returning(
                JobRunRow.task_id,
                JobRunRow.attempt,
            )
        ).one_or_none()
        if row is None:
            self.session.rollback()
            return None
        self.session.commit()
        return JobLease(job_run_id, row.task_id, worker_id, row.attempt, expires)

    def is_active(self, job_run_id: str, worker_id: str) -> bool:
        current = self.now()
        row = self.session.execute(
            select(
                JobRunRow.status,
                JobRunRow.worker_id,
                JobRunRow.lease_expires_at,
            ).where(JobRunRow.id == job_run_id)
        ).one_or_none()
        return bool(
            row is not None
            and row.status == "running"
            and row.worker_id == worker_id
            and row.lease_expires_at is not None
            and _as_utc(row.lease_expires_at) > current
        )

    def finish(self, job_run_id: str, worker_id: str, status: str) -> bool:
        if status not in {"completed", "failed"}:
            raise ValueError("invalid final job status")
        current = self.now()
        row = self.session.scalar(
            select(JobRunRow)
            .where(JobRunRow.id == job_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.worker_id != worker_id
            or row.status not in {"running", "pause_requested", "cancel_requested"}
            or row.lease_expires_at is None
            or _as_utc(row.lease_expires_at) <= current
        ):
            self.session.rollback()
            return False
        task_target: TaskStatus
        if row.status == "pause_requested":
            row.status = "paused"
            task_target = TaskStatus.PAUSED
        elif row.status == "cancel_requested":
            row.status = "cancelled"
            task_target = TaskStatus.CANCELLED
        else:
            row.status = status
            task_target = (
                TaskStatus.COMPLETED
                if status == "completed"
                else TaskStatus.FAILED_RETRYABLE
            )
        row.finished_at = current
        row.lease_expires_at = None
        task_changed = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == row.task_id,
                TaskRow.status == TaskStatus.RUNNING.value,
            )
            .values(
                status=task_target.value,
                status_version=TaskRow.status_version + 1,
            )
            .returning(TaskRow.id)
        ).scalar_one_or_none()
        TaskEventService(self.session).append(
            row.task_id,
            (
                f"task.{task_target.value}"
                if task_changed is not None
                else f"job.{row.status}"
            ),
            {"attempt": row.attempt},
            commit=False,
        )
        self.session.commit()
        return True

    def recover_expired(self) -> int:
        current = self.now()
        expired_ids = list(
            self.session.scalars(
                select(JobRunRow.id).where(
                    JobRunRow.status.in_(
                        ("running", "pause_requested", "cancel_requested")
                    ),
                    JobRunRow.lease_expires_at <= current,
                )
            ).all()
        )
        recovered = 0
        for job_id in expired_ids:
            row = self.session.scalar(
                select(JobRunRow)
                .where(JobRunRow.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                row is None
                or row.status
                not in {"running", "pause_requested", "cancel_requested"}
                or row.lease_expires_at is None
                or _as_utc(row.lease_expires_at) > current
            ):
                self.session.rollback()
                continue
            task = self.session.scalar(
                select(TaskRow).where(TaskRow.id == row.task_id).with_for_update()
            )
            if task is None:
                self.session.rollback()
                continue
            recovered += 1
            row.worker_id = None
            row.heartbeat_at = None
            row.lease_expires_at = None
            if row.status == "running" and task.status != TaskStatus.RUNNING.value:
                settled = {
                    TaskStatus.PAUSED.value: "paused",
                    TaskStatus.CANCELLED.value: "cancelled",
                    TaskStatus.COMPLETED.value: "completed",
                }.get(task.status, "failed")
                row.status = settled
                row.finished_at = current
                TaskEventService(self.session).append(
                    row.task_id,
                    f"job.{settled}",
                    {"attempt": row.attempt, "reason": "task_not_running"},
                    commit=False,
                )
                self.repository.record_audit(
                    None,
                    "task.recover",
                    "task",
                    row.task_id,
                    settled,
                    {"attempt": row.attempt, "reason": "task_not_running"},
                    commit=False,
                )
                self.session.commit()
                continue
            if row.status in {"pause_requested", "cancel_requested"}:
                settled = {
                    "pause_requested": "paused",
                    "cancel_requested": "cancelled",
                }[row.status]
                row.status = settled
                row.finished_at = current
                TaskEventService(self.session).append(
                    row.task_id,
                    f"job.{settled}",
                    {"attempt": row.attempt, "reason": "lease_expired"},
                    commit=False,
                )
                self.repository.record_audit(
                    None,
                    "task.recover",
                    "task",
                    row.task_id,
                    settled,
                    {"attempt": row.attempt},
                    commit=False,
                )
                self.session.commit()
                continue
            if row.attempt <= self.max_auto_retries:
                row.attempt += 1
                row.status = "queued"
                task.status = TaskStatus.QUEUED.value
                task.status_version += 1
                TaskEventService(self.session).append(
                    row.task_id,
                    "task.queued",
                    {"attempt": row.attempt, "reason": "lease_expired"},
                    commit=False,
                )
                self.repository.record_audit(
                    None,
                    "task.recover",
                    "task",
                    row.task_id,
                    "requeued",
                    {"attempt": row.attempt},
                    commit=False,
                )
                self.session.commit()
                if self.queue is not None:
                    try:
                        broker_id = self.queue.enqueue(row.task_id, row.command_id)
                    except Exception:
                        row.status = "enqueue_failed"
                        task.status = TaskStatus.FAILED_RETRYABLE.value
                        task.status_version += 1
                        TaskEventService(self.session).append(
                            row.task_id,
                            "task.failed_retryable",
                            {"attempt": row.attempt, "reason": "queue_unavailable"},
                            commit=False,
                        )
                        self.repository.record_audit(
                            None,
                            "task.recover",
                            "task",
                            row.task_id,
                            "failed_retryable",
                            {"attempt": row.attempt, "reason": "queue_unavailable"},
                            commit=False,
                        )
                        self.session.commit()
                    else:
                        row.broker_id = broker_id
                        self.session.commit()
            else:
                row.status = "failed"
                row.finished_at = current
                task.status = TaskStatus.FAILED.value
                task.status_version += 1
                TaskEventService(self.session).append(
                    row.task_id,
                    "task.failed",
                    {"attempt": row.attempt, "reason": "retry_budget_exhausted"},
                    commit=False,
                )
                self.repository.record_audit(
                    None,
                    "task.recover",
                    "task",
                    row.task_id,
                    "failed",
                    {"attempt": row.attempt},
                    commit=False,
                )
                self.session.commit()
        return recovered
