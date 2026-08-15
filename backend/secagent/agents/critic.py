import json

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
                system="仅根据证据账本判断任务是否完成。",
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
        return CriticDecision.model_validate(response.data), response

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
