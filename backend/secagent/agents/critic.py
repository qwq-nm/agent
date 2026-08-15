import json
import re

from secagent.domain import (
    CriticDecision,
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

_FACTUAL_EVIDENCE_TERMS = (
    "evidence",
    "fact",
    "finding",
    "observation",
    "data",
    "证据",
    "事实",
    "发现",
    "观察",
    "数据",
)
_REPORT_ONLY_PATTERNS = (
    re.compile(
        r"\b(?:generate|write|draft|create|produce|render|finalize|compile)\b"
        r".{0,32}\breport\b"
    ),
    re.compile(
        r"\breport\b.{0,32}"
        r"\b(?:generation|generated|writing|written|drafting|drafted|"
        r"creation|created|missing|absent)\b"
    ),
    re.compile(r"(?:生成|撰写|编写|输出|创建|完成|整理).{0,8}报告"),
    re.compile(r"报告.{0,8}(?:尚未|未|待)(?:生成|撰写|编写|输出|创建|完成|整理)"),
    re.compile(r"缺少.{0,4}报告"),
)


def _is_report_generation_only(item: str) -> bool:
    text = " ".join(item.casefold().split())
    if any(term in text for term in _FACTUAL_EVIDENCE_TERMS):
        return False
    canonical = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text).strip()
    if canonical in {"report", "final report", "报告", "最终报告"}:
        return True
    return any(pattern.search(text) for pattern in _REPORT_ONLY_PATTERNS)


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
                    "报告本身不得列为缺失证据；missing_evidence 只能包含可通过"
                    "白名单工具补充的事实或观察。"
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
            if not _is_report_generation_only(item)
        ]
        if missing_evidence != decision.missing_evidence:
            decision = decision.model_copy(
                update={
                    "is_complete": decision.is_complete or not missing_evidence,
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
            "missing_evidence": [str(item)[:256] for item in missing[:12]],
        }
