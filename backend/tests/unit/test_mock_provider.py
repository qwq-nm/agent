import json

import pytest

from secagent.agents.reporter import ReportSections
from secagent.domain import ModelRequest, ModelStage
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
