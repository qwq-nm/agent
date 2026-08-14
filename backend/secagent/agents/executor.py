from secagent.domain import RiskLevel, TaskStatus, ToolResult
from secagent.services.ledger import LedgerService
from secagent.tools.base import BaseTool, ToolContext
from secagent.tools.registry import ToolRegistry


class ExecutionInterrupted(RuntimeError):
    pass


class DemoEvidenceTool(BaseTool):
    name = "demo_evidence"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        return ToolResult(
            success=True,
            summary="已生成稳定的演示日志证据",
            findings=[{"kind": "suspicious_request", "severity": "medium"}],
            evidence=[
                {
                    "evidence_type": "raw_line",
                    "source": "demo:1",
                    "content": "203.0.113.10 GET /admin 401",
                    "confidence": 1.0,
                }
            ],
        )


class Executor:
    def __init__(self, registry: ToolRegistry, ledger: LedgerService) -> None:
        self.registry = registry
        self.ledger = ledger

    async def execute(
        self,
        *,
        task_id: str,
        step_id: str,
        tool_name: str,
        params: dict,
        context: ToolContext,
    ) -> ToolResult:
        result = await self.registry.execute(tool_name, params, context)
        task = self.ledger.repository.get_task(task_id)
        if task is None or task.status is not TaskStatus.RUNNING:
            raise ExecutionInterrupted("task execution was invalidated")
        self.ledger.record_tool_result(task_id, step_id, tool_name, params, result)
        return result
