import json

from secagent.domain import CriticDecision, ModelRequest, ModelResponse, ModelStage, ParsedTask
from secagent.providers.router import ModelRouter
from secagent.services.ledger import LedgerService


class Critic:
    def __init__(self, router: ModelRouter, ledger: LedgerService) -> None:
        self.router = router
        self.ledger = ledger

    async def review(
        self, task_id: str, parsed: ParsedTask
    ) -> tuple[CriticDecision, ModelResponse]:
        snapshot = self.ledger.snapshot(task_id)
        response = await self.router.complete(
            ModelStage.CRITIC,
            ModelRequest(
                system="仅根据证据账本判断任务是否完成。",
                user=json.dumps(
                    {
                        "goal": parsed.goal,
                        "evidence_count": len(snapshot["evidences"]),
                    },
                    ensure_ascii=False,
                ),
                response_schema=CriticDecision.model_json_schema(),
            ),
        )
        return CriticDecision.model_validate(response.data), response
