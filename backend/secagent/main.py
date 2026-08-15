from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis import Redis

from secagent import db_models  # noqa: F401 -- registers SQLAlchemy tables
from secagent.agents.executor import DemoEvidenceTool
from secagent.api.admin import router as admin_router
from secagent.api.auth import router as auth_router
from secagent.api.errors import install_error_handlers
from secagent.api.events import router as events_router
from secagent.api.tasks import router as tasks_router
from secagent.api.system import router as system_router
from secagent.auth.stream_tickets import (
    FakeTicketReplayStore,
    RedisTicketReplayStore,
    StreamTicketService,
)
from secagent.config import Settings, get_settings
from secagent.db import make_session_factory
from secagent.providers import build_providers
from secagent.providers.runtime import ProviderRuntimeFactory
from secagent.providers.router import ModelRouter
from secagent.queue.base import JobQueue
from secagent.queue.celery_queue import CeleryJobQueue
from secagent.repository import TaskRepository
from secagent.services.job_service import JobService
from secagent.security.url_guard import UrlGuard
from secagent.tools.registry import ToolRegistry
from secagent.tools.log_tools import (
    AttackPatternDetector,
    LogAnalyzer,
    LogTypeDetector,
    TimelineBuilder,
)
from secagent.tools.source_tools import (
    ConfigChecker,
    ProjectDetector,
    SecretScanner,
    SourceScanner,
)
from secagent.tools.http_request import HttpRequest
from secagent.tools.web_tools import FormExtract, HeaderCheck, UrlGuardTool


def create_app(
    settings: Settings | None = None, job_queue: JobQueue | None = None
) -> FastAPI:
    resolved_settings = settings or get_settings()
    session_factory = make_session_factory(resolved_settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            with application.state.session_factory() as session:
                JobService(
                    TaskRepository(session),
                    application.state.job_queue,
                    lease_seconds=application.state.settings.job_lease_seconds,
                    max_auto_retries=application.state.settings.job_auto_retries,
                ).recover_expired()
            yield
        finally:
            await application.state.model_router.aclose()

    app = FastAPI(title="SecAgent-X", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.provider_runtime_factory = ProviderRuntimeFactory(resolved_settings)
    app.state.job_queue = job_queue or CeleryJobQueue()
    replay_store = (
        FakeTicketReplayStore()
        if resolved_settings.database_url.startswith("sqlite")
        else RedisTicketReplayStore(Redis.from_url(resolved_settings.redis_url))
    )
    app.state.stream_ticket_service = StreamTicketService(
        resolved_settings.jwt_key(), replay_store
    )
    app.state.model_router = ModelRouter(
        build_providers(
            app.state.settings,
            allow_missing_live=True,
        ),
        mode=app.state.settings.model_mode,
        allow_missing=app.state.settings.model_mode == "live",
    )
    allowed_hosts = {
        host.strip()
        for host in app.state.settings.web_allowed_hosts.split(",")
        if host.strip()
    }
    url_guard = UrlGuard(allowed_hosts)
    app.state.tool_registry = ToolRegistry(
        [
            DemoEvidenceTool(),
            LogTypeDetector(),
            LogAnalyzer(),
            AttackPatternDetector(),
            TimelineBuilder(),
            ProjectDetector(),
            SourceScanner(),
            SecretScanner(),
            ConfigChecker(),
            UrlGuardTool(url_guard),
            HttpRequest(url_guard),
            HeaderCheck(),
            FormExtract(),
        ]
    )
    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(tasks_router)
    app.include_router(events_router)
    return app


app = create_app()
