from pathlib import Path

from secagent.api.errors import ApprovalExpired
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
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.tools.registry import ToolRegistry

TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PARSED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PLANNED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_HUMAN,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED_RETRYABLE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_HUMAN: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.PAUSED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.FAILED_RETRYABLE: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
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
    ) -> None:
        self.repository = repository
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

    async def run(self, task_id: str, actor: AuthenticatedUser) -> TaskRunResult:
        task = self.repository.get_authorized(task_id, actor)
        if task is None:
            raise KeyError(task_id)
        self.repository.record_audit(
            actor.id, "task.run", "task", task_id, "started", {}
        )
        return await self.runner.run(task_id)

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
            updated = self.repository.transition_task_status(
                task_id, task.status, target, commit=False
            )
        except ValueError:
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
            actor.id, f"task.{action}", "task", task_id, "success", {}
        )
        return updated

    def pause(self, task_id: str, actor: AuthenticatedUser) -> TaskRead:
        return self.transition(task_id, TaskStatus.PAUSED, actor, "pause")

    def resume(self, task_id: str, actor: AuthenticatedUser) -> TaskRead:
        return self.transition(task_id, TaskStatus.RUNNING, actor, "resume")

    def cancel(self, task_id: str, actor: AuthenticatedUser) -> TaskRead:
        return self.transition(task_id, TaskStatus.CANCELLED, actor, "cancel")

    def retry(self, task_id: str, actor: AuthenticatedUser) -> TaskRead:
        return self.transition(task_id, TaskStatus.RUNNING, actor, "retry")

    async def approve(
        self,
        task_id: str,
        actor: AuthenticatedUser,
        *,
        approved: bool,
        reason: str,
    ) -> TaskRead:
        task = self.repository.get_authorized(task_id, actor)
        if task is None:
            raise KeyError(task_id)
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
            self.repository.record_audit(
                actor.id,
                "task.approve",
                "task",
                task_id,
                "failure",
                {"reason": "pending_approval_not_found"},
            )
            raise
        target = TaskStatus.RUNNING if approved else TaskStatus.CANCELLED
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
        )
        if approved:
            await self.runner.run(task_id)
            latest = self.repository.get_task(task_id)
            if latest is None:
                raise KeyError(task_id)
            updated = latest
        return updated
