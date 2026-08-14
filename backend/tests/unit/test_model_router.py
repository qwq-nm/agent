import pytest
from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.db import Base
from secagent.domain import ModelRequest, ModelResponse, ModelStage
from secagent.main import create_app
from secagent.providers import build_providers
from secagent.providers.base import ProviderErrorCode, ProviderUnavailable
from secagent.providers.deepseek import DeepSeekProvider
from secagent.providers.glm import GLMProvider
from secagent.providers.router import FIXED_PROVIDER, ModelRouter
from secagent.queue.fake import FakeJobQueue
from secagent.worker import _get_worker_runtime, _shutdown_worker_runtime


class StubProvider:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.fail:
            raise ProviderUnavailable(
                self.name, ProviderErrorCode.NETWORK, retryable=True
            )
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
    assert FIXED_PROVIDER[ModelStage.CRITIC] == "deepseek"
    assert FIXED_PROVIDER[ModelStage.REPORT] == "glm"


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
async def test_auto_does_not_fall_back_to_another_provider_or_mock() -> None:
    router = ModelRouter(
        {
            "glm": StubProvider("glm", True),
            "deepseek": StubProvider("deepseek", True),
            "mock": StubProvider("mock"),
        },
        mode="auto",
    )
    with pytest.raises(ProviderUnavailable) as caught:
        await router.complete(
            ModelStage.CRITIC,
            ModelRequest(system="s", user="u", response_schema={}),
        )
    assert caught.value.provider == "deepseek"


@pytest.mark.asyncio
async def test_mock_provider_is_used_only_in_explicit_mock_mode() -> None:
    router = ModelRouter({"mock": StubProvider("mock")}, mode="mock")

    response = await router.complete(
        ModelStage.REPORT,
        ModelRequest(system="s", user="u", response_schema={}),
    )

    assert response.provider == "mock"
    assert response.is_demo is True


def test_live_mode_rejects_missing_fixed_provider_before_completion() -> None:
    with pytest.raises(ProviderUnavailable) as caught:
        ModelRouter({"glm": StubProvider("glm")}, mode="live")

    assert caught.value.code is ProviderErrorCode.AUTH
    assert str(caught.value) == "deepseek: auth"


@pytest.mark.asyncio
async def test_preferred_model_cannot_override_fixed_stage_route() -> None:
    router = ModelRouter(
        {
            "glm": StubProvider("glm"),
            "deepseek": StubProvider("deepseek"),
        },
        mode="auto",
    )

    response = await router.complete(
        ModelStage.PLAN,
        ModelRequest(system="s", user="u", response_schema={}),
        preferred="glm",
    )

    assert response.provider == "deepseek"


@pytest.mark.asyncio
async def test_build_providers_uses_specific_adapters_and_one_shared_client() -> None:
    providers = build_providers(
        Settings(
            deepseek_api_key="ds-key",
            glm_api_key="glm-key",
            deepseek_model="deepseek-v4-pro",
            glm_model="glm-5.2",
        )
    )

    assert isinstance(providers["deepseek"], DeepSeekProvider)
    assert isinstance(providers["glm"], GLMProvider)
    assert providers["deepseek"].client is providers["glm"].client
    assert providers["deepseek"].model == "deepseek-v4-pro"
    assert providers["glm"].model == "glm-5.2"
    await ModelRouter(providers, mode="live").aclose()


@pytest.mark.asyncio
async def test_build_providers_reads_secret_files(tmp_path) -> None:
    deepseek_key = tmp_path / "deepseek-key"
    glm_key = tmp_path / "glm-key"
    deepseek_key.write_text("ds-file-key", encoding="utf-8")
    glm_key.write_text("glm-file-key", encoding="utf-8")

    providers = build_providers(
        Settings(
            deepseek_api_key_file=deepseek_key,
            glm_api_key_file=glm_key,
        )
    )

    assert "deepseek" in providers
    assert "glm" in providers
    await ModelRouter(providers, mode="live").aclose()


@pytest.mark.parametrize(
    ("field", "value"),
    [("deepseek_model", "gpt-5"), ("glm_model", "GPT-4o")],
)
def test_build_providers_rejects_gpt_models(field: str, value: str) -> None:
    values = {
        "deepseek_api_key": "ds-key",
        "glm_api_key": "glm-key",
        field: value,
    }

    with pytest.raises(ProviderUnavailable) as caught:
        build_providers(Settings(**values))

    assert caught.value.code is ProviderErrorCode.INVALID_SCHEMA
    assert value not in str(caught.value)


@pytest.mark.parametrize("missing", ["deepseek", "glm"])
def test_live_build_rejects_missing_key_before_creating_clients(missing: str) -> None:
    values = {
        "model_mode": "live",
        "deepseek_api_key": "ds-key",
        "glm_api_key": "glm-key",
    }
    values[f"{missing}_api_key"] = None

    with pytest.raises(ProviderUnavailable) as caught:
        build_providers(Settings(**values))

    assert caught.value.provider == missing
    assert caught.value.code is ProviderErrorCode.AUTH
    assert str(caught.value) == f"{missing}: auth"


def test_app_lifespan_closes_shared_provider_client(tmp_path) -> None:
    application = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'app.db'}",
            model_mode="live",
            deepseek_api_key="ds-key",
            glm_api_key="glm-key",
            jwt_signing_key="test-signing-key-at-least-32-bytes",
        ),
        job_queue=FakeJobQueue(),
    )
    provider = application.state.model_router.providers["deepseek"]
    Base.metadata.create_all(application.state.session_factory.kw["bind"])

    with TestClient(application):
        assert provider.client.is_closed is False

    assert provider.client.is_closed is True


def test_sync_worker_runtime_reuses_and_closes_one_provider_pool(tmp_path) -> None:
    _shutdown_worker_runtime()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        model_mode="live",
        deepseek_api_key="ds-key",
        glm_api_key="glm-key",
    )

    first = _get_worker_runtime(settings)
    second = _get_worker_runtime(settings)
    client = first.router.providers["deepseek"].client

    assert first is second
    assert first.runner is second.runner
    assert client is first.router.providers["glm"].client
    assert client.is_closed is False

    _shutdown_worker_runtime()
    assert client.is_closed is True
