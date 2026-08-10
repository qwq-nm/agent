from pathlib import Path

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

    async def run(self, task_id: str) -> TaskRunResult:
        return await self.runner.run(task_id)

    def transition(self, task_id: str, target: TaskStatus) -> TaskRead:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        require_transition(task.status, target)
        return self.repository.set_task_status(task_id, target)

    def pause(self, task_id: str) -> TaskRead:
        return self.transition(task_id, TaskStatus.PAUSED)

    def resume(self, task_id: str) -> TaskRead:
        return self.transition(task_id, TaskStatus.RUNNING)

    def cancel(self, task_id: str) -> TaskRead:
        return self.transition(task_id, TaskStatus.CANCELLED)

    def retry(self, task_id: str) -> TaskRead:
        return self.transition(task_id, TaskStatus.RUNNING)

    async def approve(
        self, task_id: str, *, approved: bool, reason: str
    ) -> TaskRead:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is not TaskStatus.WAITING_HUMAN:
            raise ValueError("task is not waiting for approval")
        self.repository.decide_latest_approval(
            task_id, approved=approved, reason=reason
        )
        target = TaskStatus.RUNNING if approved else TaskStatus.CANCELLED
        updated = self.transition(task_id, target)
        if approved:
            await self.runner.run(task_id)
            latest = self.repository.get_task(task_id)
            if latest is None:
                raise KeyError(task_id)
            return latest
        return updated
