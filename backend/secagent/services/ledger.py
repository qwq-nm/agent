import json
import hashlib
import re
from pathlib import Path
from typing import Any

from secagent.domain import (
    ModelResponse,
    ModelStage,
    ToolResult,
    normalize_finish_reason,
    normalize_token_count,
)
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_mapping, scrub_approval_reason


ROUTE_REASONS = {
    "中文任务理解",
    "技术计划生成",
    "证据完整性复核",
    "中文报告生成",
    "fixed_stage",
}
ERROR_CODES = {
    "auth",
    "rate_limit",
    "server",
    "timeout",
    "network",
    "empty_content",
    "truncated",
    "invalid_json",
    "invalid_schema",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def _safe_identifier(value: object, fallback: str | None = None) -> str | None:
    if isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value):
        return value
    return fallback


class LedgerService:
    def __init__(
        self,
        repository: TaskRepository,
        *,
        data_dir: Path | None = None,
        evidence_file_max_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.repository = repository
        self.data_dir = data_dir
        self.evidence_file_max_bytes = evidence_file_max_bytes

    def record_model_call(
        self,
        task_id: str,
        *,
        provider: str,
        stage: str,
        route_reason: str,
        input_summary: str,
        is_demo: bool,
        model: str = "unknown",
        latency_ms: int = 0,
        request_id: str | None = None,
        finish_reason: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        retry_count: int = 0,
        status: str = "completed",
        error_code: str | None = None,
        lease: Any | None = None,
    ) -> str:
        del input_summary  # Never persist prompts, reasoning, or request bodies.
        safe_stage = stage if stage in {item.value for item in ModelStage} else "unknown"
        safe_route_reason = (
            route_reason if route_reason in ROUTE_REASONS else "fixed_stage"
        )
        safe_status = status if status in {"completed", "error"} else "error"
        safe_error = error_code if error_code in ERROR_CODES else None
        attempt = int(getattr(lease, "attempt", 1))
        return self.repository.add_model_call(
            lease=lease,
            task_id=task_id,
            attempt=attempt,
            provider=_safe_identifier(provider, "unknown") or "unknown",
            model=_safe_identifier(model, "unknown") or "unknown",
            stage=safe_stage,
            route_reason=safe_route_reason[:64],
            input_summary=f"{safe_stage} structured request",
            request_id=_safe_identifier(request_id),
            finish_reason=normalize_finish_reason(finish_reason),
            prompt_tokens=normalize_token_count(prompt_tokens),
            completion_tokens=normalize_token_count(completion_tokens),
            retry_count=normalize_token_count(retry_count),
            latency_ms=normalize_token_count(latency_ms),
            is_demo=is_demo,
            status=safe_status,
            error_code=safe_error,
        )

    def record_model_response(
        self,
        task_id: str,
        stage: ModelStage,
        response: ModelResponse,
        *,
        lease: Any | None = None,
    ) -> str:
        reasons = {
            ModelStage.TASK_PARSE: "中文任务理解",
            ModelStage.PLAN: "技术计划生成",
            ModelStage.CRITIC: "证据完整性复核",
            ModelStage.REPORT: "中文报告生成",
        }
        return self.record_model_call(
            task_id,
            provider=response.provider,
            model=response.model,
            stage=stage.value,
            route_reason=reasons[stage],
            input_summary=f"{stage.value} structured request",
            request_id=response.request_id,
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            retry_count=response.retry_count,
            latency_ms=response.latency_ms,
            is_demo=response.is_demo,
            lease=lease,
        )

    def record_model_error(
        self,
        task_id: str,
        stage: ModelStage,
        *,
        provider: str,
        model: str,
        error_code: str,
        request_id: str | None,
        lease: Any | None = None,
    ) -> str:
        return self.record_model_call(
            task_id,
            provider=provider,
            model=model,
            stage=stage.value,
            route_reason="fixed_stage",
            input_summary="",
            request_id=request_id,
            is_demo=False,
            status="error",
            error_code=error_code,
            lease=lease,
        )

    def record_tool_call(
        self,
        task_id: str,
        *,
        tool_name: str,
        params: dict[str, Any],
        result: dict[str, Any],
        step_id: str | None = None,
    ) -> str:
        return self.repository.add_tool_call(
            task_id=task_id,
            step_id=step_id,
            tool_name=tool_name,
            params_json=json.dumps(redact_mapping(params), ensure_ascii=False),
            result_json=json.dumps(redact_mapping(result), ensure_ascii=False),
            status="completed",
        )

    def record_evidence(
        self,
        task_id: str,
        *,
        evidence_type: str,
        source: str,
        content: Any,
        confidence: float,
        metadata: dict[str, Any] | None = None,
        file_ref: str | None = None,
        tool_call_id: str | None = None,
        lease: Any | None = None,
    ) -> str:
        prepared = self._prepare_evidence(task_id, content, file_ref)
        return self.repository.add_evidence(
            lease=lease,
            task_id=task_id,
            tool_call_id=tool_call_id,
            evidence_type=evidence_type,
            source=source,
            content=prepared["content"],
            sha256=prepared["sha256"],
            confidence=confidence,
            file_ref=prepared["file_ref"],
            metadata_json=json.dumps(redact_mapping(metadata or {}), ensure_ascii=False),
        )

    def record_tool_result(
        self,
        task_id: str,
        step_id: str,
        tool_name: str,
        params: dict[str, Any],
        result: ToolResult,
    ) -> str:
        tool_call_id = self.record_tool_call(
            task_id,
            step_id=step_id,
            tool_name=tool_name,
            params=params,
            result=result.model_dump(mode="json"),
        )
        for item in result.evidence:
            self.record_evidence(
                task_id,
                tool_call_id=tool_call_id,
                evidence_type=item.get("evidence_type", "observation"),
                source=item.get("source", tool_name),
                content=item.get("content", ""),
                confidence=float(item.get("confidence", 1.0)),
                metadata=item.get("metadata", {}),
                file_ref=item.get("file_ref"),
            )
        return tool_call_id

    def record_step_result(
        self,
        lease: Any,
        *,
        step_id: str,
        tool_name: str,
        params: dict[str, Any],
        result: ToolResult,
    ) -> str:
        """Persist a tool outcome, its evidence, and the step result atomically."""
        safe_params = redact_mapping(params)
        safe_result = redact_mapping(result.model_dump(mode="json"))
        safe_evidence: list[dict[str, Any]] = []
        for item in result.evidence:
            redacted = redact_mapping(item)
            metadata = redacted.get("metadata", {})
            prepared = self._prepare_evidence(
                lease.task_id,
                redacted.get("content", ""),
                item.get("file_ref"),
            )
            safe_evidence.append(
                {
                    "evidence_type": str(
                        redacted.get("evidence_type", "observation")
                    ),
                    "source": str(redacted.get("source", tool_name)),
                    "content": prepared["content"],
                    "sha256": prepared["sha256"],
                    "file_ref": prepared["file_ref"],
                    "confidence": float(redacted.get("confidence", 1.0)),
                    "metadata": metadata if isinstance(metadata, dict) else {},
                }
            )
        return self.repository.persist_step_result(
            lease,
            step_id=step_id,
            tool_name=tool_name,
            params=safe_params,
            result=safe_result,
            evidence=safe_evidence,
        )

    @staticmethod
    def evidence_hashes(result: ToolResult) -> list[str]:
        return [
            hashlib.sha256(
                json.dumps(
                    redact_mapping(item.get("content", "")),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            for item in result.evidence
        ]

    def _prepare_evidence(
        self, task_id: str, content: Any, file_ref: object
    ) -> dict[str, str | None]:
        safe_content = redact_mapping(content)
        canonical = json.dumps(
            safe_content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        stored_content = (
            safe_content if isinstance(safe_content, str) else canonical.decode("utf-8")
        )
        stored_ref: str | None = None
        referenced_bytes = b""
        if file_ref is not None:
            if not isinstance(file_ref, str) or not file_ref:
                raise ValueError("invalid evidence file reference")
            if self.data_dir is None:
                raise ValueError("evidence file requires a safe upload root")
            upload_root = (self.data_dir / "tasks" / task_id / "uploads").resolve()
            raw_path = Path(file_ref)
            candidate = (
                raw_path.resolve()
                if raw_path.is_absolute()
                else (upload_root / raw_path).resolve()
            )
            if not candidate.is_relative_to(upload_root):
                raise ValueError("evidence file must stay within the task upload root")
            if not candidate.is_file():
                raise ValueError("evidence file does not exist")
            size = candidate.stat().st_size
            if size > self.evidence_file_max_bytes:
                raise ValueError("evidence file exceeds size limit")
            referenced_bytes = candidate.read_bytes()
            stored_ref = candidate.relative_to(upload_root).as_posix()
        return {
            "content": str(stored_content),
            "file_ref": stored_ref,
            "sha256": hashlib.sha256(canonical + referenced_bytes).hexdigest(),
        }

    def record_error(
        self,
        task_id: str,
        error_type: str,
        message: str,
        *,
        lease: Any,
    ) -> str:
        content = redact_mapping(f"{error_type}: {message}")
        canonical = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.repository.add_error_evidence(
            lease,
            task_id=task_id,
            tool_call_id=None,
            evidence_type="runtime_error",
            source="agent_runner",
            content=content,
            sha256=hashlib.sha256(canonical).hexdigest(),
            confidence=1.0,
            metadata_json="{}",
        )

    def snapshot(self, task_id: str) -> dict[str, Any]:
        rows = self.repository.ledger_rows(task_id)
        runtime = self.repository.task_runtime(task_id)
        task = self.repository.get_task(task_id)
        plan_preview = self._plan_preview(task_id)
        current_stage = self._current_stage(task, rows, plan_preview)
        approvals = [
            {
                "id": row.id,
                "step_id": row.step_id,
                "tool_name": row.tool_name,
                "risk_level": row.risk_level,
                "params_summary": row.params_summary,
                "status": row.status,
                "reason": (
                    scrub_approval_reason(row.reason) if row.reason is not None else None
                ),
            }
            for row in rows["approvals"]
        ]
        pending = next(
            (item for item in reversed(approvals) if item["status"] == "pending"),
            None,
        )
        return {
            **runtime,
            "current_stage": current_stage,
            "steps": [
                {
                    "id": row.id,
                    "index": row.step_index,
                    "name": row.name,
                    "purpose": row.purpose,
                    "tool_name": row.tool_name,
                    "params": json.loads(row.params_json),
                    "risk_level": row.risk_level,
                    "status": row.status,
                    "model_provider": row.model_provider,
                    "route_reason": row.route_reason,
                }
                for row in rows["steps"]
            ],
            "model_calls": [
                {
                    "id": row.id,
                    "attempt": row.attempt,
                    "provider": row.provider,
                    "model": row.model,
                    "stage": row.stage,
                    "route_reason": row.route_reason,
                    "input_summary": row.input_summary,
                    "request_id": row.request_id,
                    "finish_reason": row.finish_reason,
                    "prompt_tokens": row.prompt_tokens,
                    "completion_tokens": row.completion_tokens,
                    "retry_count": row.retry_count,
                    "latency_ms": row.latency_ms,
                    "status": row.status,
                    "error_code": row.error_code,
                    "is_demo": row.is_demo,
                }
                for row in rows["model_calls"]
            ],
            "tool_calls": [
                {
                    "id": row.id,
                    "tool_name": row.tool_name,
                    "params": json.loads(row.params_json),
                    "result": json.loads(row.result_json),
                    "status": row.status,
                }
                for row in rows["tool_calls"]
            ],
            "evidences": [
                {
                    "id": row.id,
                    "evidence_type": row.evidence_type,
                    "source": row.source,
                    "content": row.content,
                    "confidence": row.confidence,
                    "evidence_hash": row.sha256,
                    "metadata": json.loads(row.metadata_json),
                }
                for row in rows["evidences"]
            ],
            "reports": [
                {
                    "id": row.id,
                    "content": row.content,
                    "evidence_ids": json.loads(row.evidence_ids_json),
                    "is_demo": row.is_demo,
                }
                for row in rows["reports"]
            ],
            "approvals": approvals,
            "pending_approval": pending,
            "plan_preview": plan_preview,
        }

    @staticmethod
    def _current_stage(
        task: Any | None, rows: dict[str, list[Any]], plan_preview: dict[str, Any] | None
    ) -> str | None:
        step_stage = next(
            (
                row.name
                for row in reversed(rows["steps"])
                if row.status in {"running", "pending"}
            ),
            None,
        )
        if step_stage:
            return step_stage
        pending = next(
            (
                row
                for row in reversed(rows["approvals"])
                if row.status == "pending"
            ),
            None,
        )
        if pending is not None:
            return f"等待人工确认：{pending.tool_name}"
        status = task.status.value if task is not None else None
        if status == "planning":
            return "正在理解任务并生成执行计划"
        if status == "planned":
            return "执行计划已生成，等待确认开始"
        if status == "queued":
            return "任务已排队，等待 Worker 执行"
        if status == "running":
            if not rows["model_calls"]:
                return "正在启动自主决策流程"
            if plan_preview and not rows["tool_calls"]:
                return "准备调用第一批工具"
            return "正在复核证据或生成报告"
        if status == "waiting_human":
            return "等待人工确认"
        return None

    def _plan_preview(self, task_id: str) -> dict[str, Any] | None:
        task = self.repository.get_task(task_id)
        if task is None:
            return None
        checkpoint = self.repository.orchestration_checkpoint(task_id)
        stages = checkpoint.get("stages")
        if not isinstance(stages, dict):
            return None
        parsed_stage = stages.get(ModelStage.TASK_PARSE.value)
        plan_stage = stages.get(ModelStage.PLAN.value)
        if not isinstance(parsed_stage, dict) or not isinstance(plan_stage, dict):
            return None
        parsed = parsed_stage.get("data")
        plan = plan_stage.get("data")
        if not isinstance(parsed, dict) or not isinstance(plan, dict):
            return None
        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list):
            return None
        steps = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                continue
            steps.append(
                {
                    "index": index,
                    "name": raw_step.get("name"),
                    "purpose": raw_step.get("purpose"),
                    "tool_name": raw_step.get("tool_name"),
                    "params": raw_step.get("params") if isinstance(raw_step.get("params"), dict) else {},
                    "risk_level": raw_step.get("risk_level"),
                    "need_human_confirm": bool(raw_step.get("need_human_confirm")),
                }
            )
        return {
            "task_id": task.id,
            "scene": parsed.get("scene") or task.scene or task.scene_hint,
            "goal_summary": parsed.get("goal") or task.goal,
            "target_summary": task.target_url,
            "authorization_summary": parsed.get("authorization_scope")
            or task.authorization_scope,
            "safety_mode": task.safety_mode,
            "constraints": parsed.get("constraints")
            if isinstance(parsed.get("constraints"), list)
            else [],
            "expected_outputs": parsed.get("expected_outputs")
            if isinstance(parsed.get("expected_outputs"), list)
            else [],
            "steps": steps,
        }
