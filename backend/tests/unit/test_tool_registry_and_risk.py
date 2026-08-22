import pytest

from secagent.agents.risk import RiskGate, RiskRejected
from secagent.domain import RiskLevel, SafetyMode, ToolResult
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


def test_risk_gate_conservative_blocks_high() -> None:
    gate = RiskGate()

    assert (
        gate.check(
            RiskLevel.MEDIUM,
            approved=False,
            safety_mode=SafetyMode.CONSERVATIVE,
        ).action
        == "wait"
    )
    with pytest.raises(RiskRejected, match="保守模式禁止执行高风险动作"):
        gate.check(
            RiskLevel.HIGH,
            approved=True,
            safety_mode=SafetyMode.CONSERVATIVE,
        )


def test_risk_gate_standard_waits_for_medium_and_blocks_high() -> None:
    gate = RiskGate()

    decision = gate.check(
        RiskLevel.MEDIUM,
        approved=False,
        safety_mode=SafetyMode.STANDARD,
    )

    assert decision.action == "wait"
    assert "标准模式" in decision.reason
    with pytest.raises(RiskRejected, match="标准模式暂不允许执行高风险动作"):
        gate.check(
            RiskLevel.HIGH,
            approved=True,
            safety_mode=SafetyMode.STANDARD,
        )


def test_risk_gate_expert_waits_for_high_until_approved() -> None:
    gate = RiskGate()

    waiting = gate.check(
        RiskLevel.HIGH,
        approved=False,
        safety_mode=SafetyMode.EXPERT,
    )
    approved = gate.check(
        RiskLevel.HIGH,
        approved=True,
        safety_mode=SafetyMode.EXPERT,
    )

    assert waiting.action == "wait"
    assert "人工强确认" in waiting.reason
    assert approved.action == "execute"


@pytest.mark.parametrize(
    "mode",
    [
        SafetyMode.CONSERVATIVE,
        SafetyMode.STANDARD,
        SafetyMode.EXPERT,
    ],
)
def test_risk_gate_forbidden_is_always_rejected(mode: SafetyMode) -> None:
    gate = RiskGate()

    with pytest.raises(RiskRejected, match="禁止动作"):
        gate.check(RiskLevel.FORBIDDEN, approved=True, safety_mode=mode)
