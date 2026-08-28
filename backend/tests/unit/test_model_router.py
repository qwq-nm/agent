import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

import secagent.worker as worker_module
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
from secagent.worker import (
    _get_worker_runtime,
    _run_worker_coroutine,
    _shutdown_worker_runtime,
)


class StubProvider:
    def __init__(
        self, name: str, fail: bool = False, *, model: str = "stub"
    ) -> None:
        self.name = name
        self.fail = fail
        self.model = model
        self.complete_count = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.complete_count += 1
        if self.fail:
            raise ProviderUnavailable(
                self.name, ProviderErrorCode.NETWORK, retryable=True
            )
        return ModelResponse(
            provider=self.name,
            model=self.model,
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
async def test_auto_missing_fixed_provider_does_not_call_other_live_provider() -> None:
    deepseek = StubProvider("deepseek")
    router = ModelRouter({"deepseek": deepseek}, mode="auto")

    with pytest.raises(ProviderUnavailable) as caught:
        await router.complete(
            ModelStage.TASK_PARSE,
            ModelRequest(system="s", user="u", response_schema={}),
        )

    assert caught.value.provider == "glm"
    assert caught.value.code is ProviderErrorCode.AUTH
    assert deepseek.complete_count == 0


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
async def test_new_fixed_stages_use_flash_deepseek_despite_caller_preference() -> None:
    glm = StubProvider("glm", model="glm-5.2")
    deepseek = StubProvider("deepseek", model="deepseek-v4-flash")
    router = ModelRouter({"glm": glm, "deepseek": deepseek}, mode="auto")
    request = ModelRequest(system="s", user="u", response_schema={})

    decompose = await router.complete(
        ModelStage.DECOMPOSE, request, preferred="glm"
    )
    synthesize = await router.complete(
        ModelStage.SYNTHESIZE, request, preferred="glm"
    )

    assert FIXED_PROVIDER[ModelStage.DECOMPOSE] == "deepseek"
    assert FIXED_PROVIDER[ModelStage.SYNTHESIZE] == "deepseek"
    assert decompose.provider == "deepseek"
    assert synthesize.provider == "deepseek"
    assert glm.complete_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred", ["glm", "deepseek"])
async def test_subtask_execute_uses_required_logical_preference(preferred: str) -> None:
    router = ModelRouter(
        {
            "glm": StubProvider("glm", model="glm-5.2"),
            "deepseek": StubProvider("deepseek", model="deepseek-v4-flash"),
        },
        mode="auto",
    )

    response = await router.complete(
        ModelStage.SUBTASK_EXECUTE,
        ModelRequest(system="s", user="u", response_schema={}),
        preferred=preferred,
    )

    assert response.provider == preferred


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred", [None, "mock", "unknown"])
async def test_subtask_execute_rejects_missing_or_non_logical_preference(
    preferred: str | None,
) -> None:
    router = ModelRouter(
        {
            "glm": StubProvider("glm", model="glm-5.2"),
            "deepseek": StubProvider("deepseek", model="deepseek-v4-flash"),
            "mock": StubProvider("mock"),
        },
        mode="auto",
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await router.complete(
            ModelStage.SUBTASK_EXECUTE,
            ModelRequest(system="s", user="u", response_schema={}),
            preferred=preferred,
        )

    assert caught.value.code is ProviderErrorCode.INVALID_SCHEMA
    assert all(provider.complete_count == 0 for provider in router.providers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "live"])
async def test_decompose_missing_deepseek_never_calls_glm_or_mock(mode: str) -> None:
    glm = StubProvider("glm", model="glm-5.2")
    mock = StubProvider("mock")
    router = ModelRouter(
        {"glm": glm, "mock": mock}, mode=mode, allow_missing=True
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await router.complete(
            ModelStage.DECOMPOSE,
            ModelRequest(system="s", user="u", response_schema={}),
        )

    assert caught.value.provider == "deepseek"
    assert glm.complete_count == 0
    assert mock.complete_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [ModelStage.DECOMPOSE, ModelStage.SYNTHESIZE])
async def test_fixed_deepseek_stage_rejects_non_flash_adapter_before_call(
    stage: ModelStage,
) -> None:
    deepseek = StubProvider("deepseek", model="deepseek-v4-pro")
    router = ModelRouter({"deepseek": deepseek}, mode="auto")

    with pytest.raises(ProviderUnavailable) as caught:
        await router.complete(
            stage,
            ModelRequest(system="s", user="u", response_schema={}),
        )

    assert caught.value.provider == "deepseek"
    assert caught.value.code is ProviderErrorCode.INVALID_SCHEMA
    assert caught.value.retryable is False
    assert deepseek.complete_count == 0


@pytest.mark.asyncio
async def test_explicit_mock_mode_emulates_fixed_decompose_with_mock_only() -> None:
    mock = StubProvider("mock")
    deepseek = StubProvider("deepseek", model="deepseek-v4-flash")
    router = ModelRouter({"mock": mock, "deepseek": deepseek}, mode="mock")

    response = await router.complete(
        ModelStage.DECOMPOSE,
        ModelRequest(system="s", user="u", response_schema={}),
        preferred="glm",
    )

    assert response.provider == "mock"
    assert mock.complete_count == 1
    assert deepseek.complete_count == 0


@pytest.mark.asyncio
async def test_explicit_mock_subtask_still_requires_logical_preference() -> None:
    mock = StubProvider("mock")
    router = ModelRouter({"mock": mock}, mode="mock")

    with pytest.raises(ProviderUnavailable) as caught:
        await router.complete(
            ModelStage.SUBTASK_EXECUTE,
            ModelRequest(system="s", user="u", response_schema={}),
        )

    assert caught.value.code is ProviderErrorCode.INVALID_SCHEMA
    assert mock.complete_count == 0


@pytest.mark.asyncio
async def test_decompose_does_not_require_glm_before_model_call() -> None:
    deepseek = StubProvider("deepseek", model="deepseek-v4-flash")
    router = ModelRouter({"deepseek": deepseek}, mode="auto")

    response = await router.complete(
        ModelStage.DECOMPOSE,
        ModelRequest(system="s", user="u", response_schema={}),
    )

    assert response.provider == "deepseek"
    assert deepseek.complete_count == 1


def test_logical_assignment_providers_are_derived_from_mode_and_configuration() -> None:
    auto = ModelRouter(
        {
            "deepseek": StubProvider("deepseek", model="deepseek-v4-flash"),
            "mock": StubProvider("mock"),
        },
        mode="auto",
    )
    explicit_mock = ModelRouter({"mock": StubProvider("mock")}, mode="mock")

    assert auto.logical_assignment_providers() == {"deepseek"}
    assert explicit_mock.logical_assignment_providers() == {"glm", "deepseek"}


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


def test_explicit_provider_key_snapshot_does_not_fall_back_to_settings() -> None:
    providers = build_providers(
        Settings(
            model_mode="auto",
            deepseek_api_key="environment-deepseek-key",
            glm_api_key="environment-glm-key",
        ),
        provider_keys={},
    )

    assert set(providers) == {"mock"}


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


def test_sync_worker_runtime_reuses_runner_and_registry(tmp_path) -> None:
    _shutdown_worker_runtime()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        model_mode="live",
        deepseek_api_key="ds-key",
        glm_api_key="glm-key",
    )

    first = _get_worker_runtime(settings)
    second = _get_worker_runtime(settings)
    assert first is second
    assert first.runner is second.runner
    assert first.registry is second.registry

    _shutdown_worker_runtime()


def test_worker_runtime_is_thread_safe_and_shutdown_runs_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shutdown_worker_runtime()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'thread-worker.db'}",
        model_mode="live",
        deepseek_api_key="ds-key",
        glm_api_key="glm-key",
    )
    original_registry = worker_module.build_worker_registry
    build_count = 0
    build_count_lock = threading.Lock()

    def slow_build(allowed_hosts):
        nonlocal build_count
        with build_count_lock:
            build_count += 1
        time.sleep(0.05)
        return original_registry(allowed_hosts)

    monkeypatch.setattr(worker_module, "build_worker_registry", slow_build)
    start = threading.Barrier(2)
    runtimes = []
    results = []
    errors = []

    def invoke(index: int) -> None:
        coroutine = None
        try:
            start.wait()
            runtime = _get_worker_runtime(settings)
            runtimes.append(runtime)
            coroutine = asyncio.sleep(0.02, result=index)
            results.append(_run_worker_coroutine(settings, lambda _: coroutine))
        except Exception as exc:  # asserted below with both worker threads joined
            errors.append(exc)
            if coroutine is not None:
                coroutine.close()

    threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(results) == [0, 1]
    assert build_count == 1
    assert len({id(runtime) for runtime in runtimes}) == 1
    assert len({id(runtime.registry) for runtime in runtimes}) == 1

    runtime = runtimes[0]
    original_close = runtime.close
    close_count = 0

    def counted_close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    runtime.close = counted_close
    stop = threading.Barrier(2)

    def shutdown() -> None:
        stop.wait()
        _shutdown_worker_runtime()

    shutdown_threads = [threading.Thread(target=shutdown) for _ in range(2)]
    for thread in shutdown_threads:
        thread.start()
    for thread in shutdown_threads:
        thread.join()

    assert close_count == 1
def test_worker_shutdown_waits_for_inflight_runtime_call(tmp_path) -> None:
    _shutdown_worker_runtime()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'shutdown-worker.db'}",
        model_mode="live",
        deepseek_api_key="ds-key",
        glm_api_key="glm-key",
    )
    started = threading.Event()
    release = threading.Event()
    run_errors = []

    async def blocked_call() -> str:
        started.set()
        await asyncio.to_thread(release.wait)
        return "finished"

    def invoke() -> None:
        try:
            assert (
                _run_worker_coroutine(settings, lambda _: blocked_call())
                == "finished"
            )
        except Exception as exc:  # asserted after joining the worker thread
            run_errors.append(exc)

    run_thread = threading.Thread(target=invoke)
    run_thread.start()
    assert started.wait(timeout=2)

    shutdown_thread = threading.Thread(target=_shutdown_worker_runtime)
    shutdown_thread.start()
    shutdown_thread.join(timeout=0.05)
    assert shutdown_thread.is_alive()

    release.set()
    run_thread.join(timeout=2)
    shutdown_thread.join(timeout=2)

    assert run_errors == []
    assert run_thread.is_alive() is False
    assert shutdown_thread.is_alive() is False
