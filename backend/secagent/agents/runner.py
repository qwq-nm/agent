from pathlib import Path
import json
from collections.abc import Callable

from secagent.agents.critic import Critic
from secagent.agents.executor import ExecutionInterrupted, Executor
from secagent.agents.parser import TaskParser
from secagent.agents.planner import Planner
from secagent.agents.reporter import Reporter
from secagent.agents.risk import RiskGate
from secagent.domain import ModelStage, TaskRunResult, TaskStatus
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.tools.base import ToolContext
from secagent.domain import ToolResult
from secagent.services.task_events import TaskEventService


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
        self._lease_is_active: Callable[[], bool] | None = None

    async def run(
        self,
        task_id: str,
        lease_is_active: Callable[[], bool] | None = None,
    ) -> TaskRunResult:
        self._lease_is_active = lease_is_active
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        try:
            self._ensure_running(task_id)
            parsed, parse_call = await self.parser.parse(task)
            self._ensure_running(task_id)
            self.ledger.record_model_response(
                task_id, ModelStage.TASK_PARSE, parse_call
            )
            self.repository.set_task_scene(task_id, parsed.scene)

            plan, plan_call = await self.planner.plan(task, parsed)
            self._ensure_running(task_id)
            self.ledger.record_model_response(task_id, ModelStage.PLAN, plan_call)
            workspace = self.data_dir / "tasks" / task_id
            workspace.mkdir(parents=True, exist_ok=True)
            runtime: dict = {}

            for index, step in enumerate(plan, start=1):
                self._ensure_running(task_id)
                params = self._resolve_params(step.params, workspace, runtime)
                idempotency_key = self.repository.step_idempotency_key(
                    task_id, index, step
                )
                cached = self.repository.completed_step_result(
                    task_id, idempotency_key
                )
                if cached is not None:
                    result = ToolResult.model_validate(cached["result"])
                    if step.tool_name == "http_fetch" and result.evidence:
                        runtime["http_response"] = result.evidence[0].get(
                            "metadata", {}
                        )
                    continue
                step_id = self.repository.add_step(
                    task_id,
                    index,
                    step,
                    idempotency_key=idempotency_key,
                )
                approved = self.repository.is_tool_approved(
                    task_id, step.tool_name
                )
                decision = self.risk_gate.check(
                    step.risk_level, approved=approved
                )
                if decision.action == "wait":
                    self._ensure_running(task_id)
                    self.repository.add_approval(
                        task_id,
                        step_id=step_id,
                        tool_name=step.tool_name,
                        risk_level=step.risk_level.value,
                        params_summary=str(params),
                    )
                    self.repository.set_task_status(
                        task_id, TaskStatus.WAITING_HUMAN, commit=False
                    )
                    TaskEventService(self.repository.session).append(
                        task_id,
                        "task.waiting_human",
                        {"step_index": index},
                        commit=False,
                    )
                    self.repository.commit()
                    return TaskRunResult(
                        task_id=task_id,
                        status=TaskStatus.WAITING_HUMAN,
                        is_demo=parse_call.is_demo or plan_call.is_demo,
                    )
                result = await self.executor.execute(
                    task_id=task_id,
                    step_id=step_id,
                    tool_name=step.tool_name,
                    params=params,
                    context=ToolContext(task_id, parsed.scene.value, workspace),
                )
                if result.success:
                    self.repository.complete_step(
                        step_id,
                        result.model_dump(mode="json"),
                        self.ledger.evidence_hashes(result),
                    )
                else:
                    self.repository.update_step(step_id, "failed")
                if step.tool_name == "http_fetch" and result.evidence:
                    runtime["http_response"] = result.evidence[0].get(
                        "metadata", {}
                    )
                if not result.success:
                    raise RuntimeError(result.error or result.summary)

            self._ensure_running(task_id)
            critic, critic_call = await self.critic.review(task_id, parsed)
            self._ensure_running(task_id)
            self.ledger.record_model_response(
                task_id, ModelStage.CRITIC, critic_call
            )
            if not critic.is_complete:
                raise RuntimeError(f"missing evidence: {critic.missing_evidence}")

            report, report_call = await self.reporter.render(task_id, parsed)
            self._ensure_running(task_id)
            self.ledger.record_model_response(
                task_id, ModelStage.REPORT, report_call
            )
            is_demo = any(
                call.is_demo
                for call in (parse_call, plan_call, critic_call, report_call)
            )
            self.repository.set_task_status(
                task_id,
                TaskStatus.RUNNING,
                is_demo=is_demo,
                commit=False,
            )
            self.repository.save_report(task_id, report, is_demo=is_demo)
            return TaskRunResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                is_demo=is_demo,
                report=report,
            )
        except ExecutionInterrupted:
            raise
        except Exception as exc:
            self.ledger.record_error(task_id, type(exc).__name__, str(exc))
            raise

    def _ensure_running(self, task_id: str) -> None:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ExecutionInterrupted("task execution was invalidated")
        if self._lease_is_active is not None and not self._lease_is_active():
            raise ExecutionInterrupted("job lease was lost")

    @staticmethod
    def _resolve_params(
        params: dict, workspace: Path, runtime: dict
    ) -> dict:
        resolved = dict(params)
        if resolved.get("file_path") == "$upload":
            metadata = json.loads(
                (workspace / "upload.json").read_text(encoding="utf-8")
            )
            resolved["file_path"] = str(
                workspace / "uploads" / metadata["stored_name"]
            )
        if resolved.get("project_path") == "$project":
            extracted = workspace / "extracted"
            resolved["project_path"] = str(
                extracted if extracted.is_dir() else workspace / "uploads"
            )
        if resolved.get("response") == "$http":
            if "http_response" not in runtime:
                raise RuntimeError("HTTP observation is not available")
            resolved["response"] = runtime["http_response"]
        return resolved
