"""Deterministic backend-derived conversation tool authorization.

There is no per-conversation tool allowlist in ConversationSettings, so the
authorized set is a backend fact derived from the safety mode. The model never
widens it; the coordinator input, the persisted allowed_tools intersection and
the ToolGateway re-check all consume this one function.
"""

from __future__ import annotations

from secagent.domain import RiskLevel, SafetyMode
from secagent.tools.registry import ToolRegistry

_AUTHORIZED_RISKS_BY_SAFETY_MODE: dict[SafetyMode, frozenset[RiskLevel]] = {
    SafetyMode.CONSERVATIVE: frozenset({RiskLevel.LOW}),
    SafetyMode.STANDARD: frozenset({RiskLevel.LOW, RiskLevel.MEDIUM}),
    SafetyMode.EXPERT: frozenset({RiskLevel.LOW, RiskLevel.MEDIUM}),
}


def derive_authorized_tools(
    safety_mode: SafetyMode | str, registry: ToolRegistry
) -> frozenset[str]:
    mode = (
        safety_mode
        if isinstance(safety_mode, SafetyMode)
        else SafetyMode(safety_mode)
    )
    authorized_risks = _AUTHORIZED_RISKS_BY_SAFETY_MODE[mode]
    return frozenset(
        spec["name"]
        for spec in registry.describe()
        if RiskLevel(spec["risk_level"]) in authorized_risks
    )


__all__ = ["derive_authorized_tools"]
