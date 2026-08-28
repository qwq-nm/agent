from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from secagent.conversation_decomposition import (
    DecompositionDocument,
    validate_decomposition,
)


def valid_document() -> dict:
    return {
        "plan_version": 2,
        "goal_summary": "分析项目中的高风险问题",
        "subtasks": [
            {
                "key": "extract_context",
                "title": "提炼中文材料",
                "objective": "提取项目材料中的关键事实和约束。",
                "dependency_keys": [],
                "required_capabilities": ["chinese_semantic"],
                "proposed_provider": "glm",
                "route_reason_code": "glm_chinese_strength",
                "allowed_tools": [],
                "expected_output": "结构化事实摘要",
                "required": True,
            },
            {
                "key": "assess_risk",
                "title": "评估代码风险",
                "objective": "基于已提炼事实判断代码和安全风险。",
                "dependency_keys": ["extract_context"],
                "required_capabilities": ["code_security_reasoning"],
                "proposed_provider": "deepseek",
                "route_reason_code": "deepseek_code_security_strength",
                "allowed_tools": ["source_scanner"],
                "expected_output": "带依据的风险列表",
                "required": True,
            },
        ],
        "synthesis_requirements": ["区分事实和推断"],
    }


def test_decomposition_document_is_strict_and_validates_a_dag():
    document = DecompositionDocument.model_validate(valid_document())
    assert validate_decomposition(
        document, expected_plan_version=2, max_subtasks=2
    ) is document
    with pytest.raises(ValidationError):
        DecompositionDocument.model_validate(valid_document() | {"extra": True})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda data: data["subtasks"].append(deepcopy(data["subtasks"][0])),
            "duplicate subtask key",
        ),
        (
            lambda data: data["subtasks"][1]["dependency_keys"].append("missing"),
            "missing dependency",
        ),
        (
            lambda data: data["subtasks"][0]["dependency_keys"].append(
                "extract_context"
            ),
            "self-dependency",
        ),
        (
            lambda data: (
                data["subtasks"][0].update({"dependency_keys": ["assess_risk"]}),
                data["subtasks"][1].update(
                    {"dependency_keys": ["extract_context"]}
                ),
            ),
            "cycle",
        ),
    ],
)
def test_validate_decomposition_rejects_invalid_graph(change, message):
    data = valid_document()
    change(data)
    document = DecompositionDocument.model_validate(data)

    with pytest.raises(ValueError, match=message):
        validate_decomposition(document, expected_plan_version=2, max_subtasks=64)


def test_validate_decomposition_rejects_wrong_plan_version():
    document = DecompositionDocument.model_validate(valid_document())

    with pytest.raises(ValueError, match="plan version"):
        validate_decomposition(document, expected_plan_version=3, max_subtasks=64)


def test_validate_decomposition_rejects_invalid_dynamic_limits():
    document = DecompositionDocument.model_validate(valid_document())

    with pytest.raises(ValueError, match="max_subtasks"):
        validate_decomposition(document, expected_plan_version=2, max_subtasks=0)
    with pytest.raises(ValueError, match="subtask count"):
        validate_decomposition(document, expected_plan_version=2, max_subtasks=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "bad key"),
        ("title", "   "),
        ("objective", "   "),
        ("expected_output", "   "),
    ],
)
def test_subtask_spec_rejects_invalid_strings(field, value):
    data = valid_document()
    data["subtasks"][0][field] = value

    with pytest.raises(ValidationError):
        DecompositionDocument.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dependency_keys", ["extract_context", "extract_context"]),
        ("required_capabilities", ["chinese_semantic", "chinese_semantic"]),
        ("allowed_tools", ["source_scanner", "source_scanner"]),
    ],
)
def test_subtask_spec_rejects_duplicate_values(field, value):
    data = valid_document()
    data["subtasks"][1][field] = value

    with pytest.raises(ValidationError):
        DecompositionDocument.model_validate(data)


def test_subtask_spec_rejects_policy_correction_route_codes():
    data = valid_document()
    data["subtasks"][0]["route_reason_code"] = "policy_tool_filtered"

    with pytest.raises(ValidationError):
        DecompositionDocument.model_validate(data)


def test_decomposition_document_rejects_non_strict_scalar_coercion():
    data = valid_document()
    data["plan_version"] = "2"

    with pytest.raises(ValidationError):
        DecompositionDocument.model_validate(data)


@pytest.mark.parametrize("plan_version", [0, -1])
def test_decomposition_document_rejects_non_positive_plan_versions(plan_version):
    data = valid_document()
    data["plan_version"] = plan_version

    with pytest.raises(ValidationError):
        DecompositionDocument.model_validate(data)


def test_validate_decomposition_rejects_document_larger_than_supplied_limit():
    data = valid_document()
    data["subtasks"].extend(
        {
            **data["subtasks"][0],
            "key": f"extra_{index}",
        }
        for index in range(2)
    )
    document = DecompositionDocument.model_validate(data)

    with pytest.raises(ValueError, match="subtask count"):
        validate_decomposition(document, expected_plan_version=2, max_subtasks=2)
