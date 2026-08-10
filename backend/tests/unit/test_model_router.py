import pytest

from secagent.domain import ModelRequest, ModelResponse, ModelStage
from secagent.providers.router import ModelRouter, ProviderUnavailable


class StubProvider:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.fail:
            raise ProviderUnavailable(self.name)
        return ModelResponse(
            provider=self.name,
            model="stub",
            data={"ok": True},
            latency_ms=1,
            is_demo=self.name == "mock",
        )


@pytest.mark.asyncio
async def test_auto_routes_parse_to_glm_and_plan_to_deepseek() -> None:
    router = ModelRouter(
        {
            "glm": StubProvider("glm"),
            "deepseek": StubProvider("deepseek"),
            "mock": StubProvider("mock"),
        },
        mode="auto",
    )
    request = ModelRequest(system="s", user="u", response_schema={})
    assert (await router.complete(ModelStage.TASK_PARSE, request)).provider == "glm"
    assert (await router.complete(ModelStage.PLAN, request)).provider == "deepseek"


@pytest.mark.asyncio
async def test_live_never_falls_back_to_mock() -> None:
    router = ModelRouter(
        {
            "glm": StubProvider("glm", True),
            "deepseek": StubProvider("deepseek", True),
            "mock": StubProvider("mock"),
        },
        mode="live",
    )
    with pytest.raises(ProviderUnavailable):
        await router.complete(
            ModelStage.TASK_PARSE,
            ModelRequest(system="s", user="u", response_schema={}),
        )


@pytest.mark.asyncio
async def test_auto_marks_mock_fallback_as_demo() -> None:
    router = ModelRouter(
        {
            "glm": StubProvider("glm", True),
            "deepseek": StubProvider("deepseek", True),
            "mock": StubProvider("mock"),
        },
        mode="auto",
    )
    response = await router.complete(
        ModelStage.CRITIC,
        ModelRequest(system="s", user="u", response_schema={}),
    )
    assert response.provider == "mock"
    assert response.is_demo is True
