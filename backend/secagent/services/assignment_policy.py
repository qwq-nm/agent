from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from secagent.conversation_decomposition import (
    Capability,
    DecompositionDocument,
    LogicalProvider,
    RouteReasonCode,
)

CapabilityLevel = StrEnum(
    "CapabilityLevel",
    {
        "UNSUPPORTED": "unsupported",
        "SUPPORTED": "supported",
        "PREFERRED": "preferred",
        "FIXED": "fixed",
    },
)


def _immutable_matrix() -> Mapping[LogicalProvider, Mapping[Capability, CapabilityLevel]]:
    glm = {
        Capability.CHINESE_SEMANTIC: CapabilityLevel.PREFERRED,
        Capability.LONG_DOCUMENT_SUMMARIZATION: CapabilityLevel.PREFERRED,
        Capability.CLASSIFICATION_EXTRACTION: CapabilityLevel.PREFERRED,
        Capability.TASK_DECOMPOSITION: CapabilityLevel.UNSUPPORTED,
        Capability.CODE_SECURITY_REASONING: CapabilityLevel.SUPPORTED,
        Capability.REVERSE_CAUSAL_ANALYSIS: CapabilityLevel.UNSUPPORTED,
        Capability.EVIDENCE_CONFLICT_RESOLUTION: CapabilityLevel.SUPPORTED,
        Capability.FINAL_SYNTHESIS: CapabilityLevel.UNSUPPORTED,
        Capability.TOOL_REQUEST: CapabilityLevel.SUPPORTED,
    }
    deepseek = {
        Capability.CHINESE_SEMANTIC: CapabilityLevel.SUPPORTED,
        Capability.LONG_DOCUMENT_SUMMARIZATION: CapabilityLevel.SUPPORTED,
        Capability.CLASSIFICATION_EXTRACTION: CapabilityLevel.SUPPORTED,
        Capability.TASK_DECOMPOSITION: CapabilityLevel.FIXED,
        Capability.CODE_SECURITY_REASONING: CapabilityLevel.PREFERRED,
        Capability.REVERSE_CAUSAL_ANALYSIS: CapabilityLevel.PREFERRED,
        Capability.EVIDENCE_CONFLICT_RESOLUTION: CapabilityLevel.PREFERRED,
        Capability.FINAL_SYNTHESIS: CapabilityLevel.FIXED,
        Capability.TOOL_REQUEST: CapabilityLevel.SUPPORTED,
    }
    return MappingProxyType(
        {
            LogicalProvider.GLM: MappingProxyType(glm),
            LogicalProvider.DEEPSEEK: MappingProxyType(deepseek),
        }
    )


CAPABILITY_MATRIX = _immutable_matrix()


ROUTE_REASON_TEXT_ZH = MappingProxyType(
    {
        RouteReasonCode.GLM_CHINESE_STRENGTH: "中文语义任务优先使用 GLM。",
        RouteReasonCode.GLM_LONG_DOCUMENT_STRENGTH: "长文档摘要任务优先使用 GLM。",
        RouteReasonCode.GLM_EXTRACTION_STRENGTH: "分类与字段提取任务优先使用 GLM。",
        RouteReasonCode.DEEPSEEK_DECOMPOSITION_FIXED: "任务分解固定使用 DeepSeek。",
        RouteReasonCode.DEEPSEEK_CODE_SECURITY_STRENGTH: "代码与安全推理任务优先使用 DeepSeek。",
        RouteReasonCode.DEEPSEEK_REVERSE_CAUSAL_STRENGTH: "逆向与因果分析任务优先使用 DeepSeek。",
        RouteReasonCode.DEEPSEEK_EVIDENCE_CONFLICT_STRENGTH: "证据冲突分析任务优先使用 DeepSeek。",
        RouteReasonCode.DEEPSEEK_SYNTHESIS_FIXED: "最终综合固定使用 DeepSeek。",
        RouteReasonCode.BALANCED_MODEL_SUGGESTION: "能力匹配均衡，保留有效的模型建议。",
        RouteReasonCode.TOOL_COMPATIBLE: "工具请求能力由兼容提供方处理。",
        RouteReasonCode.POLICY_PREFERRED_CAPABILITY: "策略按能力偏好纠正提供方。",
        RouteReasonCode.POLICY_PROVIDER_UNAVAILABLE: "模型建议的提供方当前不可用。",
        RouteReasonCode.POLICY_CAPABILITY_MISMATCH: "模型建议的提供方不支持所需能力。",
        RouteReasonCode.POLICY_TOOL_FILTERED: "工具请求已按注册与授权范围过滤。",
    }
)


_POLICY_CODES = frozenset(
    {
        RouteReasonCode.POLICY_PREFERRED_CAPABILITY,
        RouteReasonCode.POLICY_PROVIDER_UNAVAILABLE,
        RouteReasonCode.POLICY_CAPABILITY_MISMATCH,
        RouteReasonCode.POLICY_TOOL_FILTERED,
    }
)
CorrectionCodeInput = Annotated[RouteReasonCode, Field(strict=False)]


class AssignmentPolicyError(ValueError):
    """A safe, deterministic assignment failure identified by subtask key/code."""

    def __init__(self, key: str, code: RouteReasonCode):
        if code not in _POLICY_CODES:
            raise ValueError("invalid assignment policy error code")
        self.key = key
        self.code = code
        super().__init__(f"assignment policy error: {key} ({code.value})")


class AssignmentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str
    proposed_provider: LogicalProvider = Field(strict=False)
    assigned_provider: LogicalProvider = Field(strict=False)
    route_reason_code: RouteReasonCode = Field(strict=False)
    route_reason: str
    allowed_tools: list[str]
    corrected: bool
    correction_codes: list[CorrectionCodeInput]

    @field_validator("correction_codes")
    @classmethod
    def require_policy_codes(
        cls, value: list[RouteReasonCode]
    ) -> list[RouteReasonCode]:
        if any(code not in _POLICY_CODES for code in value):
            raise ValueError("correction_codes must contain policy codes")
        if len(value) != len(set(value)):
            raise ValueError("correction_codes must be unique")
        return value

    @model_validator(mode="after")
    def require_backend_reason(self) -> AssignmentDecision:
        if self.corrected != bool(self.correction_codes):
            raise ValueError("corrected must match correction_codes")
        if self.route_reason != ROUTE_REASON_TEXT_ZH[self.route_reason_code]:
            raise ValueError("route_reason must be backend-owned text")
        return self


_CAPABILITY_ROUTE_CODES: Mapping[LogicalProvider, Mapping[Capability, RouteReasonCode]] = MappingProxyType(
    {
        LogicalProvider.GLM: MappingProxyType(
            {
                Capability.CHINESE_SEMANTIC: RouteReasonCode.GLM_CHINESE_STRENGTH,
                Capability.LONG_DOCUMENT_SUMMARIZATION: RouteReasonCode.GLM_LONG_DOCUMENT_STRENGTH,
                Capability.CLASSIFICATION_EXTRACTION: RouteReasonCode.GLM_EXTRACTION_STRENGTH,
            }
        ),
        LogicalProvider.DEEPSEEK: MappingProxyType(
            {
                Capability.TASK_DECOMPOSITION: RouteReasonCode.DEEPSEEK_DECOMPOSITION_FIXED,
                Capability.CODE_SECURITY_REASONING: RouteReasonCode.DEEPSEEK_CODE_SECURITY_STRENGTH,
                Capability.REVERSE_CAUSAL_ANALYSIS: RouteReasonCode.DEEPSEEK_REVERSE_CAUSAL_STRENGTH,
                Capability.EVIDENCE_CONFLICT_RESOLUTION: RouteReasonCode.DEEPSEEK_EVIDENCE_CONFLICT_STRENGTH,
                Capability.FINAL_SYNTHESIS: RouteReasonCode.DEEPSEEK_SYNTHESIS_FIXED,
            }
        ),
    }
)


class AssignmentPolicy:
    """Pure deterministic provider and tool assignment over a validated plan."""

    def assign(
        self,
        document: DecompositionDocument,
        *,
        available_providers: Iterable[LogicalProvider | str],
        registered_tools: Iterable[str],
        authorized_tools: Iterable[str],
    ) -> list[AssignmentDecision]:
        if not isinstance(document, DecompositionDocument):
            raise TypeError("document must be a DecompositionDocument")

        available = frozenset(
            provider
            for provider in (self._provider(value) for value in available_providers)
            if provider is not None
        )
        registered = frozenset(registered_tools)
        authorized = frozenset(authorized_tools)
        decisions: list[AssignmentDecision] = []
        for subtask in document.subtasks:
            decisions.append(
                self._assign_subtask(
                    subtask,
                    available=available,
                    registered=registered,
                    authorized=authorized,
                )
            )
        return decisions

    @staticmethod
    def _provider(value: LogicalProvider | str) -> LogicalProvider | None:
        if isinstance(value, LogicalProvider):
            return value
        try:
            return LogicalProvider(value)
        except (TypeError, ValueError):
            return None

    def _assign_subtask(
        self,
        subtask,
        *,
        available: frozenset[LogicalProvider],
        registered: frozenset[str],
        authorized: frozenset[str],
    ) -> AssignmentDecision:
        capabilities = tuple(subtask.required_capabilities)
        candidates = [
            provider
            for provider in (LogicalProvider.DEEPSEEK, LogicalProvider.GLM)
            if provider in available
            and all(
                CAPABILITY_MATRIX[provider][capability]
                is not CapabilityLevel.UNSUPPORTED
                for capability in capabilities
            )
        ]
        proposed = subtask.proposed_provider
        if not candidates:
            code = (
                RouteReasonCode.POLICY_PROVIDER_UNAVAILABLE
                if proposed not in available
                else RouteReasonCode.POLICY_CAPABILITY_MISMATCH
            )
            raise AssignmentPolicyError(subtask.key, code)

        scores = {
            provider: sum(
                CAPABILITY_MATRIX[provider][capability]
                in (CapabilityLevel.PREFERRED, CapabilityLevel.FIXED)
                for capability in capabilities
            )
            for provider in candidates
        }
        highest = max(scores.values(), default=0)
        best = [provider for provider in candidates if scores[provider] == highest]
        if proposed in best:
            assigned = proposed
        else:
            assigned = best[0]

        corrections: list[RouteReasonCode] = []
        if proposed not in available:
            corrections.append(RouteReasonCode.POLICY_PROVIDER_UNAVAILABLE)
        elif proposed not in candidates:
            corrections.append(RouteReasonCode.POLICY_CAPABILITY_MISMATCH)
        elif assigned != proposed:
            corrections.append(RouteReasonCode.POLICY_PREFERRED_CAPABILITY)

        effective_tools = [
            tool
            for tool in subtask.allowed_tools
            if tool in registered and tool in authorized
        ]
        if len(effective_tools) != len(subtask.allowed_tools):
            corrections.append(RouteReasonCode.POLICY_TOOL_FILTERED)

        route_reason_code = self._route_reason_code(assigned, capabilities)
        return AssignmentDecision(
            key=subtask.key,
            proposed_provider=proposed,
            assigned_provider=assigned,
            route_reason_code=route_reason_code,
            route_reason=ROUTE_REASON_TEXT_ZH[route_reason_code],
            allowed_tools=effective_tools,
            corrected=bool(corrections),
            correction_codes=corrections,
        )

    @staticmethod
    def _route_reason_code(
        provider: LogicalProvider, capabilities: tuple[Capability, ...]
    ) -> RouteReasonCode:
        for capability in capabilities:
            code = _CAPABILITY_ROUTE_CODES.get(provider, {}).get(capability)
            if code is not None:
                return code
        if Capability.TOOL_REQUEST in capabilities:
            return RouteReasonCode.TOOL_COMPATIBLE
        return RouteReasonCode.BALANCED_MODEL_SUGGESTION
