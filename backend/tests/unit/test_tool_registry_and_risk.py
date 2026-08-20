import pytest

from secagent.agents.risk import RiskGate, RiskRejected
from secagent.domain import RiskLevel, ToolResult
from secagent.tools.base import BaseTool, ToolContext
from secagent.tools.registry import ToolRegistry


class EchoTool(BaseTool):
    name = "echo"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, summary=params["text"])


@pytest.mark.asyncio
async def test_registry_executes_only_allowlisted_scene_tool(tmp_path) -> None:
    registry = ToolRegistry([EchoTool()])
    context = ToolContext("t1", "incident_response", tmp_path)
    result = await registry.execute("echo", {"text": "ok"}, context)
    assert result.summary == "ok"
    with pytest.raises(KeyError):
        await registry.execute("missing", {}, context)


def test_risk_gate_rejects_forbidden_and_waits_for_medium() -> None:
    gate = RiskGate()
    assert gate.check(RiskLevel.LOW, approved=False).action == "execute"
    assert gate.check(RiskLevel.MEDIUM, approved=False).action == "wait"
    with pytest.raises(RiskRejected):
        gate.check(RiskLevel.FORBIDDEN, approved=True)
