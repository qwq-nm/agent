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
from secagent.domain import (
    ModelStage,
    ParsedTask,
    PlanPreview,
    PlanPreviewStep,
    PlanStep,
    TaskRead,
    TaskRunResult,
    TaskStatus,
)
from secagent.providers.router import ModelRouter
from secagent.queue.base import JobQueue
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.services.job_service import JobLease, JobService
from secagent.services.task_events import TaskEventService
from secagent.tools.registry import ToolRegistry

TRANSITIONS = {
    TaskStatus.CREATED: {
        TaskStatus.PLANNING,
        TaskStatus.QUEUED,
        TaskStatus.PAUSED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PLANNING: {TaskStatus.PLANNED, TaskStatus.FAILED_RETRYABLE},
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
    TaskStatus.FAILED_RETRYABLE: {
        TaskStatus.PLANNING,
        TaskStatus.QUEUED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: {TaskStatus.QUEUED},
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
        task_timeout_seconds: int = 1800,
        max_replans: int = 2,
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
        self.ledger = LedgerService(repository, data_dir=data_dir)
        self.data_dir = data_dir
        self.parser = TaskParser(router)
        self.planner = Planner(router, registry, data_dir)
        self.runner = AgentRunner(
            repository=repository,
            ledger=self.ledger,
            risk_gate=RiskGate(),
            data_dir=data_dir,
            parser=self.parser,
            planner=self.planner,
            executor=Executor(registry, self.ledger),
            critic=Critic(router, self.ledger),
            reporter=Reporter(router, self.ledger),
            timeout_seconds=task_timeout_seconds,
            max_replans=max_replans,
        )

    def run(
        self, task_id: str, actor: AuthenticatedUser, idempotency_key: str
    ) -> TaskRead:
        return self._enqueue(task_id, actor, idempotency_key, "run")

    async def plan(self, task_id: str, actor: AuthenticatedUser) -> TaskRead:
        task = self.repository.get_authorized(task_id, actor)
        if task is None:
            raise KeyError(task_id)
        if task.status is TaskStatus.PLANNED:
            return task
        if task.status not in {TaskStatus.CREATED, TaskStatus.FAILED_RETRYABLE}:
            raise ValueError("task must be created before generating a plan")

        workspace = self.data_dir / "tasks" / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        fingerprint = AgentRunner._task_fingerprint(task, workspace)
        try:
            self.repository.transition_task_status(
                task_id, task.status, TaskStatus.PLANNING, commit=False
            )
            TaskEventService(self.repository.session).append(
                task_id, "agent.parsing_started", {}, commit=False
            )
            self.repository.record_audit(
                actor.id,
                "task.plan",
                "task",
                task_id,
                "started",
                {},
                commit=False,
            )
            self.repository.commit()

            checkpoint = self.repository.load_plan_preview_checkpoint(
                task_id, fingerprint=fingerprint
            )
            stages = checkpoint.get("stages", {})
            parsed = await self._preview_parse(task, fingerprint, stages)
            self.repository.set_task_scene(task_id, parsed.scene)
            TaskEventService(self.repository.session).append(
                task_id,
                "agent.parsing_completed",
                {"scene": parsed.scene.value},
                commit=False,
            )
            TaskEventService(self.repository.session).append(
                task_id, "agent.planning_started", {}, commit=False
            )
            self.repository.commit()

            plan = await self._preview_plan(task, parsed, fingerprint, stages)
            preview = self._plan_preview(task, parsed, plan)
            TaskEventService(self.repository.session).append(
                task_id,
                "agent.planning_completed",
                preview.model_dump(mode="json"),
                commit=False,
            )
            updated = self.repository.transition_task_status(
                task_id, TaskStatus.PLANNING, TaskStatus.PLANNED, commit=False
            )
            self.repository.record_audit(
                actor.id,
                "task.plan",
                "task",
                task_id,
                "success",
                {"steps": len(plan), "scene": parsed.scene.value},
                commit=False,
            )
            self.repository.commit()
            return updated
        except Exception as exc:
            self.repository.rollback()
            try:
                current = self.repository.get_task(task_id)
                if current is not None and current.status is TaskStatus.PLANNING:
                    self.repository.transition_task_status(
                        task_id,
                        TaskStatus.PLANNING,
                        TaskStatus.FAILED_RETRYABLE,
                        commit=False,
                    )
                TaskEventService(self.repository.session).append(
                    task_id,
                    "task.failed_retryable",
                    {"action": "plan", "error_type": type(exc).__name__},
                    commit=False,
                )
                self.repository.record_audit(
                    actor.id,
                    "task.plan",
                    "task",
                    task_id,
                    "failure",
                    {"error_type": type(exc).__name__},
                    commit=False,
                )
                self.repository.commit()
            except Exception:
                self.repository.rollback()
            raise

    async def _preview_parse(
        self, task: TaskRead, fingerprint: str, stages: dict
    ) -> ParsedTask:
        parsed_data = AgentRunner._successful_stage(stages, ModelStage.TASK_PARSE)
        if parsed_data is not None:
            return ParsedTask.model_validate(parsed_data)
        parsed, response = await self.parser.parse(task)
        model_call_id = self.ledger.record_model_response(
            task.id, ModelStage.TASK_PARSE, response
        )
        self.repository.save_plan_preview_checkpoint(
            task.id,
            fingerprint=fingerprint,
            stage=ModelStage.TASK_PARSE.value,
            data=parsed.model_dump(mode="json"),
            model_call_id=model_call_id,
        )
        return parsed

    async def _preview_plan(
        self, task: TaskRead, parsed: ParsedTask, fingerprint: str, stages: dict
    ) -> list[PlanStep]:
        plan_data = AgentRunner._successful_stage(stages, ModelStage.PLAN)
        if plan_data is not None:
            plan, _replan_round, _step_start_index = self.runner._load_plan_checkpoint(
                plan_data
            )
            return plan
        plan, response = await self.planner.plan(task, parsed)
        model_call_id = self.ledger.record_model_response(
            task.id, ModelStage.PLAN, response
        )
        self.repository.save_plan_preview_checkpoint(
            task.id,
            fingerprint=fingerprint,
            stage=ModelStage.PLAN.value,
            data=self.runner._plan_checkpoint(plan, 0, 1),
            model_call_id=model_call_id,
        )
        return plan

    @staticmethod
    def _plan_preview(
        task: TaskRead, parsed: ParsedTask, plan: list[PlanStep]
    ) -> PlanPreview:
        return PlanPreview(
            task_id=task.id,
            scene=parsed.scene,
            goal_summary=parsed.goal,
            target_summary=task.target_url,
            authorization_summary=parsed.authorization_scope,
            safety_mode=task.safety_mode,
            constraints=parsed.constraints,
            expected_outputs=parsed.expected_outputs,
            steps=[
                PlanPreviewStep(index=index, **step.model_dump())
                for index, step in enumerate(plan, start=1)
            ],
        )

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
            final_job_status = (
                "completed" if result.status is TaskStatus.COMPLETED else "failed"
            )
            finished = self.job_service.finish(
                lease.job_run_id, lease.worker_id, final_job_status
            )
            self.repository.record_audit(
                None,
                "task.execute",
                "task",
                task_id,
                final_job_status if finished else "stale",
                {"command_id": command_id, "task_status": result.status.value},
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
                current_attempt = self.repository.current_task_attempt(task_id)
                attempt = (
                    current_attempt + 1
                    if action in {"retry", "continue"}
                    else max(1, current_attempt)
                )
                if attempt == 1:
                    self.repository.add_job_run(task_id, command_id)
                else:
                    self.repository.add_job_run(
                        task_id, command_id, attempt=attempt
                    )
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

    def continue_analysis(
        self, task_id: str, actor: AuthenticatedUser, idempotency_key: str
    ) -> TaskRead:
        task = self.repository.get_authorized(task_id, actor)
        if task is None:
            raise KeyError(task_id)
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED_RETRYABLE}:
            raise ValueError("task can only continue after completion or retryable failure")
        self.repository.clear_orchestration_checkpoint(task_id, commit=False)
        TaskEventService(self.repository.session).append(
            task_id,
            "task.continue_requested",
            {
                "from_status": task.status.value,
                "reason": "user requested another evidence-driven analysis round",
            },
            commit=False,
        )
        self.repository.commit()
        return self._enqueue(task_id, actor, idempotency_key, "continue", task=task)

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
            self.repository.add_job_run(
                task_id,
                command_id,
                attempt=max(1, self.repository.current_task_attempt(task_id)),
            )
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
