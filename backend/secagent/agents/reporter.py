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
    evidence_ids: list[str] = Field(default_factory=list)


class ReportArtifact(BaseModel):
    content: str
    evidence_ids: list[str]


class Reporter:
    def __init__(self, router: ModelRouter, ledger: LedgerService) -> None:
        self.router = router
        self.ledger = ledger

    async def render(
        self, task_id: str, parsed: ParsedTask
    ) -> tuple[ReportArtifact, ModelResponse]:
        snapshot = self.ledger.snapshot(task_id)
        findings = [
            {
                "id": item["id"],
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
        allowed_ids = {item["id"] for item in snapshot["evidences"]}
        cited_ids = sections.evidence_ids or [
            item["id"] for item in snapshot["evidences"]
        ]
        if len(cited_ids) != len(set(cited_ids)) or any(
            evidence_id not in allowed_ids for evidence_id in cited_ids
        ):
            raise ValueError("invalid evidence citation for current task")
        cited = {
            item["id"]: item
            for item in findings
            if item["id"] in set(cited_ids)
        }
        demo = response.is_demo or any(
            item["is_demo"] for item in snapshot["model_calls"]
        )
        title = "# 演示结果：SecAgent-X 安全分析报告" if demo else "# SecAgent-X 安全分析报告"
        evidence_lines = [
            f"- `[{evidence_id}]` `{cited[evidence_id]['source']}`："
            f"{cited[evidence_id]['content']}（置信度 {cited[evidence_id]['confidence']:.2f}）"
            for evidence_id in cited_ids
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
        return ReportArtifact(content=report, evidence_ids=cited_ids), response
