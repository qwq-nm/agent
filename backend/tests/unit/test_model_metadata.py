from secagent.domain import ModelResponse, ModelStage
from secagent.services.ledger import LedgerService


class CapturingRepository:
    def __init__(self) -> None:
        self.values = None

    def add_model_call(self, **values):
        self.values = values
        return "call-1"


def test_ledger_persists_safe_provider_response_metadata() -> None:
    repository = CapturingRepository()
    response = ModelResponse(
        provider="deepseek",
        model="deepseek-v4-pro",
        data={"steps": []},
        latency_ms=15,
        request_id="req-1",
        finish_reason="stop",
        prompt_tokens=12,
        completion_tokens=5,
        retry_count=2,
    )

    LedgerService(repository).record_model_response(
        "task-1", ModelStage.PLAN, response
    )

    assert repository.values is not None
    assert repository.values["request_id"] == "req-1"
    assert repository.values["finish_reason"] == "stop"
    assert repository.values["prompt_tokens"] == 12
    assert repository.values["completion_tokens"] == 5
    assert repository.values["retry_count"] == 2
