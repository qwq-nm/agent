"""The single tool boundary for conversation subtasks.

Every model-requested tool call flows through: allowlist check, backend
authorization set, RiskGate, approval creation, ToolRegistry execution and
redacted ledger persistence with turn/subtask/attempt linkage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from secagent.agents.risk import RiskGate, RiskRejected
from secagent.domain import RiskLevel, SafetyMode, ToolResult
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_mapping, redact_text
from secagent.services.ledger import LedgerService
from secagent.services.tool_authorization import derive_authorized_tools
from secagent.tools.base import ToolContext
from secagent.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ToolRequestOutcome:
    status: Literal["executed", "wait", "rejected"]
    result: ToolResult | None
    reason: str


class ToolGateway:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        ledger: LedgerService,
        dag: DagRepository,
        task_repository: TaskRepository,
        workspace: Path,
        safety_mode: SafetyMode | str,
        allowed_tools: list[str],
        task_id: str,
        turn_id: str,
        subtask_id: str,
        attempt_id: str,
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.dag = dag
        self.task_repository = task_repository
        self.workspace = workspace
        self.safety_mode = safety_mode
        self.allowed_tools = frozenset(allowed_tools)
        self.authorized_tools = derive_authorized_tools(safety_mode, registry)
        self.task_id = task_id
        self.turn_id = turn_id
        self.subtask_id = subtask_id
        self.attempt_id = attempt_id

    async def submit(
        self,
        *,
        subtask_key: str,
        tool_name: str,
        params: dict[str, Any],
    ) -> ToolRequestOutcome:
        self.dag.record_subtask_event(
            self.subtask_id,
            "subtask.tool.requested",
            {
                "subtask_id": self.subtask_id,
                "key": subtask_key,
                "tool_name": redact_text(str(tool_name)),
            },
        )

        if tool_name not in self.allowed_tools:
            return self._reject(subtask_key, tool_name, "tool is not allowed for this subtask")
        if tool_name not in self.authorized_tools:
            return self._reject(subtask_key, tool_name, "tool is not authorized for this conversation")
        try:
            tool = self.registry.get(tool_name)
        except KeyError:
            return self._reject(subtask_key, tool_name, "tool is not registered")

        risk = tool.risk_level
        try:
            decision = RiskGate().check(
                risk, approved=False, safety_mode=self.safety_mode
            )
        except RiskRejected as exc:
            return self._reject(subtask_key, tool_name, str(exc))

        if decision.action == "wait":
            approval_id = self.task_repository.add_approval(
                self.task_id,
                step_id=None,
                tool_name=tool_name,
                risk_level=risk.value,
                params_summary=redact_text(json.dumps(redact_mapping(params), ensure_ascii=False))[:2_000],
                commit=False,
                turn_id=self.turn_id,
                subtask_id=self.subtask_id,
                attempt_id=self.attempt_id,
            )
            self.task_repository.session.flush()
            self.dag.record_subtask_event(
                self.subtask_id,
                "subtask.waiting_approval",
                {
                    "subtask_id": self.subtask_id,
                    "key": subtask_key,
                    "tool_name": tool_name,
                    "approval_id": approval_id,
                    "reason": decision.reason,
                },
            )
            return ToolRequestOutcome(
                status="wait", result=None, reason=decision.reason
            )

        result = await self._execute(subtask_key, tool_name, tool.scene, params)
        return ToolRequestOutcome(
            status="executed", result=result, reason=decision.reason
        )

    # ------------------------------------------------------------------ internals

    async def _execute(
        self,
        subtask_key: str,
        tool_name: str,
        tool_scene: str,
        params: dict[str, Any],
    ) -> ToolResult:
        context = ToolContext(self.task_id, tool_scene, self.workspace)
        try:
            result: ToolResult = await self.registry.execute(
                tool_name, params, context
            )
        except Exception as exc:
            result = ToolResult(
                success=False,
                summary=f"{tool_name} 执行失败",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
        self._record(subtask_key, tool_name, params, result)
        return result

    def _record(
        self,
        subtask_key: str,
        tool_name: str,
        params: dict[str, Any],
        result: ToolResult,
    ) -> None:
        tool_call_id = self.ledger.record_tool_call(
            self.task_id,
            tool_name=tool_name,
            params=params,
            result=result.model_dump(mode="json"),
            turn_id=self.turn_id,
            subtask_id=self.subtask_id,
            attempt_id=self.attempt_id,
        )
        for item in result.evidence:
            source = str(item.get("source", tool_name))
            self.ledger.record_evidence(
                self.task_id,
                evidence_type=item.get("evidence_type", "observation"),
                source=f"{subtask_key}:{source}",
                content=item.get("content", ""),
                confidence=float(item.get("confidence", 1.0)),
                metadata=item.get("metadata", {}),
                file_ref=item.get("file_ref"),
                tool_call_id=tool_call_id,
                turn_id=self.turn_id,
                subtask_id=self.subtask_id,
                attempt_id=self.attempt_id,
            )

    def _reject(
        self, subtask_key: str, tool_name: str, reason: str
    ) -> ToolRequestOutcome:
        self.dag.record_subtask_event(
            self.subtask_id,
            "subtask.tool.rejected",
            {
                "subtask_id": self.subtask_id,
                "key": subtask_key,
                "tool_name": redact_text(str(tool_name)),
                "reason": reason,
            },
        )
        return ToolRequestOutcome(status="rejected", result=None, reason=reason)


__all__ = ["ToolGateway", "ToolRequestOutcome"]
