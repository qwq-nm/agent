import pytest
from pydantic import ValidationError

from secagent.dag_domain import (
    JobKind,
    ModelFailureCreate,
    ModelFailureDecision,
    ModelFailureStage,
    SubtaskResultDocument,
    SubtaskStatus,
    dag_command_id,
)


def test_enum_values_match_the_plan_contract() -> None:
    assert {item.value for item in JobKind} == {
        "turn_decompose",
        "subtask_execute",
        "turn_synthesize",
    }
    assert {item.value for item in SubtaskStatus} == {
        "pending_dependency",
        "queued",
        "running",
        "waiting_tool_approval",
        "waiting_model_decision",
        "completed",
        "incomplete",
        "skipped",
        "superseded",
        "failed",
        "cancelled",
    }
    assert {item.value for item in ModelFailureStage} == {
        "decompose",
        "subtask",
        "synthesize",
    }
    assert {item.value for item in ModelFailureDecision} == {
        "retry_same",
        "reassign",
        "skip_and_replan",
        "terminate_turn",
    }


def _result_document() -> dict:
    return {
        "status": "completed",
        "summary": "提炼了关键事实",
        "claims": [
            {
                "statement": "日志包含扫描行为",
                "evidence_ref": "ev-1",
            }
        ],
        "evidence_refs": ["ev-1"],
        "inference_notes": ["扫描可能来自同一攻击者"],
        "unresolved": [],
    }


def test_subtask_result_document_is_strict_and_valid() -> None:
    document = SubtaskResultDocument.model_validate(_result_document())
    assert document.status == "completed"
    assert document.claims[0].evidence_ref == "ev-1"

    with pytest.raises(ValidationError):
        SubtaskResultDocument.model_validate(_result_document() | {"extra": True})

    with pytest.raises(ValidationError):
        SubtaskResultDocument.model_validate(
            _result_document()
            | {"claims": [{"statement": "没有引用的事实断言"}]}
        )

    with pytest.raises(ValidationError):
        SubtaskResultDocument.model_validate(
            _result_document() | {"status": 1}
        )

    with pytest.raises(ValidationError):
        SubtaskResultDocument.model_validate(
            _result_document() | {"evidence_refs": ["a", "a"]}
        )


def test_claim_document_accepts_any_single_reference_kind() -> None:
    from secagent.dag_domain import ClaimDocument

    upstream_only = ClaimDocument(statement="上游结论", upstream_key="extract_context")
    assert upstream_only.upstream_key == "extract_context"
    assert upstream_only.evidence_ref is None

    attachment_only = ClaimDocument(statement="附件结论", attachment_ref="uploads/a.txt")
    assert attachment_only.attachment_ref == "uploads/a.txt"

    with pytest.raises(ValidationError):
        ClaimDocument(statement="没有任何引用")


def test_model_failure_create_is_strict() -> None:
    payload = {
        "turn_id": "00000000-0000-0000-0000-000000000001",
        "subtask_id": None,
        "stage": "decompose",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "error_code": "provider_unavailable",
        "detail": "DeepSeek 配置缺失",
    }
    failure = ModelFailureCreate.model_validate(payload)
    assert failure.stage is ModelFailureStage.DECOMPOSE

    with pytest.raises(ValidationError):
        ModelFailureCreate.model_validate(payload | {"stage": "unknown"})
    with pytest.raises(ValidationError):
        ModelFailureCreate.model_validate(payload | {"detail": "   "})


def test_dag_command_id_is_deterministic_and_scoped() -> None:
    first = dag_command_id("turn", "turn-1", "synthesize")
    assert first == dag_command_id("turn", "turn-1", "synthesize")
    assert first != dag_command_id("turn", "turn-2", "synthesize")
    assert len(first) == 64
