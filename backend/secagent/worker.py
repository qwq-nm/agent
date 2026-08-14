import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from celery.signals import worker_process_shutdown
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


@dataclass
class _WorkerRuntime:
    runner: asyncio.Runner
    router: ModelRouter
    registry: ToolRegistry


_runtime: _WorkerRuntime | None = None


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
    *,
    worker_id: str | None = None,
    lease_seconds: int = 90,
    heartbeat_seconds: int = 15,
    max_auto_retries: int = 1,
) -> None:
    with session_factory() as session:
        service = TaskService(
            TaskRepository(session),
            router,
            registry,
            data_dir,
            CeleryJobQueue(),
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            max_auto_retries=max_auto_retries,
            heartbeat_session_factory=session_factory,
        )
        await service.execute_queued(
            task_id, command_id, worker_id or f"worker-{uuid4()}"
        )


def _get_worker_runtime(settings) -> _WorkerRuntime:
    global _runtime
    if _runtime is None:
        allowed_hosts = {
            host.strip()
            for host in settings.web_allowed_hosts.split(",")
            if host.strip()
        }
        _runtime = _WorkerRuntime(
            runner=asyncio.Runner(),
            router=ModelRouter(
                build_providers(settings), mode=settings.model_mode
            ),
            registry=build_worker_registry(allowed_hosts),
        )
    return _runtime


def _shutdown_worker_runtime() -> None:
    global _runtime
    runtime, _runtime = _runtime, None
    if runtime is None:
        return
    runtime.runner.run(runtime.router.aclose())
    runtime.runner.close()


@worker_process_shutdown.connect
def _close_worker_runtime(**_: object) -> None:
    _shutdown_worker_runtime()


@celery.task(name="secagent.run_task")
def run_task(task_id: str, command_id: str) -> None:
    settings = get_settings()
    runtime = _get_worker_runtime(settings)
    runtime.runner.run(
        execute_queued_task(
            task_id,
            command_id,
            make_session_factory(settings.database_url),
            runtime.router,
            runtime.registry,
            settings.data_dir,
            worker_id=getattr(run_task.request, "id", None),
            lease_seconds=settings.job_lease_seconds,
            heartbeat_seconds=settings.job_heartbeat_seconds,
            max_auto_retries=settings.job_auto_retries,
        )
    )
