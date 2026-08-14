import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, TypeVar
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
    run_lock: RLock

    def run(self, coroutine: Coroutine[Any, Any, "T"]) -> "T":
        with self.run_lock:
            return self.runner.run(coroutine)

    def close(self) -> None:
        with self.run_lock:
            self.runner.run(self.router.aclose())
            self.runner.close()


_runtime: _WorkerRuntime | None = None
_runtime_lock = RLock()
T = TypeVar("T")


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
    task_timeout_seconds: int = 300,
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
            task_timeout_seconds=task_timeout_seconds,
            heartbeat_session_factory=session_factory,
        )
        await service.execute_queued(
            task_id, command_id, worker_id or f"worker-{uuid4()}"
        )


def _get_worker_runtime(settings) -> _WorkerRuntime:
    global _runtime
    with _runtime_lock:
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
                run_lock=RLock(),
            )
        return _runtime


def _run_worker_coroutine(
    settings,
    operation: Callable[[_WorkerRuntime], Coroutine[Any, Any, T]],
) -> T:
    with _runtime_lock:
        runtime = _get_worker_runtime(settings)
        return runtime.run(operation(runtime))


def _shutdown_worker_runtime() -> None:
    global _runtime
    with _runtime_lock:
        runtime, _runtime = _runtime, None
        if runtime is None:
            return
        runtime.close()


@worker_process_shutdown.connect
def _close_worker_runtime(**_: object) -> None:
    _shutdown_worker_runtime()


@celery.task(name="secagent.run_task")
def run_task(task_id: str, command_id: str) -> None:
    settings = get_settings()
    _run_worker_coroutine(
        settings,
        lambda runtime: execute_queued_task(
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
            task_timeout_seconds=settings.task_timeout_seconds,
        ),
    )
