"""Durable subtask execution: one job, one bounded model/tool loop."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secagent.conversation_domain import ConversationSettings
from secagent.dag_domain import (
    ClaimDocument,
    ModelFailureCreate,
    ModelFailureStage,
    SubtaskResultDocument,
    SubtaskStatus,
    ToolRequestDocument,
    dag_command_id,
)
from secagent.db_models import ConversationRow, ConversationTurnRow, SubtaskRow
from secagent.domain import ModelStage, SafetyMode
from secagent.providers.base import ProviderUnavailable
from secagent.providers.runtime import ProviderRuntimeFactory
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_mapping
from secagent.services.ledger import LedgerService
from secagent.services.job_service import JobLease, JobService
from secagent.services.runtime_memory import build_conversation_memory
from secagent.services.tool_gateway import ToolGateway
from secagent.subtask_prompts import build_subtask_request
from secagent.tools.registry import ToolRegistry

#: Tools addressed by a single target URL where the URL is the only meaningful
#: parameter, so duplicate detection keys on the URL alone. ``http_fetch`` is
#: deliberately excluded: its method/headers/body change the request (e.g. an
#: HTTP-header-maze challenge needs different User-Agent/Cookie/Referer), so it
#: falls back to full-params dedup.
_URL_ADDRESSED_TOOLS = frozenset(
    {
        "url_guard",
        "browser_snapshot",
        "dirsearch_scan",
        "login_probe",
        "sqlmap_probe",
    }
)

#: Cap the accumulated tool observations per subtask so the prompt stays within
#: the model's context window. Approximate compaction: once past this many, the
#: oldest observations are dropped (the most recent are the most relevant).
_MAX_TOOL_OBSERVATIONS = 40


async def execute_subtask_job(
    subtask_id: str,
    command_id: str,
    session_factory: sessionmaker[Session],
    registry: ToolRegistry,
    settings: Any,
    *,
    worker_id: str | None = None,
    queue: Any | None = None,
) -> None:
    from secagent.services.dag_scheduler import DagScheduler

    scheduler = (
        DagScheduler(
            session_factory=session_factory,
            queue=queue,
            max_parallel=settings.max_parallel_subtasks_per_conversation,
        )
        if queue is not None
        else None
    )
    runner = SubtaskRunner(
        session_factory=session_factory,
        registry=registry,
        settings=settings,
        scheduler=scheduler,
        lease_seconds=settings.job_lease_seconds,
        heartbeat_seconds=settings.job_heartbeat_seconds,
    )
    await runner.run(subtask_id, command_id, worker_id or "subtask-worker")


class SubtaskRunner:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        registry: ToolRegistry,
        settings: Any,
        scheduler: Any | None,
        lease_seconds: int = 90,
        heartbeat_seconds: int = 15,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.settings = settings
        self.scheduler = scheduler
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    async def run(
        self, subtask_id: str, command_id: str, worker_id: str
    ) -> None:
        plan = self._load_plan(subtask_id, command_id)
        if plan is None:
            return
        (
            task_id,
            turn_id,
            subtask,
            turn,
            conversation,
            budget,
        ) = plan

        claim_session = self.session_factory()
        try:
            lease = DagRepository(claim_session).claim_dag_job(
                command_id,
                worker_id,
                lease_seconds=self.lease_seconds,
            )
        except Exception:
            claim_session.close()
            raise
        if lease is None:
            claim_session.close()
            return
        try:
            await self._execute(
                subtask_id, task_id, turn_id, subtask, turn, conversation, budget, lease
            )
        finally:
            claim_session.close()

    # ------------------------------------------------------------------ internals

    def _load_plan(
        self, subtask_id: str, command_id: str
    ) -> tuple[str, str, Any, Any, Any, Any] | None:
        with self.session_factory() as session:
            dag = DagRepository(session)
            subtask = dag.get_subtask(subtask_id)
            if subtask is None:
                raise KeyError(subtask_id)
            turn = dag.require_turn(subtask.turn_id)
            if turn.task_id is None:
                raise ValueError(f"turn {turn.id} has no compatible task row")
            conversation = session.get(ConversationRow, turn.conversation_id)
            if conversation is None:  # pragma: no cover - guarded upstream
                raise KeyError(turn.conversation_id)
            from secagent.conversation_domain import TurnBudgetSnapshot

            return (
                turn.task_id,
                turn.id,
                subtask,
                turn,
                conversation,
                TurnBudgetSnapshot.model_validate_json(turn.budget_json),
            )

    async def _execute(
        self,
        subtask_id: str,
        task_id: str,
        turn_id: str,
        subtask: Any,
        turn: Any,
        conversation: Any,
        budget: Any,
        lease: JobLease,
    ) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat(lease, self.session_factory)
        )
        router = None
        try:
            with self.session_factory() as session:
                router = ProviderRuntimeFactory(self.settings).build(session)

            attempt_id = self._open_attempt(
                subtask_id, subtask.assigned_provider, lease
            )
            outcome = await self._run_loop(
                subtask_id=subtask_id,
                task_id=task_id,
                turn_id=turn_id,
                subtask=subtask,
                conversation=conversation,
                budget=budget,
                lease=lease,
                attempt_id=attempt_id,
                router=router,
            )
            with self.session_factory() as session:
                DagRepository(session).finish_dag_job(
                    lease.job_run_id, lease.worker_id, outcome
                )
        except ProviderUnavailable as exc:
            self._record_subtask_model_failure(
                subtask_id, task_id, turn_id, subtask, exc
            )
            with self.session_factory() as session:
                DagRepository(session).finish_dag_job(
                    lease.job_run_id, lease.worker_id, "failed"
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            if router is not None:
                with suppress(Exception):
                    await router.aclose()

    def _open_attempt(self, subtask_id: str, provider: str, lease: JobLease) -> str:
        with self.session_factory() as session:
            dag = DagRepository(session)
            attempt = dag.create_attempt(
                subtask_id,
                provider=provider,
                model=(
                    "deepseek-v4-flash"
                    if provider == "deepseek"
                    else "glm-5.2"
                ),
                idempotency_key=dag_command_id("subtask", subtask_id, "attempt", lease.worker_id or "w"),
                worker_id=lease.worker_id,
            )
            session.commit()
            return attempt.id

    async def _run_loop(
        self,
        *,
        subtask_id: str,
        task_id: str,
        turn_id: str,
        subtask: Any,
        conversation: Any,
        budget: Any,
        lease: JobLease,
        attempt_id: str,
        router: Any,
    ) -> str:
        settings = ConversationSettings.model_validate_json(
            conversation.settings_json
        )
        goal_summary = self._goal_summary(turn_id)
        tool_observations: list[dict[str, Any]] = []
        tool_history: set[str] = set()
        model_calls_left = budget.max_model_calls_per_subtask
        tool_calls_left = budget.max_tool_calls_per_subtask
        runtime_memory = self._conversation_memory(conversation.id)

        with self.session_factory() as session:
            dag = DagRepository(session)
            dag.transition_subtask(
                subtask_id,
                SubtaskStatus.RUNNING,
                expected={SubtaskStatus.QUEUED},
                reason="worker claimed the job",
            )
            session.commit()

        while model_calls_left > 0:
            # Compaction: keep only the most recent observations so the prompt
            # does not grow past the model's context window.
            if len(tool_observations) > _MAX_TOOL_OBSERVATIONS:
                tool_observations = tool_observations[-_MAX_TOOL_OBSERVATIONS:]
            if not self._lease_active(lease):
                return "failed"
            if not self._turn_dispatchable(turn_id):
                self._supersede(subtask_id)
                return "completed"

            with self.session_factory() as session:
                dag = DagRepository(session)
                dependencies = self._dependency_outputs(session, subtask_id)
                request = build_subtask_request(
                    subtask=subtask,
                    goal_summary=goal_summary,
                    dependency_outputs=dependencies,
                    tool_observations=tool_observations,
                    remaining_model_calls=model_calls_left,
                    remaining_tool_calls=tool_calls_left,
                    registered_tools=self.registry.describe(),
                    preferred_provider=subtask.assigned_provider,
                    runtime_memory=runtime_memory,
                )
            response = await router.complete(
                ModelStage.SUBTASK_EXECUTE, request, preferred=subtask.assigned_provider
            )
            model_calls_left -= 1

            with self.session_factory() as session:
                LedgerService(TaskRepository(session)).record_model_response(
                    task_id,
                    ModelStage.SUBTASK_EXECUTE,
                    response,
                    turn_id=turn_id,
                    subtask_id=subtask_id,
                    attempt_id=attempt_id,
                )

            safe_data = redact_mapping(response.data)
            if safe_data.get("status") == "tool_request":
                if tool_calls_left <= 0:
                    return self._finish_incomplete(
                        subtask_id, "tool budget exhausted before any tool call"
                    )
                try:
                    request_output = ToolRequestDocument.model_validate(safe_data)
                except ValidationError as exc:
                    tool_observations.append(
                        self._schema_feedback_observation(
                            "tool_request_schema",
                            "上一次工具请求格式不符合协议，工具未执行。",
                            exc,
                        )
                    )
                    continue
                signature = self._tool_signature(
                    request_output.tool_name, request_output.params
                )
                if signature in tool_history:
                    tool_observations.append(
                        self._duplicate_tool_observation(request_output.tool_name)
                    )
                    continue
                outcome = await self._submit_tool(
                    task_id=task_id,
                    turn_id=turn_id,
                    subtask=subtask,
                    attempt_id=attempt_id,
                    settings=settings,
                    tool_name=request_output.tool_name,
                    params=request_output.params,
                )
                if outcome.status == "wait":
                    with self.session_factory() as session:
                        DagRepository(session).transition_subtask(
                            subtask_id,
                            SubtaskStatus.WAITING_TOOL_APPROVAL,
                            expected={SubtaskStatus.RUNNING},
                            reason=outcome.reason,
                        )
                        session.commit()
                    return "completed"
                if outcome.status == "rejected":
                    tool_observations.append(
                        {
                            "tool_name": request_output.tool_name,
                            "summary": (
                                f"工具 {request_output.tool_name} 被安全策略拒绝："
                                f"{outcome.reason}。请改用被动/低风险替代工具，"
                                "或直接给出结论，不要重复请求被拒绝的工具。"
                            ),
                            "findings": [
                                {
                                    "kind": "tool_rejected",
                                    "description": outcome.reason,
                                }
                            ],
                        }
                    )
                    continue
                tool_history.add(signature)
                tool_calls_left -= 1
                assert outcome.result is not None
                tool_observations.append(
                    self._tool_observation(
                        request_output.tool_name, outcome.result
                    )
                )
                if (
                    request_output.tool_name == "submit_flag"
                    and outcome.result.success
                ):
                    return self._finish_flag(
                        subtask_id, attempt_id, request_output.params.get("flag")
                    )
                continue

            try:
                result = SubtaskResultDocument.model_validate(safe_data)
            except ValidationError as exc:
                tool_observations.append(
                    self._schema_feedback_observation(
                        "subtask_result_schema",
                        "上一次子任务结果格式不符合协议，结果未保存。",
                        exc,
                    )
                )
                continue  # bounded correction with the remaining model budget

            return self._finish_with_result(subtask_id, attempt_id, result)

        return self._finish_incomplete(
            subtask_id, "model call budget exhausted without a final result"
        )

    # ----------------------------------------------------------------- helpers

    def _schema_feedback_observation(
        self, name: str, summary: str, exc: ValidationError
    ) -> dict[str, Any]:
        return {
            "tool_name": name,
            "summary": summary,
            "findings": [
                {
                    "kind": "schema_error",
                    "description": str(exc)[:1_000],
                },
                {
                    "kind": "required_contract",
                    "description": (
                        "工具请求必须包含 status=tool_request、tool_name、params、"
                        "reason、expected_evidence；最终结果必须包含合法的 "
                        "status、summary、claims、evidence_refs、inference_notes、"
                        "unresolved。"
                    ),
                },
            ],
        }

    @staticmethod
    def _tool_signature(tool_name: str, params: dict[str, Any]) -> str:
        # http_fetch: key on the actual request (url/method/headers/body/query)
        # so the model can't re-fetch the same page by tweaking cosmetic flags
        # like details/include_body, while a genuinely different request (e.g.
        # a different User-Agent/Cookie/Referer) is NOT treated as a duplicate.
        if tool_name in {"http_fetch", "http_request"}:
            request = {
                key: params[key]
                for key in ("url", "method", "query", "headers", "body")
                if key in params
            }
            canonical = json.dumps(
                redact_mapping(request),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return f"{tool_name}:req:{canonical}"
        if tool_name in _URL_ADDRESSED_TOOLS:
            url = params.get("url")
            if isinstance(url, str) and url.strip():
                return f"{tool_name}:url:{url.strip()}"
        canonical = json.dumps(
            redact_mapping(params),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{tool_name}:{canonical}"

    @staticmethod
    def _duplicate_tool_observation(tool_name: str) -> dict[str, Any]:
        return {
            "tool_name": tool_name,
            "summary": (
                f"工具 {tool_name} 及相同目标已在本子任务中执行过，结果不会改变；"
                "请换一个方向继续，或直接给出结论。"
            ),
            "findings": [
                {"kind": "duplicate_tool", "description": "重复工具调用已被跳过"}
            ],
        }

    @staticmethod
    def _tool_observation(tool_name: str, result: Any) -> dict[str, Any]:
        """Bounded tool observation for the model.

        Includes the tool's evidence metadata (e.g. http_fetch's body_preview
        and headers) so the model can actually read the page content instead of
        only a "HTTP 200" summary. Truncated to keep the prompt bounded.
        """
        observation: dict[str, Any] = {
            "tool_name": tool_name,
            "summary": result.summary,
            "findings": result.findings,
        }
        evidence_view: list[dict[str, Any]] = []
        for item in result.evidence[:8]:
            entry: dict[str, Any] = {
                "content": str(item.get("content", ""))[:400],
            }
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                bounded: dict[str, Any] = {}
                for key, value in metadata.items():
                    if isinstance(value, str):
                        bounded[key] = value[:8000]
                    elif isinstance(value, dict):
                        bounded[key] = {
                            str(k)[:80]: str(v)[:400]
                            for k, v in list(value.items())[:64]
                        }
                    elif isinstance(value, list):
                        bounded[key] = [str(v)[:400] for v in value[:64]]
                    elif value is None or isinstance(value, (int, float, bool)):
                        bounded[key] = value
                    else:
                        bounded[key] = str(value)[:400]
                entry["metadata"] = bounded
            evidence_view.append(entry)
        if evidence_view:
            observation["evidence"] = evidence_view
        return observation

    async def _submit_tool(
        self,
        *,
        task_id: str,
        turn_id: str,
        subtask: Any,
        attempt_id: str,
        settings: Any,
        tool_name: str,
        params: dict[str, Any],
    ):
        with self.session_factory() as session:
            workspace = Path(self.settings.data_dir) / "tasks" / task_id
            workspace.mkdir(parents=True, exist_ok=True)
            gateway = ToolGateway(
                registry=self.registry,
                ledger=LedgerService(TaskRepository(session)),
                dag=DagRepository(session),
                task_repository=TaskRepository(session),
                workspace=workspace,
                safety_mode=ConversationSettings.model_validate_json(
                    self._settings_json(session, turn_id)
                ).safety_mode,
                allowed_tools=list(subtask.allowed_tools),
                task_id=task_id,
                turn_id=turn_id,
                subtask_id=subtask.id,
                attempt_id=attempt_id,
                auto_approve=bool(
                    getattr(self.settings, "auto_approve_tools", False)
                ),
            )
            outcome = await gateway.submit(
                subtask_key=subtask.key, tool_name=tool_name, params=params
            )
            session.commit()
            return outcome

    def _finish_with_result(
        self, subtask_id: str, attempt_id: str, result: SubtaskResultDocument
    ) -> str:
        with self.session_factory() as session:
            dag = DagRepository(session)
            dag.save_subtask_result(attempt_id, result)
            dag.finish_attempt(attempt_id, status=result.status)
            dag.transition_subtask(
                subtask_id,
                SubtaskStatus(result.status),
                expected={SubtaskStatus.RUNNING},
                reason=result.summary[:200],
            )
            session.commit()
        if self.scheduler is not None:
            self.scheduler.on_subtask_finished(subtask_id)
        return "completed"

    def _finish_incomplete(self, subtask_id: str, reason: str) -> str:
        with self.session_factory() as session:
            dag = DagRepository(session)
            dag.transition_subtask(
                subtask_id,
                SubtaskStatus.INCOMPLETE,
                expected={SubtaskStatus.RUNNING},
                reason=reason,
            )
            session.commit()
        if self.scheduler is not None:
            self.scheduler.on_subtask_finished(subtask_id)
        return "completed"

    def _finish_flag(self, subtask_id: str, attempt_id: str, flag: Any) -> str:
        flag_text = str(flag or "").strip()
        result = SubtaskResultDocument(
            status="completed",
            summary=f"已找到并提交 flag：{flag_text}",
            claims=[
                ClaimDocument(
                    statement=f"找到 flag {flag_text}",
                    evidence_ref="submit_flag:flag",
                )
            ],
        )
        return self._finish_with_result(subtask_id, attempt_id, result)

    def _supersede(self, subtask_id: str) -> None:
        with self.session_factory() as session:
            dag = DagRepository(session)
            with suppress(Exception):
                dag.transition_subtask(
                    subtask_id,
                    SubtaskStatus.SUPERSEDED,
                    expected={SubtaskStatus.RUNNING, SubtaskStatus.QUEUED},
                    reason="turn replanned or stopped",
                )
                session.commit()

    def _record_subtask_model_failure(
        self,
        subtask_id: str,
        task_id: str,
        turn_id: str,
        subtask: Any,
        exc: ProviderUnavailable,
    ) -> None:
        code = getattr(getattr(exc, "code", None), "value", "server") or "server"
        with self.session_factory() as session:
            dag = DagRepository(session)
            ledger = LedgerService(TaskRepository(session))
            ledger.record_model_error(
                task_id,
                ModelStage.SUBTASK_EXECUTE,
                provider=getattr(exc, "provider", subtask.assigned_provider),
                model=(
                    "deepseek-v4-flash"
                    if subtask.assigned_provider == "deepseek"
                    else "glm-5.2"
                ),
                error_code=code,
                request_id=getattr(exc, "request_id", None),
                turn_id=turn_id,
                subtask_id=subtask_id,
            )
            dag.record_model_failure(
                ModelFailureCreate(
                    turn_id=turn_id,
                    subtask_id=subtask_id,
                    stage=ModelFailureStage.SUBTASK,
                    provider=getattr(exc, "provider", subtask.assigned_provider),
                    model=(
                        "deepseek-v4-flash"
                        if subtask.assigned_provider == "deepseek"
                        else "glm-5.2"
                    ),
                    error_code=code,
                    detail=f"{type(exc).__name__}: {exc}"[:1_800],
                )
            )
            auto_resolve = bool(
                getattr(self.settings, "auto_resolve_model_failures", False)
            )
            if auto_resolve:
                # Autonomous mode: don't block on the operator decision; mark the
                # subtask failed (terminal) so the scheduler synthesizes a partial
                # result instead of waiting for a decision forever.
                with suppress(Exception):
                    dag.transition_subtask(
                        subtask_id,
                        SubtaskStatus.FAILED,
                        expected={SubtaskStatus.RUNNING, SubtaskStatus.QUEUED},
                        reason="provider failure auto-resolved to failed (autonomous)",
                    )
            else:
                with suppress(Exception):
                    dag.transition_subtask(
                        subtask_id,
                        SubtaskStatus.WAITING_MODEL_DECISION,
                        expected={SubtaskStatus.RUNNING, SubtaskStatus.QUEUED},
                        reason="provider failure awaiting user decision",
                    )
                with suppress(Exception):
                    dag.mark_turn_state(
                        turn_id,
                        "waiting_model_decision",
                        expected={
                            "running",
                            "scheduling",
                            "waiting_tool_approval",
                        },
                    )
            session.commit()
        if self.scheduler is not None:
            self.scheduler.on_subtask_finished(subtask_id)

    def _lease_active(self, lease: JobLease) -> bool:
        with self.session_factory() as session:
            return JobService(
                TaskRepository(session), None, lease_seconds=self.lease_seconds
            ).is_active(lease.job_run_id, lease.worker_id)

    def _turn_dispatchable(self, turn_id: str) -> bool:
        with self.session_factory() as session:
            turn = session.get(ConversationTurnRow, turn_id)
            return (
                turn is not None
                and turn.status
                in {"running", "scheduling", "waiting_tool_approval"}
            )

    def _settings_json(self, session: Session, turn_id: str) -> str:
        conversation = (
            session.query(ConversationRow)
            .join(ConversationTurnRow, ConversationTurnRow.conversation_id == ConversationRow.id)
            .filter(ConversationTurnRow.id == turn_id)
            .one()
        )
        return conversation.settings_json

    def _goal_summary(self, turn_id: str) -> str:
        with self.session_factory() as session:
            turn = session.get(ConversationTurnRow, turn_id)
            if turn is None or turn.task_id is None:
                return ""
            from secagent.db_models import TaskRow

            task = session.get(TaskRow, turn.task_id)
            return task.goal if task is not None else ""

    def _conversation_memory(self, conversation_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            return build_conversation_memory(
                session, conversation_id=conversation_id
            )

    def _dependency_outputs(
        self, session: Session, subtask_id: str
    ) -> list[dict[str, Any]]:
        from secagent.db_models import SubtaskDependencyRow

        rows = session.execute(
            select(SubtaskRow.key, SubtaskRow.result_summary)
            .join(
                SubtaskDependencyRow,
                SubtaskDependencyRow.dependency_subtask_id == SubtaskRow.id,
            )
            .where(SubtaskDependencyRow.subtask_id == subtask_id)
        ).all()
        return [
            {"key": key, "summary": summary or ""}
            for key, summary in rows
        ]

    async def _heartbeat(
        self, lease: JobLease, session_factory: sessionmaker[Session]
    ) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            with session_factory() as session:
                renewed = JobService(
                    TaskRepository(session),
                    None,
                    lease_seconds=self.lease_seconds,
                ).heartbeat(lease.job_run_id, lease.worker_id)
            if renewed is None:
                return
