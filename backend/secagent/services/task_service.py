import asyncio
import hashlib
from contextlib import suppress
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from secagent.api.errors import ApprovalExpired, QueueUnavailable
from secagent.auth.dependencies import AuthenticatedUser
from secagent.agents.critic import Critic
from secagent.agents.executor import Executor
from secagent.agents.parser import TaskParser
from secagent.agents.planner import Planner
from secagent.agents.reporter import Reporter
from secagent.agents.risk import RiskGate
from secagent.agents.runner import AgentRunner
from secagent.domain import TaskRead, TaskRunResult, TaskStatus
from secagent.providers.router import ModelRouter
from secagent.queue.base import JobQueue
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.services.job_service import JobLease, JobService
from secagent.services.task_events import TaskEventService
from secagent.tools.registry import ToolRegistry

TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PARSED: {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PLANNED: {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_HUMAN,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED_RETRYABLE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_HUMAN: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.PAUSED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.FAILED_RETRYABLE: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def require_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise ValueError(
            f"illegal task transition: {current.value} -> {target.value}"
        )


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        router: ModelRouter,
        registry: ToolRegistry,
        data_dir: Path,
        job_queue: JobQueue,
        *,
        lease_seconds: int = 90,
        heartbeat_seconds: int = 15,
        max_auto_retries: int = 1,
        heartbeat_session_factory=None,
    ) -> None:
        self.repository = repository
        self.job_queue = job_queue
        self.heartbeat_seconds = heartbeat_seconds
        self.heartbeat_session_factory = heartbeat_session_factory
        self.job_service = JobService(
            repository,
            job_queue,
            lease_seconds=lease_seconds,
            max_auto_retries=max_auto_retries,
        )
        ledger = LedgerService(repository)
        self.runner = AgentRunner(
            repository=repository,
            ledger=ledger,
            risk_gate=RiskGate(),
            data_dir=data_dir,
            parser=TaskParser(router),
            planner=Planner(router, registry, data_dir),
            executor=Executor(registry, ledger),
            critic=Critic(router, ledger),
            reporter=Reporter(router, ledger),
        )

    def run(
        self, task_id: str, actor: AuthenticatedUser, idempotency_key: str
    ) -> TaskRead:
        return self._enqueue(task_id, actor, idempotency_key, "run")

    async def execute_queued(
        self, task_id: str, command_id: str, worker_id: str
    ) -> TaskRunResult | None:
        lease = self.job_service.claim(task_id, command_id, worker_id)
        if lease is None:
            return None
        self.repository.record_audit(
            None,
            "task.execute",
            "task",
            task_id,
            "started",
            {"command_id": command_id},
        )
        heartbeat = (
            asyncio.create_task(self._heartbeat(lease))
            if self.heartbeat_session_factory is not None
            else None
        )
        try:
            try:
                result = await self.runner.run(
                    task_id,
                    lease,
                    lambda: self.job_service.is_active(
                        lease.job_run_id, lease.worker_id
                    ),
                )
            except Exception as exc:
                job = self.repository.get_job_run(command_id)
                requested = job.status if job is not None else "missing"
                finished = self.job_service.finish(
                    lease.job_run_id, lease.worker_id, "failed"
                )
                if finished and requested in {
                    "pause_requested",
                    "cancel_requested",
                }:
                    final_status = {
                        "pause_requested": "paused",
                        "cancel_requested": "cancelled",
                    }[requested]
                    self.repository.record_audit(
                        None,
                        "task.execute",
                        "task",
                        task_id,
                        final_status,
                        {"command_id": command_id},
                    )
                    return None
                if not finished:
                    self.repository.record_audit(
                        None,
                        "task.execute",
                        "task",
                        task_id,
                        "stale",
                        {"error_type": type(exc).__name__},
                    )
                    return None
                self.repository.record_audit(
                    None,
                    "task.execute",
                    "task",
                    task_id,
                    "failure",
                    {"command_id": command_id, "error_type": type(exc).__name__},
                )
                raise
            finished = self.job_service.finish(
                lease.job_run_id, lease.worker_id, "completed"
            )
            self.repository.record_audit(
                None,
                "task.execute",
                "task",
                task_id,
                "success" if finished else "stale",
                {"command_id": command_id},
            )
            return result if finished else None
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat(self, lease: JobLease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            with self.heartbeat_session_factory() as session:
                renewed = JobService(
                    TaskRepository(session),
                    None,
                    lease_seconds=self.job_service.lease_seconds,
                ).heartbeat(lease.job_run_id, lease.worker_id)
            if renewed is None:
                return

    @staticmethod
    def _command_id(task_id: str, action: str, idempotency_key: str) -> str:
        value = f"secagent:{task_id}:{action}:{idempotency_key}".encode()
        return hashlib.sha256(value).hexdigest()

    def _enqueue(
        self,
        task_id: str,
        actor: AuthenticatedUser,
        idempotency_key: str,
        action: str,
        *,
        task: TaskRead | None = None,
    ) -> TaskRead:
        task = task or self.repository.get_authorized(task_id, actor)
        if task is None:
            raise KeyError(task_id)
        command_id = self._command_id(task_id, action, idempotency_key)
        existing = self.repository.get_job_run(command_id)
        needs_commit = existing is None or existing.status == "enqueue_failed"
        if existing is not None and existing.status not in {
            "enqueue_failed",
            "pending_publish",
            "publishing",
        }:
            latest = self.repository.get_task(task_id)
            if latest is None:
                raise KeyError(task_id)
            return latest

        if existing is None:
            if self.repository.has_unsettled_execution(task_id):
                self.repository.rollback()
                raise ValueError("previous task execution is still settling")
            require_transition(task.status, TaskStatus.QUEUED)
            try:
                self.repository.add_job_run(task_id, command_id)
                updated = self.repository.transition_task_status(
                    task_id, task.status, TaskStatus.QUEUED, commit=False
                )
            except IntegrityError:
                self.repository.rollback()
                return self._enqueue(
                    task_id,
                    actor,
                    idempotency_key,
                    action,
                )
        elif existing.status == "enqueue_failed":
            if not self.repository.claim_job_republish(command_id):
                latest = self.repository.get_task(task_id)
                if latest is None:
                    raise KeyError(task_id)
                return latest
            updated = self.repository.transition_task_status(
                task_id,
                TaskStatus.FAILED_RETRYABLE,
                TaskStatus.QUEUED,
                commit=False,
            )
        else:
            updated = task
        if needs_commit:
            TaskEventService(self.repository.session).append(
                task_id,
                "task.queued",
                {"action": action},
                commit=False,
            )
            self.repository.record_audit(
                actor.id,
                f"task.{action}",
                "task",
                task_id,
                "queued",
                {"command_id": command_id},
                commit=False,
            )
            self.repository.commit()

        publish_status = self.repository.begin_job_publish(command_id)
        if publish_status != "claimed":
            self.repository.rollback()
            if publish_status == "enqueue_failed":
                return self._enqueue(
                    task_id,
                    actor,
                    idempotency_key,
                    action,
                )
            latest = self.repository.get_task(task_id)
            if latest is None:
                raise KeyError(task_id)
            return latest

        try:
            broker_id = self.job_queue.enqueue(task_id, command_id)
        except Exception as exc:
            self.repository.mark_job_enqueue_failed(task_id, command_id)
            TaskEventService(self.repository.session).append(
                task_id,
                "task.failed_retryable",
                {"action": action, "reason": "queue_unavailable"},
                commit=False,
            )
            self.repository.record_audit(
                actor.id,
                f"task.{action}",
                "task",
                task_id,
                "failure",
                {"command_id": command_id, "error_type": type(exc).__name__},
                commit=False,
            )
            self.repository.commit()
            raise QueueUnavailable() from exc
        self.repository.mark_job_enqueued(command_id, broker_id)
        return updated

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        actor: AuthenticatedUser,
        action: str,
    ) -> TaskRead:
        task = self.repository.get_authorized(task_id, actor)
        if task is None:
            raise KeyError(task_id)
        try:
            require_transition(task.status, target)
            # Keep the command/job -> task lock order used by publishers/workers.
            if action in {"pause", "cancel"}:
                self.repository.invalidate_jobs_for_transition(task_id, action)
            updated = self.repository.transition_task_status(
                task_id, task.status, target, commit=False
            )
        except ValueError:
            self.repository.rollback()
            self.repository.record_audit(
                actor.id,
                f"task.{action}",
                "task",
                task_id,
                "failure",
                {"from_status": task.status.value, "to_status": target.value},
            )
            raise
        self.repository.record_audit(
            actor.id,
            f"task.{action}",
            "task",
            task_id,
            "success",
            {},
            commit=False,
        )
        TaskEventService(self.repository.session).append(
            task_id,
            f"task.{target.value}",
            {"action": action},
            commit=False,
        )
        self.repository.commit()
        return updated

    def pause(self, task_id: str, actor: AuthenticatedUser) -> TaskRead:
        return self.transition(task_id, TaskStatus.PAUSED, actor, "pause")

    def resume(
        self, task_id: str, actor: AuthenticatedUser, idempotency_key: str
    ) -> TaskRead:
        return self._enqueue(task_id, actor, idempotency_key, "resume")

    def cancel(self, task_id: str, actor: AuthenticatedUser) -> TaskRead:
        return self.transition(task_id, TaskStatus.CANCELLED, actor, "cancel")

    def retry(
        self, task_id: str, actor: AuthenticatedUser, idempotency_key: str
    ) -> TaskRead:
        return self._enqueue(task_id, actor, idempotency_key, "retry")

    async def approve(
        self,
        task_id: str,
        actor: AuthenticatedUser,
        *,
        approved: bool,
        reason: str,
        idempotency_key: str | None = None,
    ) -> TaskRead:
        task = self.repository.get_authorized(task_id, actor)
        if task is None:
            raise KeyError(task_id)
        if approved and idempotency_key is not None:
            command_id = self._command_id(task_id, "approve", idempotency_key)
            if self.repository.get_job_run(command_id) is not None:
                return self._enqueue(
                    task_id,
                    actor,
                    idempotency_key,
                    "approve",
                    task=task,
                )
        if task.status is not TaskStatus.WAITING_HUMAN:
            self.repository.record_audit(
                actor.id,
                "task.approve",
                "task",
                task_id,
                "failure",
                {"from_status": task.status.value},
            )
            raise ValueError("task is not waiting for approval")
        try:
            approval = self.repository.decide_latest_approval(
                task_id,
                approved=approved,
                reason=reason,
                decided_by=actor.id,
                commit=False,
            )
        except ApprovalExpired as exc:
            self.repository.record_audit(
                actor.id,
                "approval.expired",
                "approval",
                exc.approval_id,
                "failure",
                {"task_id": task_id},
            )
            raise
        except ValueError:
            self.repository.rollback()
            if approved and idempotency_key is not None:
                command_id = self._command_id(task_id, "approve", idempotency_key)
                if self.repository.get_job_run(command_id) is not None:
                    return self._enqueue(
                        task_id,
                        actor,
                        idempotency_key,
                        "approve",
                    )
            self.repository.record_audit(
                actor.id,
                "task.approve",
                "task",
                task_id,
                "failure",
                {"reason": "pending_approval_not_found"},
            )
            raise
        if approved and idempotency_key is None:
            raise ValueError("Idempotency-Key header is required")
        target = TaskStatus.QUEUED if approved else TaskStatus.CANCELLED
        try:
            updated = self.repository.transition_task_status(
                task_id,
                TaskStatus.WAITING_HUMAN,
                target,
                commit=False,
            )
        except ValueError:
            self.repository.rollback()
            self.repository.record_audit(
                actor.id,
                "task.approve",
                "task",
                task_id,
                "failure",
                {"reason": "concurrent_state_change"},
            )
            raise
        self.repository.record_audit(
            actor.id,
            "task.approve",
            "task",
            task_id,
            "success",
            {"approved": approved, "approval_id": approval.id},
            commit=False,
        )
        TaskEventService(self.repository.session).append(
            task_id,
            f"task.{target.value}",
            {"action": "approve", "approved": approved},
            commit=False,
        )
        if approved:
            command_id = self._command_id(task_id, "approve", idempotency_key)
            self.repository.add_job_run(task_id, command_id)
            self.repository.commit()
            return self._enqueue(
                task_id,
                actor,
                idempotency_key,
                "approve",
            )
        else:
            self.repository.commit()
        return updated
