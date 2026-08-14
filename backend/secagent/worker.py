import asyncio
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from secagent.agents.executor import DemoEvidenceTool
from secagent.config import get_settings
from secagent.db import make_session_factory
from secagent.providers import build_providers
from secagent.providers.router import ModelRouter
from secagent.queue.celery_app import celery
from secagent.queue.celery_queue import CeleryJobQueue
from secagent.repository import TaskRepository
from secagent.services.task_service import TaskService
from secagent.tools.log_tools import (
    AttackPatternDetector,
    LogAnalyzer,
    LogTypeDetector,
    TimelineBuilder,
)
from secagent.tools.registry import ToolRegistry
from secagent.tools.source_tools import (
    ConfigChecker,
    ProjectDetector,
    SecretScanner,
    SourceScanner,
)
from secagent.tools.web_tools import FormExtract, HeaderCheck, HttpFetch, UrlGuardTool
from secagent.security.url_guard import UrlGuard


def build_worker_registry(allowed_hosts: set[str]) -> ToolRegistry:
    guard = UrlGuard(allowed_hosts)
    return ToolRegistry(
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
            UrlGuardTool(guard),
            HttpFetch(guard),
            HeaderCheck(),
            FormExtract(),
        ]
    )


async def execute_queued_task(
    task_id: str,
    command_id: str,
    session_factory: sessionmaker[Session],
    router: ModelRouter,
    registry: ToolRegistry,
    data_dir: Path,
) -> None:
    with session_factory() as session:
        service = TaskService(
            TaskRepository(session),
            router,
            registry,
            data_dir,
            CeleryJobQueue(),
        )
        await service.execute_queued(task_id, command_id)


@celery.task(name="secagent.run_task")
def run_task(task_id: str, command_id: str) -> None:
    settings = get_settings()
    allowed_hosts = {
        host.strip() for host in settings.web_allowed_hosts.split(",") if host.strip()
    }
    asyncio.run(
        execute_queued_task(
            task_id,
            command_id,
            make_session_factory(settings.database_url),
            ModelRouter(build_providers(settings), mode=settings.model_mode),
            build_worker_registry(allowed_hosts),
            settings.data_dir,
        )
    )
