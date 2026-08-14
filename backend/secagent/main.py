from contextlib import asynccontextmanager

from fastapi import FastAPI

from secagent import db_models  # noqa: F401 -- registers SQLAlchemy tables
from secagent.agents.executor import DemoEvidenceTool
from secagent.api.admin import router as admin_router
from secagent.api.auth import router as auth_router
from secagent.api.errors import install_error_handlers
from secagent.api.tasks import router as tasks_router
from secagent.api.system import router as system_router
from secagent.config import Settings, get_settings
from secagent.db import make_session_factory
from secagent.providers import build_providers
from secagent.providers.router import ModelRouter
from secagent.queue.base import JobQueue
from secagent.queue.celery_queue import CeleryJobQueue
from secagent.repository import TaskRepository
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
from secagent.tools.web_tools import FormExtract, HeaderCheck, HttpFetch, UrlGuardTool


def create_app(
    settings: Settings | None = None, job_queue: JobQueue | None = None
) -> FastAPI:
    resolved_settings = settings or get_settings()
    session_factory = make_session_factory(resolved_settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        with application.state.session_factory() as session:
            TaskRepository(session).recover_interrupted_tasks()
        yield

    app = FastAPI(title="SecAgent-X", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.job_queue = job_queue or CeleryJobQueue()
    app.state.model_router = ModelRouter(
        build_providers(app.state.settings), mode=app.state.settings.model_mode
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
            HttpFetch(url_guard),
            HeaderCheck(),
            FormExtract(),
        ]
    )
    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(tasks_router)
    return app


app = create_app()
