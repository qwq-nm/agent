from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Generic, TypeVar

from secagent.agents.budget import BudgetExceeded, TaskBudget
from secagent.agents.critic import Critic
from secagent.agents.executor import ExecutionInterrupted, Executor
from secagent.agents.parser import TaskParser
from secagent.agents.planner import Planner
from secagent.agents.reporter import ReportArtifact, Reporter
from secagent.agents.risk import RiskGate
from secagent.domain import (
    CriticDecision,
    ModelResponse,
    ModelStage,
    ParsedTask,
    PlanStep,
    TaskRead,
    TaskRunResult,
    TaskStatus,
    ToolResult,
)
from secagent.providers.base import ProviderFailure
from secagent.providers.router import FIXED_PROVIDER
from secagent.repository import StaleJobLease, TaskRepository
from secagent.services.job_service import JobLease
from secagent.services.ledger import LedgerService
from secagent.services.task_events import TaskEventService
from secagent.tools.base import ToolContext


T = TypeVar("T")


@dataclass(frozen=True)
class _ModelOutcome(Generic[T]):
    value: T
    model_call_id: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        *,
        timeout_seconds: int = 300,
        now: Callable[[], datetime] = utcnow,
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
        self.timeout_seconds = timeout_seconds
        self.now = now

    async def run(
        self,
        task_id: str,
        lease: JobLease,
        lease_is_active: Callable[[], bool] | None = None,
    ) -> TaskRunResult:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        active_step_id: str | None = None
        try:
            self._ensure_running(task_id, lease_is_active)
            budget = TaskBudget(
                **self.repository.budget_state(
                    task_id,
                    lease=lease,
                    timeout_seconds=self.timeout_seconds,
                    now=self.now(),
                ),
                now=self.now,
            )
            budget.check_usage()
            workspace = self.data_dir / "tasks" / task_id
            workspace.mkdir(parents=True, exist_ok=True)
            fingerprint = self._task_fingerprint(task, workspace)
            checkpoint = self.repository.load_orchestration_checkpoint(
                task_id, lease=lease, fingerprint=fingerprint
            )
            stages = checkpoint.get("stages", {})

            parsed_data = self._successful_stage(stages, ModelStage.TASK_PARSE)
            if parsed_data is None:
                outcome = await self._invoke_model(
                    task_id,
                    lease,
                    budget,
                    ModelStage.TASK_PARSE,
                    lambda: self.parser.parse(task),
                )
                parsed = outcome.value
                self.repository.set_task_scene(task_id, parsed.scene, lease=lease)
                self.repository.save_orchestration_checkpoint(
                    task_id,
                    lease=lease,
                    fingerprint=fingerprint,
                    stage=ModelStage.TASK_PARSE.value,
                    data=parsed.model_dump(mode="json"),
                    model_call_id=outcome.model_call_id,
                )
                stages[ModelStage.TASK_PARSE.value] = {
                    "status": "success",
                    "data": parsed.model_dump(mode="json"),
                }
            else:
                parsed = ParsedTask.model_validate(parsed_data)

            plan_data = self._successful_stage(stages, ModelStage.PLAN)
            if plan_data is None:
                outcome = await self._invoke_model(
                    task_id,
                    lease,
                    budget,
                    ModelStage.PLAN,
                    lambda: self.planner.plan(task, parsed),
                )
                plan = outcome.value
                serialized_plan = [step.model_dump(mode="json") for step in plan]
                self.repository.save_orchestration_checkpoint(
                    task_id,
                    lease=lease,
                    fingerprint=fingerprint,
                    stage=ModelStage.PLAN.value,
                    data={"steps": serialized_plan},
                    model_call_id=outcome.model_call_id,
                )
                stages[ModelStage.PLAN.value] = {
                    "status": "success",
                    "data": {"steps": serialized_plan},
                }
            else:
                raw_steps = (
                    plan_data.get("steps") if isinstance(plan_data, dict) else None
                )
                if not isinstance(raw_steps, list):
                    raise ValueError("invalid persisted plan checkpoint")
                plan = [PlanStep.model_validate(step) for step in raw_steps]

            runtime: dict[str, Any] = {}
            for index, step in enumerate(plan, start=1):
                self._ensure_running(task_id, lease_is_active)
                params = self._resolve_params(step.params, workspace, runtime)
                idempotency_key = self.repository.step_idempotency_key(
                    task_id, index, step
                )
                cached = self.repository.completed_step_result(task_id, idempotency_key)
                if cached is not None:
                    result = ToolResult.model_validate(cached["result"])
                    self._update_runtime(step.tool_name, result, runtime)
                    continue

                existing = self.repository.get_step(task_id, index)
                if (
                    existing is not None
                    and existing.idempotency_key == idempotency_key
                    and existing.status == "pending"
                ):
                    step_id = existing.id
                else:
                    if existing is None:
                        budget.consume_step()
                    step_id = self.repository.add_step(
                        task_id,
                        index,
                        step,
                        idempotency_key=idempotency_key,
                        lease=lease,
                    )
                active_step_id = step_id
                self.repository.set_current_step_index(task_id, index, lease=lease)
                approved = self.repository.is_tool_approved(task_id, step.tool_name)
                decision = self.risk_gate.check(step.risk_level, approved=approved)
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
                        is_demo=self._is_demo(task_id),
                    )
                budget.check_deadline()
                result = await self.executor.execute(
                    task_id=task_id,
                    step_id=step_id,
                    tool_name=step.tool_name,
                    params=params,
                    context=ToolContext(task_id, parsed.scene.value, workspace),
                    lease=lease,
                )
                self._update_runtime(step.tool_name, result, runtime)
                if not result.success:
                    raise RuntimeError(result.error or result.summary)
                active_step_id = None

            critic_data = self._successful_stage(stages, ModelStage.CRITIC)
            if critic_data is None:
                outcome = await self._invoke_model(
                    task_id,
                    lease,
                    budget,
                    ModelStage.CRITIC,
                    lambda: self.critic.review(task_id, parsed),
                )
                critic = outcome.value
                self.repository.save_orchestration_checkpoint(
                    task_id,
                    lease=lease,
                    fingerprint=fingerprint,
                    stage=ModelStage.CRITIC.value,
                    data=critic.model_dump(mode="json"),
                    model_call_id=outcome.model_call_id,
                )
                stages[ModelStage.CRITIC.value] = {
                    "status": "success",
                    "data": critic.model_dump(mode="json"),
                }
            else:
                critic = CriticDecision.model_validate(critic_data)
            if not critic.is_complete:
                raise RuntimeError(f"missing evidence: {critic.missing_evidence}")

            report_data = self._successful_stage(stages, ModelStage.REPORT)
            if report_data is None:
                outcome = await self._invoke_model(
                    task_id,
                    lease,
                    budget,
                    ModelStage.REPORT,
                    lambda: self.reporter.render(task_id, parsed),
                )
                report_artifact = outcome.value
                self.repository.save_orchestration_checkpoint(
                    task_id,
                    lease=lease,
                    fingerprint=fingerprint,
                    stage=ModelStage.REPORT.value,
                    data=report_artifact.model_dump(mode="json"),
                    model_call_id=outcome.model_call_id,
                )
            else:
                report_artifact = ReportArtifact.model_validate(report_data)
                if not report_artifact.content:
                    raise ValueError("invalid persisted report checkpoint")
            is_demo = self._is_demo(task_id)
            self.repository.save_report(
                task_id,
                report_artifact.content,
                is_demo=is_demo,
                evidence_ids=report_artifact.evidence_ids,
                lease=lease,
            )
            return TaskRunResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                is_demo=is_demo,
                report=report_artifact.content,
            )
        except BudgetExceeded as exc:
            try:
                self.repository.fail_budget_exhausted(
                    task_id,
                    lease=lease,
                    dimension=exc.dimension,
                    active_step_id=active_step_id,
                )
            except StaleJobLease as stale:
                raise ExecutionInterrupted("job lease was lost") from stale
            return TaskRunResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                is_demo=self._is_demo(task_id),
            )
        except ExecutionInterrupted:
            raise
        except StaleJobLease as exc:
            raise ExecutionInterrupted("job lease was lost") from exc
        except ProviderFailure:
            raise
        except Exception as exc:
            try:
                self.ledger.record_error(
                    task_id, type(exc).__name__, str(exc), lease=lease
                )
            except StaleJobLease as stale:
                raise ExecutionInterrupted("job lease was lost") from stale
            raise

    async def _invoke_model(
        self,
        task_id: str,
        lease: JobLease,
        budget: TaskBudget,
        stage: ModelStage,
        operation: Callable[[], Awaitable[tuple[T, ModelResponse]]],
    ) -> _ModelOutcome[T]:
        budget.check_deadline()
        budget.check_model_call()
        try:
            value, response = await operation()
        except ProviderFailure as exc:
            router = getattr(
                {
                    ModelStage.TASK_PARSE: self.parser,
                    ModelStage.PLAN: self.planner,
                    ModelStage.CRITIC: self.critic,
                    ModelStage.REPORT: self.reporter,
                }[stage],
                "router",
            )
            provider_name = FIXED_PROVIDER[stage]
            provider = router.providers.get(provider_name)
            self.ledger.record_model_error(
                task_id,
                stage,
                provider=provider_name,
                model=str(getattr(provider, "model", "unknown")),
                error_code=exc.code.value,
                request_id=exc.request_id,
                lease=lease,
            )
            budget.consume_call()
            raise
        self._ensure_running(task_id, None)
        model_call_id = self.ledger.record_model_response(
            task_id, stage, response, lease=lease
        )
        budget.consume_call()
        budget.consume_tokens(response.prompt_tokens, response.completion_tokens)
        return _ModelOutcome(value, model_call_id)

    @staticmethod
    def _successful_stage(
        stages: dict[str, Any], stage: ModelStage
    ) -> dict[str, Any] | None:
        value = stages.get(stage.value)
        if not isinstance(value, dict) or value.get("status") != "success":
            return None
        data = value.get("data")
        return data if isinstance(data, dict) else None

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

    def _is_demo(self, task_id: str) -> bool:
        return any(call["is_demo"] for call in self.ledger.snapshot(task_id)["model_calls"])

    @staticmethod
    def _update_runtime(
        tool_name: str, result: ToolResult, runtime: dict[str, Any]
    ) -> None:
        if tool_name == "http_fetch" and result.evidence:
            runtime["http_response"] = result.evidence[0].get("metadata", {})

    @staticmethod
    def _task_fingerprint(task: TaskRead, workspace: Path) -> str:
        digest = hashlib.sha256(
            json.dumps(
                task.model_dump(
                    mode="json", exclude={"status", "is_demo", "scene"}
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        metadata_path = workspace / "upload.json"
        if metadata_path.is_file():
            metadata_bytes = metadata_path.read_bytes()
            digest.update(metadata_bytes)
            try:
                metadata = json.loads(metadata_bytes)
                candidate = (workspace / "uploads" / metadata["stored_name"]).resolve()
                upload_root = (workspace / "uploads").resolve()
                if candidate.is_file() and candidate.is_relative_to(upload_root):
                    if candidate.stat().st_size <= 10 * 1024 * 1024:
                        digest.update(candidate.read_bytes())
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                digest.update(b"invalid-upload-metadata")
        return digest.hexdigest()

    @staticmethod
    def _resolve_params(params: dict, workspace: Path, runtime: dict) -> dict:
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
