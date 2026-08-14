import json
import hashlib
from typing import Any

from secagent.domain import ModelResponse, ModelStage, ToolResult
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_mapping, scrub_approval_reason


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
        lease: Any | None = None,
    ) -> str:
        redacted = redact_mapping({"input_summary": input_summary})
        return self.repository.add_model_call(
            lease=lease,
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
            latency_ms=response.latency_ms,
            is_demo=response.is_demo,
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
        content: str,
        confidence: float,
        metadata: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        lease: Any | None = None,
    ) -> str:
        redacted_content = redact_mapping(content)
        return self.repository.add_evidence(
            lease=lease,
            task_id=task_id,
            tool_call_id=tool_call_id,
            evidence_type=evidence_type,
            source=source,
            content=redacted_content,
            sha256=hashlib.sha256(redacted_content.encode("utf-8")).hexdigest(),
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
            safe_evidence.append(
                {
                    "evidence_type": str(
                        redacted.get("evidence_type", "observation")
                    ),
                    "source": str(redacted.get("source", tool_name)),
                    "content": str(redacted.get("content", "")),
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
                str(redact_mapping(str(item.get("content", "")))).encode("utf-8")
            ).hexdigest()
            for item in result.evidence
        ]

    def record_error(
        self,
        task_id: str,
        error_type: str,
        message: str,
        *,
        lease: Any | None = None,
    ) -> str:
        return self.record_evidence(
            task_id,
            evidence_type="runtime_error",
            source="agent_runner",
            content=f"{error_type}: {message}",
            confidence=1.0,
            lease=lease,
        )

    def snapshot(self, task_id: str) -> dict[str, Any]:
        rows = self.repository.ledger_rows(task_id)
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
            "approvals": approvals,
            "pending_approval": pending,
        }
