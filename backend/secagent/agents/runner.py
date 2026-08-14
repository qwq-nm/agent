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
from secagent.repository import StaleJobLease, TaskRepository
from secagent.services.ledger import LedgerService
from secagent.services.job_service import JobLease
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
    async def run(
        self,
        task_id: str,
        lease: JobLease,
        lease_is_active: Callable[[], bool] | None = None,
    ) -> TaskRunResult:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        try:
            self._ensure_running(task_id, lease_is_active)
            parsed, parse_call = await self.parser.parse(task)
            self._ensure_running(task_id, lease_is_active)
            self.ledger.record_model_response(
                task_id, ModelStage.TASK_PARSE, parse_call, lease=lease
            )
            self.repository.set_task_scene(task_id, parsed.scene, lease=lease)

            plan, plan_call = await self.planner.plan(task, parsed)
            self._ensure_running(task_id, lease_is_active)
            self.ledger.record_model_response(
                task_id, ModelStage.PLAN, plan_call, lease=lease
            )
            workspace = self.data_dir / "tasks" / task_id
            workspace.mkdir(parents=True, exist_ok=True)
            runtime: dict = {}

            for index, step in enumerate(plan, start=1):
                self._ensure_running(task_id, lease_is_active)
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
                    lease=lease,
                )
                approved = self.repository.is_tool_approved(
                    task_id, step.tool_name
                )
                decision = self.risk_gate.check(
                    step.risk_level, approved=approved
                )
                if decision.action == "wait":
                    self._ensure_running(task_id, lease_is_active)
                    self.repository.add_approval(
                        task_id,
                        step_id=step_id,
                        tool_name=step.tool_name,
                        risk_level=step.risk_level.value,
                        params_summary=str(params),
                        lease=lease,
                        commit=False,
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
                    lease=lease,
                )
                if step.tool_name == "http_fetch" and result.evidence:
                    runtime["http_response"] = result.evidence[0].get(
                        "metadata", {}
                    )
                if not result.success:
                    raise RuntimeError(result.error or result.summary)

            self._ensure_running(task_id, lease_is_active)
            critic, critic_call = await self.critic.review(task_id, parsed)
            self._ensure_running(task_id, lease_is_active)
            self.ledger.record_model_response(
                task_id, ModelStage.CRITIC, critic_call, lease=lease
            )
            if not critic.is_complete:
                raise RuntimeError(f"missing evidence: {critic.missing_evidence}")

            report, report_call = await self.reporter.render(task_id, parsed)
            self._ensure_running(task_id, lease_is_active)
            self.ledger.record_model_response(
                task_id, ModelStage.REPORT, report_call, lease=lease
            )
            is_demo = any(
                call.is_demo
                for call in (parse_call, plan_call, critic_call, report_call)
            )
            self.repository.save_report(
                task_id, report, is_demo=is_demo, lease=lease
            )
            return TaskRunResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                is_demo=is_demo,
                report=report,
            )
        except ExecutionInterrupted:
            raise
        except StaleJobLease as exc:
            raise ExecutionInterrupted("job lease was lost") from exc
        except Exception as exc:
            try:
                self.ledger.record_error(
                    task_id, type(exc).__name__, str(exc), lease=lease
                )
            except StaleJobLease as stale:
                raise ExecutionInterrupted("job lease was lost") from stale
            raise

    def _ensure_running(
        self,
        task_id: str,
        lease_is_active: Callable[[], bool] | None,
    ) -> None:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ExecutionInterrupted("task execution was invalidated")
        if lease_is_active is not None and not lease_is_active():
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
