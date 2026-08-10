from pathlib import Path

from secagent.agents.critic import Critic
from secagent.agents.executor import Executor
from secagent.agents.parser import TaskParser
from secagent.agents.planner import Planner
from secagent.agents.reporter import Reporter
from secagent.agents.risk import RiskGate
from secagent.domain import ModelStage, TaskRunResult, TaskStatus
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.tools.base import ToolContext


class AgentRunner:
    def __init__(
        self,
        repository: TaskRepository,
        ledger: LedgerService,
        risk_gate: RiskGate,
        data_dir: Path,
        parser: TaskParser,
        planner: Planner,
        executor: Executor,
        critic: Critic,
        reporter: Reporter,
    ) -> None:
        self.repository = repository
        self.ledger = ledger
        self.risk_gate = risk_gate
        self.data_dir = data_dir
        self.parser = parser
        self.planner = planner
        self.executor = executor
        self.critic = critic
        self.reporter = reporter

    async def run(self, task_id: str) -> TaskRunResult:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        try:
            self.repository.set_task_status(task_id, TaskStatus.RUNNING)
            parsed, parse_call = await self.parser.parse(task)
            self.ledger.record_model_response(
                task_id, ModelStage.TASK_PARSE, parse_call
            )
            self.repository.set_task_scene(task_id, parsed.scene)

            plan, plan_call = await self.planner.plan(task, parsed)
            self.ledger.record_model_response(task_id, ModelStage.PLAN, plan_call)
            workspace = self.data_dir / "tasks" / task_id
            workspace.mkdir(parents=True, exist_ok=True)

            for index, step in enumerate(plan, start=1):
                step_id = self.repository.add_step(task_id, index, step)
                decision = self.risk_gate.check(step.risk_level, approved=False)
                if decision.action == "wait":
                    self.repository.set_task_status(task_id, TaskStatus.WAITING_HUMAN)
                    return TaskRunResult(
                        task_id=task_id,
                        status=TaskStatus.WAITING_HUMAN,
                        is_demo=parse_call.is_demo or plan_call.is_demo,
                    )
                result = await self.executor.execute(
                    task_id=task_id,
                    step_id=step_id,
                    tool_name=step.tool_name,
                    params=step.params,
                    context=ToolContext(task_id, parsed.scene.value, workspace),
                )
                self.repository.update_step(
                    step_id, "success" if result.success else "failed"
                )
                if not result.success:
                    raise RuntimeError(result.error or result.summary)

            critic, critic_call = await self.critic.review(task_id, parsed)
            self.ledger.record_model_response(
                task_id, ModelStage.CRITIC, critic_call
            )
            if not critic.is_complete:
                raise RuntimeError(f"missing evidence: {critic.missing_evidence}")

            report, report_call = await self.reporter.render(task_id, parsed)
            self.ledger.record_model_response(
                task_id, ModelStage.REPORT, report_call
            )
            is_demo = any(
                call.is_demo
                for call in (parse_call, plan_call, critic_call, report_call)
            )
            self.repository.save_report(task_id, report, is_demo=is_demo)
            self.repository.set_task_status(
                task_id, TaskStatus.COMPLETED, is_demo=is_demo
            )
            return TaskRunResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                is_demo=is_demo,
                report=report,
            )
        except Exception as exc:
            self.ledger.record_error(task_id, type(exc).__name__, str(exc))
            self.repository.set_task_status(task_id, TaskStatus.FAILED_RETRYABLE)
            raise
