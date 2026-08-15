import json

from secagent.domain import (
    CriticDecision,
    MissingEvidenceKind,
    ModelRequest,
    ModelResponse,
    ModelStage,
    ParsedTask,
)
from secagent.providers.router import ModelRouter
from secagent.security.redaction import redact_mapping
from secagent.services.ledger import LedgerService

MAX_OBSERVATIONS = 12
MAX_OBSERVATION_CONTENT_CHARS = 1024
MAX_OBSERVATION_SOURCE_CHARS = 256


class Critic:
    def __init__(self, router: ModelRouter, ledger: LedgerService) -> None:
        self.router = router
        self.ledger = ledger

    async def review(
        self, task_id: str, parsed: ParsedTask
    ) -> tuple[CriticDecision, ModelResponse]:
        observations = self.observation_summary(task_id)
        response = await self.router.complete(
            ModelStage.CRITIC,
            ModelRequest(
                system=(
                    "你正在报告生成前复核证据。仅判断现有事实证据是否足以回答"
                    "用户目标并生成报告，而不是判断报告是否已经存在。"
                    "报告本身不得列为缺失证据；如需表达尚待生成报告，"
                    "只能将其标记为 report_generation 流程项。"
                    "missing_evidence 的每一项必须包含 kind 和 description。"
                    "可通过白名单工具补充的事实或观察使用 kind=factual；"
                    "仅表示尚需生成报告的流程项使用 kind=report_generation。"
                ),
                user=json.dumps(
                    {
                        "goal": parsed.goal,
                        "evidence_count": observations["evidence_count"],
                        "observations": observations["items"],
                    },
                    ensure_ascii=False,
                ),
                response_schema=CriticDecision.model_json_schema(),
            ),
        )
        decision = CriticDecision.model_validate(response.data)
        missing_evidence = [
            item
            for item in decision.missing_evidence
            if item.kind is MissingEvidenceKind.FACTUAL
        ]
        is_complete = not missing_evidence
        if (
            missing_evidence != decision.missing_evidence
            or is_complete != decision.is_complete
        ):
            decision = decision.model_copy(
                update={
                    "is_complete": is_complete,
                    "missing_evidence": missing_evidence,
                }
            )
        return decision, response

    def observation_summary(
        self,
        task_id: str,
        decision: CriticDecision | None = None,
    ) -> dict:
        evidences = self.ledger.snapshot(task_id)["evidences"]
        items = []
        for evidence in evidences[-MAX_OBSERVATIONS:]:
            safe = redact_mapping(
                {
                    "evidence_type": evidence.get("evidence_type", "observation"),
                    "source": evidence.get("source", "unknown"),
                    "content": evidence.get("content", ""),
                }
            )
            items.append(
                {
                    "evidence_type": str(safe["evidence_type"])[:64],
                    "source": str(safe["source"])[
                        :MAX_OBSERVATION_SOURCE_CHARS
                    ],
                    "content": str(safe["content"])[
                        :MAX_OBSERVATION_CONTENT_CHARS
                    ],
                }
            )
        missing = decision.missing_evidence if decision is not None else []
        return {
            "evidence_count": len(evidences),
            "items": items,
            "missing_evidence": [item.description[:256] for item in missing[:12]],
        }
