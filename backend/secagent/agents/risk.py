from dataclasses import dataclass

from secagent.domain import RiskLevel


class RiskRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskDecision:
    action: str
    reason: str


class RiskGate:
    def check(self, level: RiskLevel, approved: bool) -> RiskDecision:
        if level in {RiskLevel.HIGH, RiskLevel.FORBIDDEN}:
            raise RiskRejected(f"risk level {level.value} is not executable in MVP")
        if level is RiskLevel.MEDIUM and not approved:
            return RiskDecision("wait", "medium-risk action requires approval")
        return RiskDecision("execute", "risk policy satisfied")
