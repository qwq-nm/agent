import json

from pydantic import BaseModel, Field

from secagent.domain import ModelRequest, ModelResponse, ModelStage, ParsedTask
from secagent.providers.router import ModelRouter
from secagent.services.ledger import LedgerService


class ReportSections(BaseModel):
    summary: str
    findings: list = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class Reporter:
    def __init__(self, router: ModelRouter, ledger: LedgerService) -> None:
        self.router = router
        self.ledger = ledger

    async def render(
        self, task_id: str, parsed: ParsedTask
    ) -> tuple[str, ModelResponse]:
        snapshot = self.ledger.snapshot(task_id)
        findings = [
            {
                "source": item["source"],
                "content": item["content"],
                "confidence": item["confidence"],
            }
            for item in snapshot["evidences"]
        ]
        response = await self.router.complete(
            ModelStage.REPORT,
            ModelRequest(
                system="根据提供的证据账本生成结构化报告章节，不得补造证据。",
                user=json.dumps(
                    {
                        "goal": parsed.goal,
                        "findings": findings,
                        "recommendations": ["复核原始证据并按授权范围处置"],
                        "errors": [],
                    },
                    ensure_ascii=False,
                ),
                response_schema=ReportSections.model_json_schema(),
            ),
        )
        sections = ReportSections.model_validate(response.data)
        demo = response.is_demo or any(
            item["is_demo"] for item in snapshot["model_calls"]
        )
        title = "# 演示结果：SecAgent-X 安全分析报告" if demo else "# SecAgent-X 安全分析报告"
        evidence_lines = [
            f"- `{item['source']}`：{item['content']}（置信度 {item['confidence']:.2f}）"
            for item in findings
        ] or ["- 暂无证据"]
        recommendation_lines = [
            f"- {item}" for item in sections.recommendations
        ] or ["- 暂无建议"]
        report = "\n\n".join(
            [
                title,
                f"## 摘要\n\n{sections.summary}",
                "## 证据链\n\n" + "\n".join(evidence_lines),
                "## 处置建议\n\n" + "\n".join(recommendation_lines),
                "## 说明\n\n模型仅负责解释，事实来源以证据账本为准。",
            ]
        )
        return report, response
