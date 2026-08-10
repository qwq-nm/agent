from secagent.domain import ModelRequest, ModelResponse


class MockProvider:
    name = "mock"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider="mock",
            model="deterministic-mock",
            data={"mock": True},
            latency_ms=0,
            is_demo=True,
        )
