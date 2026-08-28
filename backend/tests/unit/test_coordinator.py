from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from secagent.agents.coordinator import (
    CoordinatorAgent,
    CoordinatorAttachment,
    CoordinatorCompletedSubtask,
    CoordinatorEvidence,
    CoordinatorRecentMessage,
    CoordinatorTool,
    DecompositionContext,
)
from secagent.conversation_decomposition import LogicalProvider, RouteReasonCode
from secagent.conversation_domain import ConversationSettings, TurnBudgetSnapshot
from secagent.domain import ModelRequest, ModelResponse, ModelStage, RiskLevel
from secagent.providers.router import ModelRouter
from secagent.services.assignment_policy import AssignmentPolicy


def decomposition_data(*, plan_version: int = 3) -> dict:
    return {
        "plan_version": plan_version,
        "goal_summary": "分析项目材料与代码风险",
        "subtasks": [
            {
                "key": "extract_context",
                "title": "提炼材料",
                "objective": "提取中文材料中的关键事实。",
                "dependency_keys": [],
                "required_capabilities": ["chinese_semantic"],
                "proposed_provider": "glm",
                "route_reason_code": "glm_chinese_strength",
                "allowed_tools": ["source_scanner", "admin_tool"],
                "expected_output": "事实摘要",
                "required": True,
            },
            {
                "key": "assess_risk",
                "title": "评估代码风险",
                "objective": "根据事实判断代码与安全风险。",
                "dependency_keys": ["extract_context"],
                "required_capabilities": ["code_security_reasoning"],
                "proposed_provider": "deepseek",
                "route_reason_code": "deepseek_code_security_strength",
                "allowed_tools": ["source_scanner"],
                "expected_output": "风险列表",
                "required": True,
            },
        ],
        "synthesis_requirements": ["区分事实和推断"],
    }


def valid_context(**changes) -> DecompositionContext:
    data = {
        "current_message": "请分析这份项目材料",
        "conversation_summary": "用户要求在授权范围内检查代码。",
        "recent_messages": [
            {"role": "user", "content": "先梳理中文需求。"},
            {"role": "assistant", "content": "我会先提取约束。"},
        ],
        "attachments": [
            {
                "original_name": "project.zip",
                "relative_path": "uploads/project.zip",
                "content_type": "application/zip",
                "sha256": "a" * 64,
                "scan_summary": "归档已完成安全扫描。",
            }
        ],
        "settings": {
            "authorization_scope": "仅检查已上传项目",
            "safety_mode": "conservative",
            "allowed_targets": [],
            "requested_parallelism": 2,
        },
        "completed_subtasks": [
            {
                "key": "prior_check",
                "provider": "glm",
                "summary": "已确认项目语言。",
                "evidence_refs": ["evidence-1"],
            }
        ],
        "evidence": [
            {"ref": "evidence-1", "summary": "项目包含 Python。", "confidence": 0.9}
        ],
        "unresolved_questions": ["是否包含部署配置？"],
        "available_tools": [
            {
                "name": "source_scanner",
                "risk_level": "low",
                "description": "扫描源码，api_key=sk-tool-secret",
            },
            {
                "name": "admin_tool",
                "risk_level": "high",
                "description": "需要额外授权",
            },
        ],
        "budget": {
            "max_subtasks": 4,
            "max_model_calls_per_subtask": 2,
            "max_tool_calls_per_subtask": 2,
            "timeout_seconds": 180,
            "max_replans": 1,
            "max_context_tokens": 8_000,
        },
    }
    data.update(changes)
    return DecompositionContext.model_validate(data)


class CapturingProvider:
    name = "deepseek"
    model = "deepseek-v4-flash"

    def __init__(self, data: dict | None = None) -> None:
        self.data = data or decomposition_data()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data=deepcopy(self.data),
            latency_ms=1,
        )


def test_coordinator_context_dtos_accept_exact_bounded_contract() -> None:
    context = valid_context()

    assert context.recent_messages == [
        CoordinatorRecentMessage(role="user", content="先梳理中文需求。"),
        CoordinatorRecentMessage(role="assistant", content="我会先提取约束。"),
    ]
    assert isinstance(context.attachments[0], CoordinatorAttachment)
    assert isinstance(context.completed_subtasks[0], CoordinatorCompletedSubtask)
    assert context.completed_subtasks[0].provider is LogicalProvider.GLM
    assert isinstance(context.evidence[0], CoordinatorEvidence)
    assert isinstance(context.available_tools[0], CoordinatorTool)
    assert context.available_tools[0].risk_level.value == "low"
    assert isinstance(context.settings, ConversationSettings)
    assert isinstance(context.budget, TurnBudgetSnapshot)

    enum_subtask = CoordinatorCompletedSubtask(
        key="enum_task",
        provider=LogicalProvider.DEEPSEEK,
        summary="已完成检查。",
        evidence_refs=[],
    )
    enum_tool = CoordinatorTool(
        name="source_scanner",
        risk_level=RiskLevel.LOW,
        description="扫描源码",
    )
    assert enum_subtask.provider is LogicalProvider.DEEPSEEK
    assert enum_tool.risk_level is RiskLevel.LOW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current_message", " "),
        ("current_message", "x" * 64_001),
        ("conversation_summary", "x" * 16_001),
        ("recent_messages", [{"role": "user", "content": "x"}] * 41),
        ("attachments", []),
        ("completed_subtasks", []),
        ("evidence", []),
        ("unresolved_questions", ["same", "same"]),
        (
            "available_tools",
            [
                {"name": "same", "risk_level": "low", "description": "first"},
                {"name": "same", "risk_level": "low", "description": "second"},
            ],
        ),
    ],
    ids=[
        "blank-current-message",
        "oversized-current-message",
        "oversized-summary",
        "too-many-recent-messages",
        "too-many-attachments",
        "too-many-completed-subtasks",
        "too-much-evidence",
        "duplicate-questions",
        "duplicate-tool-names",
    ],
)
def test_coordinator_context_rejects_invalid_bounds_and_duplicates(field, value) -> None:
    changes = {field: value}
    if field in {"attachments", "completed_subtasks", "evidence"}:
        limits = {"attachments": 21, "completed_subtasks": 65, "evidence": 129}
        base = valid_context().model_dump(mode="json")[field]
        changes[field] = base * limits[field]

    with pytest.raises(ValidationError):
        valid_context(**changes)


@pytest.mark.parametrize(
    "model_type,data",
    [
        (CoordinatorRecentMessage, {"role": "system", "content": "x"}),
        (
            CoordinatorAttachment,
            {
                "original_name": "x",
                "relative_path": "../secret",
                "content_type": "text/plain",
                "sha256": "A" * 64,
                "scan_summary": "ok",
            },
        ),
        (
            CoordinatorCompletedSubtask,
            {
                "key": "bad key",
                "provider": "mock",
                "summary": "ok",
                "evidence_refs": [],
            },
        ),
        (CoordinatorEvidence, {"ref": "x", "summary": "ok", "confidence": 1.1}),
        (
            CoordinatorTool,
            {"name": "Bad-Tool", "risk_level": "low", "description": "ok"},
        ),
    ],
)
def test_coordinator_nested_dtos_reject_invalid_values(model_type, data) -> None:
    with pytest.raises(ValidationError):
        model_type.model_validate(data)


@pytest.mark.parametrize("provider", [b"glm", 1])
def test_completed_subtask_provider_rejects_non_string_enum_input(provider) -> None:
    with pytest.raises(ValidationError):
        CoordinatorCompletedSubtask.model_validate(
            {
                "key": "prior_check",
                "provider": provider,
                "summary": "已完成检查。",
                "evidence_refs": [],
            }
        )


@pytest.mark.parametrize("risk_level", [b"low", 1])
def test_coordinator_tool_risk_level_rejects_non_string_enum_input(
    risk_level,
) -> None:
    with pytest.raises(ValidationError):
        CoordinatorTool.model_validate(
            {
                "name": "source_scanner",
                "risk_level": risk_level,
                "description": "扫描源码",
            }
        )


def test_coordinator_context_is_strict_and_forbids_extras_or_scalar_coercion() -> None:
    data = valid_context().model_dump(mode="json")
    data["extra"] = True
    with pytest.raises(ValidationError):
        DecompositionContext.model_validate(data)

    data.pop("extra")
    data["current_message"] = 123
    with pytest.raises(ValidationError):
        DecompositionContext.model_validate(data)


def test_empty_conversation_summary_is_the_only_empty_context_text() -> None:
    assert valid_context(conversation_summary="").conversation_summary == ""
    with pytest.raises(ValidationError):
        valid_context(conversation_summary="   ")


@pytest.mark.asyncio
async def test_context_over_token_budget_is_rejected_before_model_call() -> None:
    provider = CapturingProvider()
    router = ModelRouter({"deepseek": provider}, mode="auto")
    context = valid_context(
        current_message="x" * 1_000,
        budget={
            "max_subtasks": 4,
            "max_model_calls_per_subtask": 2,
            "max_tool_calls_per_subtask": 2,
            "timeout_seconds": 180,
            "max_replans": 1,
            "max_context_tokens": 100,
        },
    )

    with pytest.raises(ValueError, match="context budget"):
        await CoordinatorAgent(router, AssignmentPolicy()).decompose(
            context,
            plan_version=3,
            registered_tools={"source_scanner"},
            authorized_tools={"source_scanner"},
        )

    assert provider.requests == []


@pytest.mark.asyncio
async def test_context_budget_is_measured_before_secret_redaction_shrinks_it() -> None:
    provider = CapturingProvider()
    router = ModelRouter({"deepseek": provider}, mode="auto")
    context = valid_context(
        current_message="api_key=" + ("s" * 1_000),
        budget={
            "max_subtasks": 4,
            "max_model_calls_per_subtask": 2,
            "max_tool_calls_per_subtask": 2,
            "timeout_seconds": 180,
            "max_replans": 1,
            "max_context_tokens": 400,
        },
    )

    with pytest.raises(ValueError, match="context budget"):
        await CoordinatorAgent(router, AssignmentPolicy()).decompose(
            context,
            plan_version=3,
            registered_tools={"source_scanner"},
            authorized_tools={"source_scanner"},
        )

    assert provider.requests == []


@pytest.mark.asyncio
async def test_coordinator_redacts_and_filters_bounded_canonical_request() -> None:
    provider = CapturingProvider()
    router = ModelRouter({"deepseek": provider}, mode="auto")
    context = valid_context(
        current_message="token=Bearer current-secret",
        conversation_summary="api_key=sk-summary-secret",
    )

    await CoordinatorAgent(router, AssignmentPolicy()).decompose(
        context,
        plan_version=3,
        registered_tools={"source_scanner", "admin_tool"},
        authorized_tools={"source_scanner"},
    )

    assert len(provider.requests) == 1
    request = provider.requests[0]
    payload = json.loads(request.user)
    assert request.stage is ModelStage.DECOMPOSE
    assert request.response_schema["title"] == "DecompositionDocument"
    assert request.user == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert [tool["name"] for tool in payload["context"]["available_tools"]] == [
        "source_scanner"
    ]
    assert "admin_tool" not in request.user
    assert "current-secret" not in request.user
    assert "summary-secret" not in request.user
    assert "tool-secret" not in request.user
    assert set(payload) == {
        "budget_limits",
        "capability_matrix",
        "context",
        "plan_version",
    }
    expected_budget = {
        "max_subtasks": 4,
        "max_model_calls_per_subtask": 2,
        "max_tool_calls_per_subtask": 2,
        "timeout_seconds": 180,
        "max_replans": 1,
        "max_context_tokens": 8_000,
    }
    assert payload["budget_limits"] == expected_budget
    assert payload["context"]["budget"] == expected_budget


@pytest.mark.asyncio
async def test_coordinator_uses_router_derived_provider_availability() -> None:
    provider = CapturingProvider()
    router = ModelRouter({"deepseek": provider}, mode="auto")

    result = await CoordinatorAgent(router, AssignmentPolicy()).decompose(
        valid_context(),
        plan_version=3,
        registered_tools={"source_scanner"},
        authorized_tools={"source_scanner"},
    )

    first = result.assignments[0]
    assert first.assigned_provider is LogicalProvider.DEEPSEEK
    assert RouteReasonCode.POLICY_PROVIDER_UNAVAILABLE in first.correction_codes


@pytest.mark.asyncio
async def test_coordinator_calls_decompose_once_and_returns_only_redacted_mapping() -> None:
    raw = decomposition_data()
    raw["goal_summary"] = "检查 token=Bearer returned-secret"
    provider = CapturingProvider(raw)
    router = ModelRouter({"deepseek": provider}, mode="auto")

    result = await CoordinatorAgent(router, AssignmentPolicy()).decompose(
        valid_context(),
        plan_version=3,
        registered_tools={"source_scanner"},
        authorized_tools={"source_scanner"},
    )

    assert len(provider.requests) == 1
    assert "returned-secret" not in result.document.goal_summary
    assert "returned-secret" not in result.model_response.model_dump_json()
    assert result.model_response.data == result.document.model_dump(mode="json")
    assert [item.allowed_tools for item in result.assignments] == [
        ["source_scanner"],
        ["source_scanner"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "message"),
    [
        (decomposition_data(plan_version=4), "plan version"),
        (
            decomposition_data()
            | {
                "subtasks": decomposition_data()["subtasks"]
                + [
                    decomposition_data()["subtasks"][0]
                    | {"key": "extra_task", "dependency_keys": []}
                ]
            },
            "subtask count",
        ),
    ],
)
async def test_coordinator_validates_plan_version_and_turn_subtask_budget(
    data: dict, message: str
) -> None:
    provider = CapturingProvider(data)
    context = valid_context(
        budget={
            "max_subtasks": 2,
            "max_model_calls_per_subtask": 2,
            "max_tool_calls_per_subtask": 2,
            "timeout_seconds": 180,
            "max_replans": 1,
            "max_context_tokens": 8_000,
        }
    )

    with pytest.raises(ValueError, match=message):
        await CoordinatorAgent(
            ModelRouter({"deepseek": provider}, mode="auto"),
            AssignmentPolicy(),
        ).decompose(
            context,
            plan_version=3,
            registered_tools={"source_scanner"},
            authorized_tools={"source_scanner"},
        )
