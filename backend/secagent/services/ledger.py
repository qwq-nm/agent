import json
import re
from collections.abc import Mapping
from typing import Any

from secagent.domain import ModelResponse, ModelStage, ToolResult
from secagent.repository import TaskRepository

SENSITIVE_KEYS = {"authorization", "api_key", "cookie", "password", "secret", "token"}
SECRET_PATTERN = re.compile(r"\b(?:sk|api)[-_][A-Za-z0-9_-]{4,}\b", re.IGNORECASE)


def redact_mapping(value: Any, key: str = "") -> Any:
    if any(sensitive in key.lower() for sensitive in SENSITIVE_KEYS):
        return "***REDACTED***"
    if isinstance(value, Mapping):
        return {item_key: redact_mapping(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact_mapping(item, key) for item in value]
    if isinstance(value, str):
        return SECRET_PATTERN.sub("***REDACTED***", value)
    return value


class LedgerService:
    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

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
    ) -> str:
        redacted = redact_mapping({"input_summary": input_summary})
        return self.repository.add_model_call(
            task_id=task_id,
            provider=provider,
            model=model,
            stage=stage,
            route_reason=route_reason,
            input_summary=redacted["input_summary"],
            latency_ms=latency_ms,
            is_demo=is_demo,
        )

    def record_model_response(
        self,
        task_id: str,
        stage: ModelStage,
        response: ModelResponse,
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
            latency_ms=response.latency_ms,
            is_demo=response.is_demo,
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
        content: str,
        confidence: float,
        metadata: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
    ) -> str:
        return self.repository.add_evidence(
            task_id=task_id,
            tool_call_id=tool_call_id,
            evidence_type=evidence_type,
            source=source,
            content=redact_mapping(content),
            confidence=confidence,
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
                content=str(item.get("content", "")),
                confidence=float(item.get("confidence", 1.0)),
                metadata=item.get("metadata", {}),
            )
        return tool_call_id

    def record_error(self, task_id: str, error_type: str, message: str) -> str:
        return self.record_evidence(
            task_id,
            evidence_type="runtime_error",
            source="agent_runner",
            content=f"{error_type}: {message}",
            confidence=1.0,
        )

    def snapshot(self, task_id: str) -> dict[str, list[dict[str, Any]]]:
        rows = self.repository.ledger_rows(task_id)
        return {
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
                }
                for row in rows["steps"]
            ],
            "model_calls": [
                {
                    "id": row.id,
                    "provider": row.provider,
                    "model": row.model,
                    "stage": row.stage,
                    "route_reason": row.route_reason,
                    "input_summary": row.input_summary,
                    "latency_ms": row.latency_ms,
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
                    "metadata": json.loads(row.metadata_json),
                }
                for row in rows["evidences"]
            ],
            "reports": [
                {"id": row.id, "content": row.content, "is_demo": row.is_demo}
                for row in rows["reports"]
            ],
        }
