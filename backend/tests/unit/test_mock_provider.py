import json

import pytest

from secagent.agents.planner import PlanDocument
from secagent.agents.reporter import ReportSections
from secagent.conversation_decomposition import (
    DecompositionDocument,
    validate_decomposition,
)
from secagent.domain import CriticDecision, ModelRequest, ModelStage
from secagent.providers.mock import MockProvider


@pytest.mark.asyncio
async def test_mock_report_sections_turn_evidence_into_traceable_narratives() -> None:
    payload = {
        "goal": "检查目标页面的安全配置",
        "findings": [
            {
                "id": "evidence-001",
                "source": "header_check",
                "content": "响应缺少 Strict-Transport-Security",
                "confidence": 0.92,
            },
            {
                "id": "evidence-002",
                "source": "form_extract",
                "content": "登录表单使用 POST 提交",
                "confidence": 0.85,
            },
        ],
        "recommendations": ["复核原始证据并按授权范围处置"],
        "errors": [],
    }
    request = ModelRequest(
        stage=ModelStage.REPORT,
        system="生成报告",
        user=json.dumps(payload, ensure_ascii=False),
        response_schema=ReportSections.model_json_schema(),
    )

    response = await MockProvider().complete(request)

    assert response.data["findings"] == [
        "证据 evidence-001：响应缺少 Strict-Transport-Security",
        "证据 evidence-002：登录表单使用 POST 提交",
    ]
    assert all(isinstance(item, str) for item in response.data["findings"])
    assert response.data["evidence_ids"] == ["evidence-001", "evidence-002"]
    assert (
        ReportSections.model_validate(response.data).findings
        == response.data["findings"]
    )
    assert payload["findings"][0]["content"] == "响应缺少 Strict-Transport-Security"


@pytest.mark.asyncio
async def test_mock_report_sections_accept_empty_findings() -> None:
    request = ModelRequest(
        stage=ModelStage.REPORT,
        system="生成报告",
        user=json.dumps(
            {
                "goal": "检查目标页面的安全配置",
                "findings": [],
                "recommendations": [],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        response_schema=ReportSections.model_json_schema(),
    )

    response = await MockProvider().complete(request)

    assert response.data["findings"] == []
    assert response.data["evidence_ids"] == []
    assert ReportSections.model_validate(response.data).findings == []


@pytest.mark.asyncio
async def test_mock_critic_continues_for_required_and_recommended_evidence_gaps() -> None:
    payload = {
        "goal": "检查目标页面的安全配置",
        "scene": "web_analysis",
        "evidence_count": 2,
        "observations": [],
        "scene_evidence_assessment": {
            "required_missing": ["authorized HTTP observation"],
            "recommended_next_focus": [
                "public form observation",
                "robots.txt observation",
            ],
        },
    }
    request = ModelRequest(
        stage=ModelStage.CRITIC,
        system="复核证据",
        user=json.dumps(payload, ensure_ascii=False),
        response_schema=CriticDecision.model_json_schema(),
    )

    response = await MockProvider().complete(request)

    assert response.data["is_complete"] is False
    assert response.data["should_continue"] is True
    assert response.data["should_report"] is False
    assert response.data["missing_evidence"] == [
        {"kind": "factual", "description": "authorized HTTP observation"},
        {"kind": "factual", "description": "public form observation"},
    ]
    assert response.data["next_focus"] == [
        "authorized HTTP observation",
        "public form observation",
        "robots.txt observation",
    ]
    assert CriticDecision.model_validate(response.data).should_continue is True


@pytest.mark.asyncio
async def test_mock_critic_reports_when_demo_evidence_is_present() -> None:
    request = ModelRequest(
        stage=ModelStage.CRITIC,
        system="复核证据",
        user=json.dumps(
            {
                "goal": "生成演示报告",
                "scene": "incident_response",
                "evidence_count": 1,
                "observations": [
                    {
                        "evidence_type": "raw_line",
                        "source": "demo_evidence",
                        "content": "演示日志证据",
                    }
                ],
                "scene_evidence_assessment": {
                    "required_missing": ["parsed log evidence"],
                    "recommended_next_focus": ["attack pattern evidence"],
                },
            },
            ensure_ascii=False,
        ),
        response_schema=CriticDecision.model_json_schema(),
    )

    response = await MockProvider().complete(request)

    assert response.data["is_complete"] is True
    assert response.data["should_continue"] is False
    assert response.data["should_report"] is True
    assert response.data["missing_evidence"] == []


@pytest.mark.asyncio
async def test_mock_replan_skips_successful_tools_and_preserves_input_payload() -> None:
    payload = {
        "goal": "检查目标页面的安全配置",
        "replan_round": 1,
        "max_plan_steps": 2,
        "allowed_tools": [
            "url_guard",
            "http_fetch",
            "header_check",
            "form_extract",
        ],
        "params_by_tool": {
            "url_guard": {"url": "https://example.test"},
            "http_fetch": {"url": "https://example.test"},
            "header_check": {"response": "$http"},
            "form_extract": {"response": "$http"},
        },
        "risk_by_tool": {
            "url_guard": "low",
            "http_fetch": "medium",
            "header_check": "low",
            "form_extract": "low",
        },
        "execution_memory": {
            "successful_tool_calls": [
                {"tool_name": "url_guard"},
                {"tool_name": "http_fetch"},
            ]
        },
    }
    request = ModelRequest(
        stage=ModelStage.PLAN,
        system="生成计划",
        user=json.dumps(payload, ensure_ascii=False),
        response_schema=PlanDocument.model_json_schema(),
    )

    response = await MockProvider().complete(request)

    assert [step["tool_name"] for step in response.data["steps"]] == [
        "header_check",
        "form_extract",
    ]
    assert PlanDocument.model_validate(response.data).steps[0].params == {
        "response": "$http"
    }
    assert payload["execution_memory"]["successful_tool_calls"] == [
        {"tool_name": "url_guard"},
        {"tool_name": "http_fetch"},
    ]


def decomposition_request(*, plan_version: int, max_subtasks: int) -> ModelRequest:
    return ModelRequest(
        stage=ModelStage.DECOMPOSE,
        system="分解任务",
        user=json.dumps(
            {
                "plan_version": plan_version,
                "budget_limits": {"max_subtasks": max_subtasks},
                "context": {
                    "current_message": "分析材料和代码",
                    "available_tools": [
                        {
                            "name": "source_scanner",
                            "risk_level": "low",
                            "description": "扫描源码",
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
        response_schema=DecompositionDocument.model_json_schema(),
    )


@pytest.mark.asyncio
async def test_mock_decomposition_emulates_flash_with_stable_two_provider_plan() -> None:
    response = await MockProvider().complete(
        decomposition_request(plan_version=7, max_subtasks=4)
    )
    document = DecompositionDocument.model_validate(response.data)

    assert validate_decomposition(
        document, expected_plan_version=7, max_subtasks=4
    ) is document
    assert [item.proposed_provider.value for item in document.subtasks] == [
        "glm",
        "deepseek",
    ]
    assert document.subtasks[1].dependency_keys == [document.subtasks[0].key]
    assert document.subtasks[1].allowed_tools == ["source_scanner"]
    assert response.provider == "mock"
    assert response.model == "deterministic-mock"
    assert response.emulated_provider == "deepseek"
    assert response.emulated_model == "deepseek-v4-flash"
    assert response.is_demo is True


@pytest.mark.asyncio
async def test_mock_decomposition_respects_exact_single_subtask_budget() -> None:
    response = await MockProvider().complete(
        decomposition_request(plan_version=9, max_subtasks=1)
    )
    document = DecompositionDocument.model_validate(response.data)

    assert validate_decomposition(
        document, expected_plan_version=9, max_subtasks=1
    ) is document
    assert len(document.subtasks) == 1
    assert document.subtasks[0].required is True


@pytest.mark.asyncio
async def test_legacy_mock_response_does_not_claim_decomposition_emulation() -> None:
    response = await MockProvider().complete(
        ModelRequest(
            stage=ModelStage.REPORT,
            system="生成报告",
            user=json.dumps(
                {"goal": "g", "findings": [], "recommendations": [], "errors": []}
            ),
            response_schema=ReportSections.model_json_schema(),
        )
    )

    assert response.emulated_provider is None
    assert response.emulated_model is None
