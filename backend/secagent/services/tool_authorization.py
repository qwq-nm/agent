"""Deterministic backend-derived conversation tool authorization.

There is no per-conversation tool allowlist in ConversationSettings, so the
authorized set is a backend fact derived from the registry: every registered
tool is authorizable at conversation level. How a tool may run (auto vs.
approval vs. never) is decided exclusively by RiskGate under the
conversation's safety mode — the two layers must not duplicate each other or
medium tools could never reach their mandatory approval step. The model never
widens the set; the coordinator input, the persisted allowed_tools
intersection and the ToolGateway re-check all consume this one function.
"""

from __future__ import annotations

from secagent.domain import SafetyMode
from secagent.tools.registry import ToolRegistry


def derive_authorized_tools(
    safety_mode: SafetyMode | str, registry: ToolRegistry
) -> frozenset[str]:
    del safety_mode  # risk policy is RiskGate's job, not the allowlist's
    return frozenset(spec["name"] for spec in registry.describe())


__all__ = ["derive_authorized_tools"]
