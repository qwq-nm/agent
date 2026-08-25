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
from secagent.config import Settings, get_settings
from secagent.db import make_session_factory
from secagent.providers.router import ModelRouter
from secagent.providers.runtime import ProviderRuntimeFactory
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
from secagent.tools.http_request import HttpRequest
from secagent.tools.browser_tool import BrowserSnapshot
from secagent.tools.dirsearch_tool import DirsearchScan
from secagent.tools.login_probe_tool import LoginProbe
from secagent.tools.sqlmap_tool import SqlmapProbe
from secagent.tools.source_tools import (
    ConfigChecker,
    ProjectDetector,
    SecretScanner,
    SourceScanner,
)
from secagent.tools.specialized_tools import (
    ReverseArtifactAnalyzer,
    VulnerabilityScanner,
)
from secagent.tools.web_tools import (
    CookieAnalyzer,
    FlagPatternDetector,
    FormExtract,
    HeaderCheck,
    JsAnalyzer,
    LinkExtract,
    PathNormalizer,
    RobotsAnalyzer,
    SensitiveFileChecker,
    UrlGuardTool,
)
from secagent.security.url_guard import UrlGuard


@dataclass
class _WorkerRuntime:
    runner: asyncio.Runner
    registry: ToolRegistry
    run_lock: RLock

    def run(self, coroutine: Coroutine[Any, Any, "T"]) -> "T":
        with self.run_lock:
            return self.runner.run(coroutine)

    def close(self) -> None:
        with self.run_lock:
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
            VulnerabilityScanner(),
            ReverseArtifactAnalyzer(),
            UrlGuardTool(guard),
            HttpRequest(guard),
            BrowserSnapshot(guard),
            DirsearchScan(guard),
            LoginProbe(guard),
            SqlmapProbe(guard),
            HeaderCheck(),
            FormExtract(),
            LinkExtract(),
            RobotsAnalyzer(),
            JsAnalyzer(),
            PathNormalizer(),
            FlagPatternDetector(),
            CookieAnalyzer(),
            SensitiveFileChecker(),
        ]
    )


async def execute_queued_task(
    task_id: str,
    command_id: str,
    session_factory: sessionmaker[Session],
    router: ModelRouter | None,
    registry: ToolRegistry,
    data_dir: Path,
    *,
    worker_id: str | None = None,
    lease_seconds: int = 90,
    heartbeat_seconds: int = 15,
    max_auto_retries: int = 1,
    task_timeout_seconds: int = 1800,
    max_replans: int = 2,
    settings: Settings | None = None,
) -> None:
    with session_factory() as session:
        task_router = router
        if task_router is None:
            if settings is None:
                raise ValueError("settings are required when router is omitted")
            task_router = ProviderRuntimeFactory(settings).build(session)
        try:
            service = TaskService(
                TaskRepository(session),
                task_router,
                registry,
                data_dir,
                CeleryJobQueue(),
                lease_seconds=lease_seconds,
                heartbeat_seconds=heartbeat_seconds,
                max_auto_retries=max_auto_retries,
                task_timeout_seconds=task_timeout_seconds,
                max_replans=max_replans,
                heartbeat_session_factory=session_factory,
            )
            await service.execute_queued(
                task_id, command_id, worker_id or f"worker-{uuid4()}"
            )
        finally:
            await task_router.aclose()


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
            None,
            runtime.registry,
            settings.data_dir,
            worker_id=getattr(run_task.request, "id", None),
            lease_seconds=settings.job_lease_seconds,
            heartbeat_seconds=settings.job_heartbeat_seconds,
            max_auto_retries=settings.job_auto_retries,
            task_timeout_seconds=settings.task_timeout_seconds,
            max_replans=settings.max_replans,
            settings=settings,
        ),
    )
