import asyncio

import pytest
from cryptography.fernet import Fernet

from secagent.config import Settings
from secagent.db import Base
from secagent.domain import ModelRequest, ModelStage
from secagent.main import create_app
from secagent.providers.base import ProviderErrorCode, ProviderUnavailable
from secagent.providers.router import ModelRouter
from secagent.queue.fake import FakeJobQueue
from secagent.services.provider_credentials import ProviderCredentialStore
from secagent.worker import execute_queued_task


def _live_settings(tmp_path, **values) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        model_mode="live",
        provider_credential_encryption_key=Fernet.generate_key().decode(),
        jwt_signing_key="test-signing-key-at-least-32-bytes",
        **values,
    )


def test_runtime_factory_refreshes_database_key_without_recreating_app(tmp_path):
    settings = _live_settings(tmp_path)
    application = create_app(settings, job_queue=FakeJobQueue())
    Base.metadata.create_all(application.state.session_factory.kw["bind"])

    with application.state.session_factory() as session:
        ProviderCredentialStore(session, settings).save(
            "deepseek", "database-deepseek-first", None
        )
        first = application.state.provider_runtime_factory.build(session)

    assert first.providers["deepseek"].api_key == "database-deepseek-first"
    asyncio.run(first.aclose())

    with application.state.session_factory() as session:
        ProviderCredentialStore(session, settings).save(
            "deepseek", "database-deepseek-second", None
        )
        second = application.state.provider_runtime_factory.build(session)

    assert second.providers["deepseek"].api_key == "database-deepseek-second"
    asyncio.run(second.aclose())


def test_live_boot_liveness_is_available_and_missing_model_is_not_mock(tmp_path):
    settings = _live_settings(tmp_path)
    application = create_app(settings, job_queue=FakeJobQueue())
    Base.metadata.create_all(application.state.session_factory.kw["bind"])

    from fastapi.testclient import TestClient

    with TestClient(application) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 503

        with application.state.session_factory() as session:
            router = application.state.provider_runtime_factory.build(session)
            with pytest.raises(ProviderUnavailable) as caught:
                asyncio.run(
                    router.complete(
                        ModelStage.PLAN,
                        ModelRequest(system="s", user="u", response_schema={}),
                    )
                )
        asyncio.run(router.aclose())

    assert caught.value.provider == "deepseek"
    assert caught.value.code is ProviderErrorCode.AUTH


def test_worker_builds_one_router_per_task_and_closes_it(
    app, fake_queue, monkeypatch: pytest.MonkeyPatch
):
    class TrackingRouter:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    routers = []

    def build(_self, _session):
        router = TrackingRouter()
        routers.append(router)
        return router

    monkeypatch.setattr(
        "secagent.worker.ProviderRuntimeFactory.build",
        build,
    )

    asyncio.run(
        execute_queued_task(
            "missing-task",
            "missing-command",
            app.state.session_factory,
            None,
            app.state.tool_registry,
            app.state.settings.data_dir,
            settings=app.state.settings,
        )
    )

    assert len(routers) == 1
    assert routers[0].closed is True
