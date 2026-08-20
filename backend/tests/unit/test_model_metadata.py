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


def test_model_response_and_ledger_normalize_untrusted_finish_reason() -> None:
    repository = CapturingRepository()
    response = ModelResponse(
        provider="glm",
        model="glm-5.2",
        data={"goal": "g"},
        latency_ms=1,
        finish_reason="Bearer top-secret-token",
    )

    LedgerService(repository).record_model_response(
        "task-1", ModelStage.REPORT, response
    )

    assert response.finish_reason == "unknown"
    assert repository.values is not None
    assert repository.values["finish_reason"] == "unknown"
    assert "top-secret-token" not in str(repository.values)


def test_model_response_and_ledger_reject_usage_outside_db_integer_range() -> None:
    repository = CapturingRepository()
    response = ModelResponse(
        provider="deepseek",
        model="deepseek-v4-pro",
        data={"steps": []},
        latency_ms=1,
        prompt_tokens=10**100,
        completion_tokens=2_147_483_648,
    )

    LedgerService(repository).record_model_response(
        "task-1", ModelStage.PLAN, response
    )

    assert response.prompt_tokens == 0
    assert response.completion_tokens == 0
    assert repository.values is not None
    assert repository.values["prompt_tokens"] == 0
    assert repository.values["completion_tokens"] == 0


def test_model_metrics_drop_prompts_raw_route_reasons_and_unsafe_request_ids() -> None:
    repository = CapturingRepository()

    LedgerService(repository).record_model_call(
        "task-1",
        provider="glm",
        model="glm-5.2",
        stage="task_parse",
        route_reason="Authorization: Bearer route-secret",
        input_summary="prompt Authorization: Bearer prompt-secret",
        request_id="req-1\nAuthorization: Bearer request-secret",
        is_demo=False,
    )

    assert repository.values is not None
    assert repository.values["route_reason"] == "fixed_stage"
    assert repository.values["input_summary"] == "task_parse structured request"
    assert repository.values["request_id"] is None
    assert "secret" not in str(repository.values)
