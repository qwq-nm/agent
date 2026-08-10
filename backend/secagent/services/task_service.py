from pathlib import Path

from secagent.agents.critic import Critic
from secagent.agents.executor import Executor
from secagent.agents.parser import TaskParser
from secagent.agents.planner import Planner
from secagent.agents.reporter import Reporter
from secagent.agents.risk import RiskGate
from secagent.agents.runner import AgentRunner
from secagent.domain import TaskRunResult
from secagent.providers.router import ModelRouter
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.tools.registry import ToolRegistry


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        router: ModelRouter,
        registry: ToolRegistry,
        data_dir: Path,
    ) -> None:
        ledger = LedgerService(repository)
        self.runner = AgentRunner(
            repository=repository,
            ledger=ledger,
            risk_gate=RiskGate(),
            data_dir=data_dir,
            parser=TaskParser(router),
            planner=Planner(router, registry),
            executor=Executor(registry, ledger),
            critic=Critic(router, ledger),
            reporter=Reporter(router, ledger),
        )

    async def run(self, task_id: str) -> TaskRunResult:
        return await self.runner.run(task_id)
