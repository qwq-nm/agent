from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import ValidationError

from secagent.conversation_decomposition import (
    Capability,
    DecompositionDocument,
    LogicalProvider,
    RouteReasonCode,
)
from secagent.services.assignment_policy import (
    CAPABILITY_MATRIX,
    ROUTE_REASON_TEXT_ZH,
    AssignmentDecision,
    AssignmentPolicy,
    AssignmentPolicyError,
    CapabilityLevel,
)


def subtask(
    key: str,
    capabilities: list[Capability],
    provider: str = "glm",
    tools: list[str] | None = None,
) -> dict:
    return {
        "key": key,
        "title": key,
        "objective": f"处理 {key}",
        "dependency_keys": [],
        "required_capabilities": [item.value for item in capabilities],
        "proposed_provider": provider,
        "route_reason_code": "balanced_model_suggestion",
        "allowed_tools": tools or [],
        "expected_output": "结果",
        "required": True,
    }


def document_with(*subtasks: dict) -> DecompositionDocument:
    return DecompositionDocument.model_validate(
        {
            "plan_version": 2,
            "goal_summary": "分析任务",
            "subtasks": list(subtasks),
            "synthesis_requirements": ["说明结论依据"],
        }
    )


def assign(document: DecompositionDocument, **kwargs):
    defaults = {
        "available_providers": {"glm", "deepseek"},
        "registered_tools": set(),
        "authorized_tools": set(),
    }
    defaults.update(kwargs)
    return AssignmentPolicy().assign(document, **defaults)


def test_policy_corrects_pure_chinese_and_code_tasks_to_provider_strengths():
    decisions = assign(
        document_with(
            subtask("cn", [Capability.CHINESE_SEMANTIC], "deepseek"),
            subtask("code", [Capability.CODE_SECURITY_REASONING], "glm"),
        )
    )

    assert [(item.key, item.assigned_provider) for item in decisions] == [
        ("cn", LogicalProvider.GLM),
        ("code", LogicalProvider.DEEPSEEK),
    ]
    assert all(item.corrected for item in decisions)
    assert all(RouteReasonCode.POLICY_PREFERRED_CAPABILITY in item.correction_codes for item in decisions)


def test_fixed_capabilities_are_deepseek_only_and_tool_request_is_both_supported():
    decisions = assign(
        document_with(
            subtask("decompose", [Capability.TASK_DECOMPOSITION], "glm"),
            subtask("synthesis", [Capability.FINAL_SYNTHESIS], "glm"),
            subtask("tools", [Capability.TOOL_REQUEST], "glm"),
        )
    )

    assert [item.assigned_provider for item in decisions] == [
        LogicalProvider.DEEPSEEK,
        LogicalProvider.DEEPSEEK,
        LogicalProvider.GLM,
    ]


def test_equal_preference_preserves_valid_model_proposal():
    decision = assign(
        document_with(
            subtask(
                "mixed",
                [Capability.CHINESE_SEMANTIC, Capability.CODE_SECURITY_REASONING],
                "deepseek",
            )
        )
    )[0]

    assert decision.assigned_provider is LogicalProvider.DEEPSEEK
    assert decision.corrected is False


def test_capability_matrix_and_reason_text_are_immutable():
    assert isinstance(CAPABILITY_MATRIX, MappingProxyType)
    assert isinstance(CAPABILITY_MATRIX[LogicalProvider.GLM], MappingProxyType)
    with pytest.raises(TypeError):
        CAPABILITY_MATRIX[LogicalProvider.GLM] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        CAPABILITY_MATRIX[LogicalProvider.GLM][Capability.CHINESE_SEMANTIC] = CapabilityLevel.UNSUPPORTED  # type: ignore[index]
    with pytest.raises(TypeError):
        ROUTE_REASON_TEXT_ZH[RouteReasonCode.BALANCED_MODEL_SUGGESTION] = "篡改"  # type: ignore[index]


def test_unavailable_proposed_provider_is_corrected_without_mutating_document():
    document = document_with(subtask("cn", [Capability.CHINESE_SEMANTIC], "glm"))
    before = document.model_dump()

    decision = assign(document, available_providers={"deepseek"})[0]

    assert decision.assigned_provider is LogicalProvider.DEEPSEEK
    assert decision.corrected is True
    assert RouteReasonCode.POLICY_PROVIDER_UNAVAILABLE in decision.correction_codes
    assert document.model_dump() == before


def test_unsupported_proposed_provider_is_corrected_with_capability_mismatch():
    decision = assign(
        document_with(subtask("fixed", [Capability.TASK_DECOMPOSITION], "glm"))
    )[0]

    assert decision.assigned_provider is LogicalProvider.DEEPSEEK
    assert RouteReasonCode.POLICY_CAPABILITY_MISMATCH in decision.correction_codes


def test_tools_are_ordered_intersection_and_filtered_tools_are_reported():
    decision = assign(
        document_with(
            subtask("tools", [Capability.TOOL_REQUEST], tools=["first", "drop", "last"])
        ),
        registered_tools={"last", "first", "extra"},
        authorized_tools={"first", "last", "drop"},
    )[0]

    assert decision.allowed_tools == ["first", "last"]
    assert RouteReasonCode.POLICY_TOOL_FILTERED in decision.correction_codes


def test_authorized_but_unregistered_tools_are_not_effective():
    decision = assign(
        document_with(subtask("tools", [Capability.TOOL_REQUEST], tools=["missing"])) ,
        registered_tools=set(),
        authorized_tools={"missing"},
    )[0]

    assert decision.allowed_tools == []
    assert RouteReasonCode.POLICY_TOOL_FILTERED in decision.correction_codes


def test_total_provider_failure_raises_safe_policy_error():
    with pytest.raises(AssignmentPolicyError, match=r"fixed.*capability") as raised:
        assign(
            document_with(subtask("fixed", [Capability.TASK_DECOMPOSITION], "glm")),
            available_providers={"glm"},
        )

    assert raised.value.key == "fixed"
    assert raised.value.code is RouteReasonCode.POLICY_CAPABILITY_MISMATCH
    assert "glm" not in str(raised.value)


def test_unknown_available_providers_do_not_become_logical_assignments():
    with pytest.raises(AssignmentPolicyError, match=r"cn.*provider"):
        assign(
            document_with(subtask("cn", [Capability.CHINESE_SEMANTIC], "glm")),
            available_providers={"unknown"},
        )


def test_assignment_decision_is_strict_and_reason_is_backend_owned():
    with pytest.raises(ValidationError):
        AssignmentDecision.model_validate(
            {
                "key": "cn",
                "proposed_provider": "glm",
                "assigned_provider": "glm",
                "route_reason_code": "balanced_model_suggestion",
                "route_reason": "模型说应该这样",
                "allowed_tools": [],
                "corrected": False,
                "correction_codes": [],
                "extra": True,
            }
        )


def test_assignment_decision_accepts_enum_values_as_wire_strings():
    code = RouteReasonCode.GLM_CHINESE_STRENGTH
    decision = AssignmentDecision.model_validate(
        {
            "key": "cn",
            "proposed_provider": "glm",
            "assigned_provider": "glm",
            "route_reason_code": code.value,
            "route_reason": ROUTE_REASON_TEXT_ZH[code],
            "allowed_tools": [],
            "corrected": True,
            "correction_codes": [RouteReasonCode.POLICY_PREFERRED_CAPABILITY.value],
        }
    )

    assert decision.correction_codes == [RouteReasonCode.POLICY_PREFERRED_CAPABILITY]


def test_assignment_never_touches_provider_or_model_objects():
    class ExplodingProvider:
        def __getattr__(self, name):
            raise AssertionError(f"provider method called: {name}")

    document = document_with(subtask("cn", [Capability.CHINESE_SEMANTIC]))
    _provider = ExplodingProvider()
    decisions = AssignmentPolicy().assign(
        document,
        available_providers={"glm", "deepseek"},
        registered_tools=set(),
        authorized_tools=set(),
    )

    assert decisions[0].assigned_provider is LogicalProvider.GLM
