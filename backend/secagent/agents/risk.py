from dataclasses import dataclass

from secagent.domain import RiskLevel, SafetyMode


class RiskRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskDecision:
    action: str
    reason: str


POLICY_MATRIX: dict[SafetyMode, dict[RiskLevel, str]] = {
    SafetyMode.CONSERVATIVE: {
        RiskLevel.LOW: "execute",
        RiskLevel.MEDIUM: "wait",
        RiskLevel.HIGH: "reject",
        RiskLevel.FORBIDDEN: "reject",
    },
    SafetyMode.STANDARD: {
        RiskLevel.LOW: "execute",
        RiskLevel.MEDIUM: "wait",
        RiskLevel.HIGH: "reject",
        RiskLevel.FORBIDDEN: "reject",
    },
    SafetyMode.EXPERT: {
        RiskLevel.LOW: "execute",
        RiskLevel.MEDIUM: "wait",
        RiskLevel.HIGH: "wait",
        RiskLevel.FORBIDDEN: "reject",
    },
}

POLICY_REASONS: dict[SafetyMode, dict[RiskLevel, str]] = {
    SafetyMode.CONSERVATIVE: {
        RiskLevel.LOW: "保守模式允许自动执行低风险、被动、只读工具。",
        RiskLevel.MEDIUM: "保守模式下，中风险工具必须经过人工确认后才能执行。",
        RiskLevel.HIGH: "保守模式禁止执行高风险动作。",
        RiskLevel.FORBIDDEN: "禁止动作无论何种安全策略都不允许执行。",
    },
    SafetyMode.STANDARD: {
        RiskLevel.LOW: "标准模式允许自动执行低风险工具。",
        RiskLevel.MEDIUM: "标准模式下，中风险验证类动作必须经过人工确认后才能执行。",
        RiskLevel.HIGH: "标准模式暂不允许执行高风险动作。",
        RiskLevel.FORBIDDEN: "禁止动作无论何种安全策略都不允许执行。",
    },
    SafetyMode.EXPERT: {
        RiskLevel.LOW: "专家模式允许自动执行低风险工具。",
        RiskLevel.MEDIUM: "专家模式下，中风险动作仍需人工确认并记录理由。",
        RiskLevel.HIGH: "专家模式下，高风险动作必须经过人工强确认后才能执行。",
        RiskLevel.FORBIDDEN: "禁止动作无论何种安全策略都不允许执行。",
    },
}


class RiskGate:
    def check(
        self,
        level: RiskLevel,
        approved: bool,
        safety_mode: SafetyMode = SafetyMode.CONSERVATIVE,
    ) -> RiskDecision:
        action = POLICY_MATRIX[safety_mode][level]
        reason = POLICY_REASONS[safety_mode][level]
        if action == "reject":
            raise RiskRejected(reason)
        if action == "wait" and not approved:
            return RiskDecision("wait", reason)
        return RiskDecision("execute", reason)
