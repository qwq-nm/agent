# SecAgent-X MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable SecAgent-X MVP that uses DeepSeek, GLM, or an explicitly marked Mock provider to complete explainable log, source-audit, and passive-Web security workflows.

**Architecture:** A Vue 3 console calls a FastAPI service backed by SQLite. A stateful AgentRunner routes each stage through ModelRouter, RiskGate, ToolRegistry, EvidenceLedger, and three scene-specific agents; deterministic tools produce evidence and the LLM only interprets, plans, reviews, and reports.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, httpx, pytest, Vue 3, TypeScript, Vite, Element Plus, Vitest, Playwright, SQLite, Docker Compose.

## Global Constraints

- Support Windows local development and Linux Docker Compose deployment on CPU-only hosts.
- Use only DeepSeek, GLM, and Mock providers; GPT is out of scope.
- Read API keys only from environment variables and never persist them.
- LLM output must pass Pydantic validation before use.
- Executor may call only registered tools and may not execute model-generated shell commands.
- Web analysis is passive GET/HEAD only; no payload submission, port scanning, brute force, directory scanning, or exploitation.
- Uploaded source is read statically and never executed.
- Limit each task to two re-planning rounds and configurable step/tool/model timeouts.
- Mock-derived tasks and reports must expose `is_demo=true` and visible “演示结果” text.
- Core backend coverage must remain at or above 80%; all three fixed Demo flows must pass end to end.
- Use TDD for every task: failing test, verified failure, minimal implementation, verified pass, then one focused commit.

---

## Planned File Map

```text
secagent-x/
├── pyproject.toml                         # Python dependencies and pytest configuration
├── .env.example                           # Safe configuration template without secrets
├── README.md                              # Local and Docker usage
├── docker-compose.yml                     # backend, frontend, and passive web-demo
├── backend/
│   ├── Dockerfile
│   ├── secagent/
│   │   ├── main.py                        # FastAPI application factory
│   │   ├── config.py                      # Typed environment configuration
│   │   ├── db.py                          # SQLAlchemy engine/session lifecycle
│   │   ├── domain.py                      # Shared enums and Pydantic contracts
│   │   ├── db_models.py                   # SQLite ORM rows
│   │   ├── repository.py                  # Task, step, call, evidence persistence
│   │   ├── providers/
│   │   │   ├── base.py                    # ModelProvider protocol
│   │   │   ├── mock.py                    # Deterministic offline responses
│   │   │   ├── openai_compatible.py       # Shared JSON-chat HTTP client
│   │   │   └── router.py                  # DeepSeek/GLM/Mock policy
│   │   ├── tools/
│   │   │   ├── base.py                    # BaseTool and ToolContext
│   │   │   ├── registry.py                # Allowlisted execution
│   │   │   ├── log_tools.py               # Log detection, analysis, patterns, timeline
│   │   │   ├── source_tools.py            # Project, risky-code, secret, config checks
│   │   │   └── web_tools.py               # Guarded fetch, headers, forms
│   │   ├── security/
│   │   │   ├── files.py                   # Upload and archive isolation
│   │   │   ├── url_guard.py               # SSRF and redirect policy
│   │   │   └── redaction.py               # Secret masking
│   │   ├── agents/
│   │   │   ├── parser.py                   # Structured TaskParser
│   │   │   ├── planner.py                  # Structured plan generation
│   │   │   ├── risk.py                     # RiskGate
│   │   │   ├── executor.py                 # Tool execution and evidence capture
│   │   │   ├── critic.py                   # Evidence sufficiency check
│   │   │   ├── reporter.py                 # Markdown report rendering
│   │   │   ├── scenes.py                   # Scene tool policies and completion criteria
│   │   │   └── runner.py                   # State-machine orchestration
│   │   ├── services/
│   │   │   ├── ledger.py                   # EvidenceLedger writes and reads
│   │   │   ├── storage.py                  # Task workspaces and uploads
│   │   │   └── task_service.py             # Lifecycle operations
│   │   └── api/
│   │       ├── tasks.py                    # Task CRUD and lifecycle endpoints
│   │       └── system.py                   # Health, provider, and tool status
│   └── tests/
│       ├── unit/                           # Deterministic component tests
│       ├── integration/                    # API + SQLite + runner tests
│       └── fixtures/                       # Fixed log/source inputs
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── api/client.ts                   # Typed REST client
│   │   ├── types.ts                        # Shared UI contracts
│   │   ├── router.ts                       # Page routes
│   │   ├── stores/tasks.ts                 # Task polling and actions
│   │   ├── views/                          # Dashboard/Create/List/Detail/Reports/System
│   │   └── components/                     # Timeline, evidence, model route, approval
│   └── tests/                              # Vitest component and store tests
├── e2e/                                    # Playwright three-scene flows
├── demo_cases/                             # Stable Demo inputs
└── web-demo/                               # Passive-analysis local target
```

## Cross-Task Interfaces

These names are fixed for every task:

```python
# backend/secagent/domain.py
class TaskScene(StrEnum): INCIDENT_RESPONSE = "incident_response"; SOURCE_AUDIT = "source_audit"; WEB_ANALYSIS = "web_analysis"
class TaskStatus(StrEnum): CREATED = "created"; PARSED = "parsed"; PLANNED = "planned"; RUNNING = "running"; WAITING_HUMAN = "waiting_human"; PAUSED = "paused"; COMPLETED = "completed"; FAILED_RETRYABLE = "failed_retryable"; FAILED = "failed"; CANCELLED = "cancelled"
class RiskLevel(StrEnum): LOW = "low"; MEDIUM = "medium"; HIGH = "high"; FORBIDDEN = "forbidden"
class ModelStage(StrEnum): TASK_PARSE = "task_parse"; PLAN = "plan"; CRITIC = "critic"; REPORT = "report"
class RouteMode(StrEnum): AUTO = "auto"; MANUAL = "manual"

class ModelRequest(BaseModel):
    system: str
    user: str
    response_schema: dict

class ModelResponse(BaseModel):
    provider: str
    model: str
    data: dict
    latency_ms: int
    is_demo: bool = False

class PlanStep(BaseModel):
    name: str
    purpose: str
    tool_name: str
    params: dict
    risk_level: RiskLevel
    need_human_confirm: bool = False

class ToolResult(BaseModel):
    success: bool
    summary: str
    findings: list[dict] = []
    evidence: list[dict] = []
    metrics: dict = {}
    warnings: list[str] = []
    error: str | None = None
```

Provider signature: `async complete(request: ModelRequest) -> ModelResponse`.

ModelRouter signature: `async complete(stage: ModelStage, request: ModelRequest, preferred: str | None = None) -> ModelResponse`.

Tool signature: `async run(params: dict, context: ToolContext) -> ToolResult`.

Runner signature: `async run(task_id: str) -> TaskRunResult`.

Repository methods used across tasks: `create_task`, `get_task`, `list_tasks`, `set_task_status`, `add_step`, `update_step`, `add_model_call`, `add_tool_call`, `add_evidence`, `save_report`, and `recover_interrupted_tasks`.

---

### Task 1: Backend Bootstrap and Health Contract

**Files:**
- Create: `pyproject.toml`
- Create: `backend/secagent/__init__.py`
- Create: `backend/secagent/config.py`
- Create: `backend/secagent/main.py`
- Create: `backend/secagent/api/__init__.py`
- Create: `backend/secagent/api/system.py`
- Test: `backend/tests/unit/test_health.py`

**Interfaces:**
- Consumes: none.
- Produces: `Settings`, `create_app() -> FastAPI`, and `GET /api/health -> {"status": "ok", "service": "secagent-x"}`.

- [ ] **Step 1: Write the failing health test**

```python
# backend/tests/unit/test_health.py
from fastapi.testclient import TestClient

from secagent.main import create_app


def test_health_contract() -> None:
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "secagent-x"}
```

- [ ] **Step 2: Run the test and verify the package is absent**

Run: `python -m pytest backend/tests/unit/test_health.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'secagent'`.

- [ ] **Step 3: Add the minimal installable backend**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "secagent-x"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.30,<1",
  "pydantic-settings>=2.6,<3",
  "sqlalchemy>=2.0,<3",
  "httpx>=0.28,<1",
  "python-multipart>=0.0.20,<1",
  "jinja2>=3.1,<4",
]

[project.optional-dependencies]
dev = ["pytest>=8.3,<9", "pytest-asyncio>=0.25,<1", "pytest-cov>=6,<7"]

[tool.setuptools.packages.find]
where = ["backend"]

[tool.pytest.ini_options]
testpaths = ["backend/tests"]
asyncio_mode = "auto"
addopts = "--strict-markers"
```

```python
# backend/secagent/config.py
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    service_name: str = "secagent-x"
    database_url: str = "sqlite:///./data/secagent.db"
    data_dir: Path = Path("data")
    model_mode: str = "auto"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# backend/secagent/api/system.py
from fastapi import APIRouter

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "secagent-x"}
```

```python
# backend/secagent/main.py
from fastapi import FastAPI

from secagent.api.system import router as system_router
from secagent.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="SecAgent-X", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.include_router(system_router)
    return app


app = create_app()
```

Create empty package markers at `backend/secagent/__init__.py` and `backend/secagent/api/__init__.py`.

- [ ] **Step 4: Install and verify the health contract**

Run: `python -m pip install -e ".[dev]" && python -m pytest backend/tests/unit/test_health.py -v`

Expected: `1 passed`.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add pyproject.toml backend/secagent backend/tests/unit/test_health.py
git commit -m "chore: bootstrap FastAPI backend"
```

---

### Task 2: Domain Contracts, SQLite Persistence, and Task CRUD

**Files:**
- Create: `backend/secagent/domain.py`
- Create: `backend/secagent/db.py`
- Create: `backend/secagent/db_models.py`
- Create: `backend/secagent/repository.py`
- Create: `backend/secagent/api/tasks.py`
- Modify: `backend/secagent/main.py`
- Create: `backend/tests/conftest.py`
- Test: `backend/tests/integration/test_task_crud.py`

**Interfaces:**
- Consumes: `Settings.database_url` and `create_app()` from Task 1.
- Produces: shared enums/contracts from “Cross-Task Interfaces”, `TaskRepository`, and task create/list/detail API contracts.

- [ ] **Step 1: Write the failing CRUD integration test**

```python
# backend/tests/integration/test_task_crud.py
from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.main import create_app


def test_create_list_and_get_task(tmp_path, monkeypatch) -> None:
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}"))
    with TestClient(app) as client:
        created = client.post("/api/tasks", json={
            "goal": "分析 access.log",
            "authorization_scope": "仅分析上传文件",
            "route_mode": "auto",
        })
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["status"] == "created"
        assert client.get("/api/tasks").json()[0]["id"] == task_id
        assert client.get(f"/api/tasks/{task_id}").json()["goal"] == "分析 access.log"
```

- [ ] **Step 2: Run the test and verify the route is absent**

Run: `python -m pytest backend/tests/integration/test_task_crud.py -v`

Expected: FAIL because `POST /api/tasks` returns `404`.

- [ ] **Step 3: Implement typed domain and persistence contracts**

```python
# backend/secagent/domain.py
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskScene(StrEnum):
    INCIDENT_RESPONSE = "incident_response"
    SOURCE_AUDIT = "source_audit"
    WEB_ANALYSIS = "web_analysis"


class TaskStatus(StrEnum):
    CREATED = "created"
    PARSED = "parsed"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FORBIDDEN = "forbidden"


class ModelStage(StrEnum):
    TASK_PARSE = "task_parse"
    PLAN = "plan"
    CRITIC = "critic"
    REPORT = "report"


class RouteMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class TaskCreate(BaseModel):
    goal: str = Field(min_length=3, max_length=4000)
    authorization_scope: str = Field(min_length=3, max_length=2000)
    route_mode: RouteMode = RouteMode.AUTO
    preferred_model: str | None = None
    scene_hint: TaskScene | None = None
    target_url: str | None = None


class TaskRead(TaskCreate):
    id: str
    scene: TaskScene | None = None
    status: TaskStatus
    is_demo: bool = False


class ModelRequest(BaseModel):
    system: str
    user: str
    response_schema: dict[str, Any]


class ModelResponse(BaseModel):
    provider: str
    model: str
    data: dict[str, Any]
    latency_ms: int
    is_demo: bool = False


class PlanStep(BaseModel):
    name: str
    purpose: str
    tool_name: str
    params: dict[str, Any]
    risk_level: RiskLevel
    need_human_confirm: bool = False


class ToolResult(BaseModel):
    success: bool
    summary: str
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
```

```python
# backend/secagent/db.py
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
```

```python
# backend/secagent/db_models.py
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from secagent.db import Base


class TaskRow(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    goal: Mapped[str] = mapped_column(Text)
    authorization_scope: Mapped[str] = mapped_column(Text)
    route_mode: Mapped[str] = mapped_column(String)
    preferred_model: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="created")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
```

```python
# backend/secagent/repository.py
from sqlalchemy import select
from sqlalchemy.orm import Session

from secagent.db_models import TaskRow
from secagent.domain import TaskCreate, TaskRead, TaskStatus


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_task(self, payload: TaskCreate) -> TaskRead:
        row = TaskRow(**payload.model_dump(mode="json"))
        self.session.add(row)
        self.session.commit()
        return self._read(row)

    def get_task(self, task_id: str) -> TaskRead | None:
        row = self.session.get(TaskRow, task_id)
        return self._read(row) if row else None

    def list_tasks(self) -> list[TaskRead]:
        rows = self.session.scalars(select(TaskRow).order_by(TaskRow.created_at.desc())).all()
        return [self._read(row) for row in rows]

    def set_task_status(self, task_id: str, status: TaskStatus, *, is_demo: bool | None = None) -> TaskRead:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        row.status = status.value
        if is_demo is not None:
            row.is_demo = is_demo
        self.session.commit()
        return self._read(row)

    @staticmethod
    def _read(row: TaskRow) -> TaskRead:
        return TaskRead.model_validate(row, from_attributes=True)
```

Implement `backend/secagent/api/tasks.py` with `POST /api/tasks` returning 201, `GET /api/tasks`, and `GET /api/tasks/{id}` returning 404 for unknown IDs. In `create_app()`, create a session factory from `Settings.database_url`, store it in `app.state.session_factory`, and include the tasks router. Import `secagent.db_models` before `Base.metadata.create_all()` so the table is registered.

```python
# backend/secagent/api/tasks.py
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from secagent.domain import TaskCreate, TaskRead
from secagent.repository import TaskRepository

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def get_repository(request: Request) -> Iterator[TaskRepository]:
    with request.app.state.session_factory() as session:
        yield TaskRepository(session)


RepositoryDep = Annotated[TaskRepository, Depends(get_repository)]


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, repository: RepositoryDep) -> TaskRead:
    return repository.create_task(payload)


@router.get("", response_model=list[TaskRead])
def list_tasks(repository: RepositoryDep) -> list[TaskRead]:
    return repository.list_tasks()


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: str, repository: RepositoryDep) -> TaskRead:
    task = repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task
```

The FastAPI dependency above closes the SQLAlchemy session after each request; keep the same dependency for later lifecycle routes.

```python
# backend/tests/conftest.py
import pytest
from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.main import create_app
from secagent.repository import TaskRepository


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", data_dir=tmp_path / "data", model_mode="mock")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as value:
        yield value


@pytest.fixture
def repository(app):
    with app.state.session_factory() as session:
        yield TaskRepository(session)
```

- [ ] **Step 4: Verify CRUD and schema validation**

Run: `python -m pytest backend/tests/integration/test_task_crud.py -v`

Expected: `1 passed`.

- [ ] **Step 5: Commit domain and CRUD**

```bash
git add backend/secagent/domain.py backend/secagent/db.py backend/secagent/db_models.py backend/secagent/repository.py backend/secagent/api/tasks.py backend/secagent/main.py backend/tests/conftest.py backend/tests/integration/test_task_crud.py
git commit -m "feat: add task persistence and CRUD API"
```

---

### Task 3: DeepSeek, GLM, Mock Providers, and Routing Policy

**Files:**
- Modify: `backend/secagent/config.py`
- Create: `backend/secagent/providers/__init__.py`
- Create: `backend/secagent/providers/base.py`
- Create: `backend/secagent/providers/mock.py`
- Create: `backend/secagent/providers/openai_compatible.py`
- Create: `backend/secagent/providers/router.py`
- Test: `backend/tests/unit/test_model_router.py`

**Interfaces:**
- Consumes: `ModelStage`, `ModelRequest`, and `ModelResponse` from Task 2.
- Produces: `ModelProvider.complete(request)`, `ModelRouter.complete(stage, request, preferred=None)`, and exact `auto/live/mock` fallback semantics.

- [ ] **Step 1: Write failing routing-policy tests**

```python
# backend/tests/unit/test_model_router.py
import pytest

from secagent.domain import ModelRequest, ModelResponse, ModelStage
from secagent.providers.router import ModelRouter, ProviderUnavailable


class StubProvider:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name, self.fail = name, fail

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.fail:
            raise ProviderUnavailable(self.name)
        return ModelResponse(provider=self.name, model="stub", data={"ok": True}, latency_ms=1, is_demo=self.name == "mock")


@pytest.mark.asyncio
async def test_auto_routes_parse_to_glm_and_plan_to_deepseek() -> None:
    router = ModelRouter({"glm": StubProvider("glm"), "deepseek": StubProvider("deepseek"), "mock": StubProvider("mock")}, mode="auto")
    request = ModelRequest(system="s", user="u", response_schema={})
    assert (await router.complete(ModelStage.TASK_PARSE, request)).provider == "glm"
    assert (await router.complete(ModelStage.PLAN, request)).provider == "deepseek"


@pytest.mark.asyncio
async def test_live_never_falls_back_to_mock() -> None:
    router = ModelRouter({"glm": StubProvider("glm", True), "deepseek": StubProvider("deepseek", True), "mock": StubProvider("mock")}, mode="live")
    with pytest.raises(ProviderUnavailable):
        await router.complete(ModelStage.TASK_PARSE, ModelRequest(system="s", user="u", response_schema={}))


@pytest.mark.asyncio
async def test_auto_marks_mock_fallback_as_demo() -> None:
    router = ModelRouter({"glm": StubProvider("glm", True), "deepseek": StubProvider("deepseek", True), "mock": StubProvider("mock")}, mode="auto")
    response = await router.complete(ModelStage.CRITIC, ModelRequest(system="s", user="u", response_schema={}))
    assert response.provider == "mock" and response.is_demo is True
```

- [ ] **Step 2: Run tests and verify the provider package is absent**

Run: `python -m pytest backend/tests/unit/test_model_router.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'secagent.providers'`.

- [ ] **Step 3: Implement providers and deterministic routing**

```python
# backend/secagent/providers/base.py
from typing import Protocol

from secagent.domain import ModelRequest, ModelResponse


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
```

```python
# backend/secagent/providers/mock.py
from secagent.domain import ModelRequest, ModelResponse


class MockProvider:
    name = "mock"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(provider="mock", model="deterministic-mock", data={"mock": True}, latency_ms=0, is_demo=True)
```

```python
# backend/secagent/providers/openai_compatible.py
import json
import time

import httpx

from secagent.domain import ModelRequest, ModelResponse


class OpenAICompatibleProvider:
    def __init__(self, *, name: str, base_url: str, api_key: str, model: str, timeout_seconds: float = 30.0) -> None:
        self.name, self.base_url, self.api_key, self.model = name, base_url.rstrip("/"), api_key, model
        self.timeout_seconds = timeout_seconds

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        payload = {"model": self.model, "response_format": {"type": "json_object"}, "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ]}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return ModelResponse(provider=self.name, model=self.model, data=json.loads(content), latency_ms=int((time.perf_counter() - started) * 1000))
```

```python
# backend/secagent/providers/router.py
from secagent.domain import ModelRequest, ModelResponse, ModelStage
from secagent.providers.base import ModelProvider


class ProviderUnavailable(RuntimeError):
    pass


class ModelRouter:
    defaults = {ModelStage.TASK_PARSE: "glm", ModelStage.REPORT: "glm", ModelStage.PLAN: "deepseek", ModelStage.CRITIC: "deepseek"}

    def __init__(self, providers: dict[str, ModelProvider], mode: str = "auto") -> None:
        if mode not in {"auto", "live", "mock"}:
            raise ValueError(f"unsupported model mode: {mode}")
        self.providers, self.mode = providers, mode

    async def complete(self, stage: ModelStage, request: ModelRequest, preferred: str | None = None) -> ModelResponse:
        if self.mode == "mock":
            return await self.providers["mock"].complete(request)
        primary = preferred or self.defaults[stage]
        candidates = [primary] if preferred else [primary, "deepseek" if primary == "glm" else "glm"]
        last_error: Exception | None = None
        for name in dict.fromkeys(candidates):
            try:
                return await self.providers[name].complete(request)
            except Exception as exc:
                last_error = exc
        if self.mode == "auto":
            return await self.providers["mock"].complete(request)
        raise ProviderUnavailable(str(last_error))

    def describe(self) -> list[dict]:
        return [{"name": name, "configured": name == "mock" or provider is not None, "mode": self.mode} for name, provider in self.providers.items()]
```

Extend `Settings` with DeepSeek/GLM key, base URL, model, and timeout fields. Add a provider factory that registers only configured live providers plus Mock. Wrap HTTP timeout/429/5xx errors as `ProviderUnavailable`, retry each live provider twice with bounded backoff, and never include the API key in exception text.

- [ ] **Step 4: Verify routing and provider-contract tests**

Run: `python -m pytest backend/tests/unit/test_model_router.py -v`

Expected: `3 passed`.

- [ ] **Step 5: Commit model routing**

```bash
git add backend/secagent/config.py backend/secagent/providers backend/tests/unit/test_model_router.py
git commit -m "feat: add DeepSeek GLM and mock routing"
```

---

### Task 4: Tool Registry, RiskGate, and Evidence Ledger

**Files:**
- Extend: `backend/secagent/db_models.py`
- Extend: `backend/secagent/repository.py`
- Create: `backend/secagent/tools/__init__.py`
- Create: `backend/secagent/tools/base.py`
- Create: `backend/secagent/tools/registry.py`
- Create: `backend/secagent/agents/__init__.py`
- Create: `backend/secagent/agents/risk.py`
- Create: `backend/secagent/services/__init__.py`
- Create: `backend/secagent/services/ledger.py`
- Modify: `backend/secagent/api/system.py`
- Modify: `backend/secagent/main.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/unit/test_tool_registry_and_risk.py`
- Test: `backend/tests/integration/test_ledger.py`
- Test: `backend/tests/integration/test_system_status.py`

**Interfaces:**
- Consumes: `RiskLevel` and `ToolResult` from Task 2; SQLite session from Task 2.
- Produces: `ToolContext`, `BaseTool`, `ToolRegistry.execute()`, `RiskDecision`, and durable ledger writes.

- [ ] **Step 1: Write failing registry, risk, and ledger tests**

```python
# backend/tests/unit/test_tool_registry_and_risk.py
import pytest

from secagent.agents.risk import RiskGate, RiskRejected
from secagent.domain import RiskLevel, ToolResult
from secagent.tools.base import BaseTool, ToolContext
from secagent.tools.registry import ToolRegistry


class EchoTool(BaseTool):
    name = "echo"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, summary=params["text"])


@pytest.mark.asyncio
async def test_registry_executes_only_allowlisted_scene_tool(tmp_path) -> None:
    registry = ToolRegistry([EchoTool()])
    result = await registry.execute("echo", {"text": "ok"}, ToolContext("t1", "incident_response", tmp_path))
    assert result.summary == "ok"
    with pytest.raises(KeyError):
        await registry.execute("missing", {}, ToolContext("t1", "incident_response", tmp_path))


def test_risk_gate_rejects_forbidden_and_waits_for_medium() -> None:
    gate = RiskGate()
    assert gate.check(RiskLevel.LOW, approved=False).action == "execute"
    assert gate.check(RiskLevel.MEDIUM, approved=False).action == "wait"
    with pytest.raises(RiskRejected):
        gate.check(RiskLevel.FORBIDDEN, approved=True)
```

```python
# backend/tests/integration/test_ledger.py
from secagent.domain import TaskCreate


def test_ledger_persists_masked_model_tool_and_evidence_records(repository, ledger) -> None:
    task = repository.create_task(TaskCreate(goal="分析日志", authorization_scope="仅上传文件"))
    ledger.record_model_call(task.id, provider="glm", stage="task_parse", route_reason="中文任务理解", input_summary="access.log", is_demo=False)
    ledger.record_tool_call(task.id, tool_name="echo", params={"token": "sk-secret"}, result={"summary": "ok"})
    ledger.record_evidence(task.id, evidence_type="raw_line", source="access.log:1", content="GET /admin", confidence=0.9)
    snapshot = ledger.snapshot(task.id)
    assert "sk-secret" not in str(snapshot)
    assert snapshot["evidences"][0]["source"] == "access.log:1"
```

```python
# backend/tests/integration/test_system_status.py
def test_system_status_lists_provider_configuration_and_registered_tools(client) -> None:
    models = client.get("/api/models/status")
    tools = client.get("/api/tools")
    assert models.status_code == tools.status_code == 200
    assert {item["name"] for item in models.json()} >= {"mock"}
    assert all({"name", "scene", "risk_level"} <= item.keys() for item in tools.json())
```

- [ ] **Step 2: Run tests and verify the modules are absent**

Run: `python -m pytest backend/tests/unit/test_tool_registry_and_risk.py backend/tests/integration/test_ledger.py -v`

Expected: collection FAIL because `secagent.tools` and `secagent.services.ledger` do not exist.

- [ ] **Step 3: Implement allowlisted execution, risk decisions, and ledger rows**

```python
# backend/secagent/tools/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from secagent.domain import RiskLevel, ToolResult


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    scene: str
    workspace: Path


class BaseTool(ABC):
    name: str
    scene: str
    risk_level: RiskLevel
    idempotent: bool
    timeout_seconds: float = 30.0

    @abstractmethod
    async def run(self, params: dict, context: ToolContext) -> ToolResult: ...
```

```python
# backend/secagent/tools/registry.py
import asyncio

from secagent.tools.base import BaseTool, ToolContext


class ToolRegistry:
    def __init__(self, tools: list[BaseTool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"tool not registered: {name}")
        return self._tools[name]

    async def execute(self, name: str, params: dict, context: ToolContext):
        tool = self.get(name)
        if tool.scene != context.scene:
            raise PermissionError(f"tool {name} not allowed for {context.scene}")
        return await asyncio.wait_for(tool.run(params, context), timeout=tool.timeout_seconds)

    def describe(self) -> list[dict]:
        return [{"name": t.name, "scene": t.scene, "risk_level": t.risk_level.value} for t in self._tools.values()]
```

```python
# backend/secagent/agents/risk.py
from dataclasses import dataclass

from secagent.domain import RiskLevel


class RiskRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskDecision:
    action: str
    reason: str


class RiskGate:
    def check(self, level: RiskLevel, approved: bool) -> RiskDecision:
        if level in {RiskLevel.HIGH, RiskLevel.FORBIDDEN}:
            raise RiskRejected(f"risk level {level.value} is not executable in MVP")
        if level is RiskLevel.MEDIUM and not approved:
            return RiskDecision("wait", "medium-risk action requires approval")
        return RiskDecision("execute", "risk policy satisfied")
```

Add ORM rows `TaskStepRow`, `ModelCallRow`, `ToolCallRow`, `EvidenceRow`, `ApprovalRow`, and `ReportRow` with task ID, timestamps, JSON/text payloads, status, and indexes on task ID. Implement the repository methods named in “Cross-Task Interfaces”. `LedgerService` must call `redact_mapping()` before persisting params or input summaries and expose `snapshot(task_id) -> dict`.

```python
# addition to backend/tests/conftest.py
from secagent.services.ledger import LedgerService


@pytest.fixture
def ledger(repository):
    return LedgerService(repository)
```

Add exact system routes backed by initialized app state:

```python
# additions to backend/secagent/api/system.py
from fastapi import Request


@router.get("/models/status")
def model_status(request: Request) -> list[dict]:
    return request.app.state.model_router.describe()


@router.get("/tools")
def tool_status(request: Request) -> list[dict]:
    return request.app.state.tool_registry.describe()
```

In `create_app()`, construct the provider dictionary from Settings, create `ModelRouter`, create the initial ToolRegistry, and assign both to `app.state` before including `system_router`.

- [ ] **Step 4: Verify registry, policy, and persistence**

Run: `python -m pytest backend/tests/unit/test_tool_registry_and_risk.py backend/tests/integration/test_ledger.py backend/tests/integration/test_system_status.py -v`

Expected: `3 passed`.

- [ ] **Step 5: Commit the execution boundary**

```bash
git add backend/secagent/db_models.py backend/secagent/repository.py backend/secagent/tools backend/secagent/agents/risk.py backend/secagent/services backend/secagent/api/system.py backend/secagent/main.py backend/tests/conftest.py backend/tests/unit/test_tool_registry_and_risk.py backend/tests/integration/test_ledger.py backend/tests/integration/test_system_status.py
git commit -m "feat: add risk-gated tool registry and ledger"
```

---

### Task 5: Mock-Mode Agent State Machine and Markdown Report

**Files:**
- Modify: `backend/secagent/domain.py`
- Modify: `backend/secagent/providers/mock.py`
- Create: `backend/secagent/agents/parser.py`
- Create: `backend/secagent/agents/planner.py`
- Create: `backend/secagent/agents/executor.py`
- Create: `backend/secagent/agents/critic.py`
- Create: `backend/secagent/agents/reporter.py`
- Create: `backend/secagent/agents/scenes.py`
- Create: `backend/secagent/agents/runner.py`
- Create: `backend/secagent/services/task_service.py`
- Modify: `backend/secagent/api/tasks.py`
- Test: `backend/tests/integration/test_mock_agent_loop.py`

**Interfaces:**
- Consumes: repository, ModelRouter, ToolRegistry, RiskGate, LedgerService, and fixed contracts from Tasks 2–4.
- Produces: `ParsedTask`, `CriticDecision`, `TaskRunResult`, `AgentRunner.run(task_id)`, `POST /api/tasks/{id}/run`, and `GET /api/tasks/{id}/report`.

- [ ] **Step 1: Write a failing end-to-end backend loop test**

```python
# backend/tests/integration/test_mock_agent_loop.py
from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.main import create_app


def test_mock_task_reaches_report_with_traceable_evidence(tmp_path, monkeypatch) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'agent.db'}", model_mode="mock", data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as client:
        task = client.post("/api/tasks", json={
            "goal": "分析示例日志并生成报告",
            "authorization_scope": "仅使用内置示例数据",
            "route_mode": "auto",
        }).json()
        run = client.post(f"/api/tasks/{task['id']}/run")
        assert run.status_code == 202
        detail = client.get(f"/api/tasks/{task['id']}").json()
        assert detail["status"] == "completed"
        assert detail["is_demo"] is True
        assert detail["steps"]
        assert detail["evidences"][0]["source"]
        report = client.get(f"/api/tasks/{task['id']}/report").text
        assert "演示结果" in report
        assert "证据链" in report
```

- [ ] **Step 2: Run the integration test and verify the run route is absent**

Run: `python -m pytest backend/tests/integration/test_mock_agent_loop.py -v`

Expected: FAIL because `POST /api/tasks/{id}/run` returns `404`.

- [ ] **Step 3: Implement the minimal validated loop**

```python
# additions to backend/secagent/domain.py
class ParsedTask(BaseModel):
    scene: TaskScene
    goal: str
    inputs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    authorization_scope: str
    risk_level: RiskLevel
    expected_outputs: list[str] = Field(default_factory=list)


class CriticDecision(BaseModel):
    is_complete: bool
    confidence: float = Field(ge=0, le=1)
    reason: str
    missing_evidence: list[str] = Field(default_factory=list)


class TaskRunResult(BaseModel):
    task_id: str
    status: TaskStatus
    is_demo: bool
    report: str | None = None
```

```python
# backend/secagent/agents/scenes.py
from dataclasses import dataclass

from secagent.domain import TaskScene


@dataclass(frozen=True)
class ScenePolicy:
    scene: TaskScene
    allowed_tools: tuple[str, ...]
    completion_evidence: tuple[str, ...]


SCENES = {
    TaskScene.INCIDENT_RESPONSE: ScenePolicy(TaskScene.INCIDENT_RESPONSE, ("demo_evidence",), ("raw_line",)),
    TaskScene.SOURCE_AUDIT: ScenePolicy(TaskScene.SOURCE_AUDIT, (), ("source_location",)),
    TaskScene.WEB_ANALYSIS: ScenePolicy(TaskScene.WEB_ANALYSIS, (), ("http_observation",)),
}
```

```python
# backend/secagent/agents/runner.py
from secagent.domain import ModelStage, TaskRunResult, TaskStatus, ToolResult
from secagent.tools.base import ToolContext


class AgentRunner:
    def __init__(self, repository, router, registry, ledger, risk_gate, storage, parser, planner, critic, reporter) -> None:
        self.repository, self.router, self.registry = repository, router, registry
        self.ledger, self.risk_gate, self.storage = ledger, risk_gate, storage
        self.parser, self.planner, self.critic, self.reporter = parser, planner, critic, reporter

    async def run(self, task_id: str) -> TaskRunResult:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        try:
            self.repository.set_task_status(task_id, TaskStatus.RUNNING)
            parsed, parse_call = await self.parser.parse(task)
            self.ledger.record_model_response(task_id, ModelStage.TASK_PARSE, parse_call)
            self.repository.set_task_scene(task_id, parsed.scene)
            plan, plan_call = await self.planner.plan(task, parsed)
            self.ledger.record_model_response(task_id, ModelStage.PLAN, plan_call)
            for index, step in enumerate(plan, start=1):
                step_id = self.repository.add_step(task_id, index, step)
                decision = self.risk_gate.check(step.risk_level, approved=False)
                if decision.action == "wait":
                    self.repository.set_task_status(task_id, TaskStatus.WAITING_HUMAN)
                    return TaskRunResult(task_id=task_id, status=TaskStatus.WAITING_HUMAN, is_demo=parse_call.is_demo or plan_call.is_demo)
                context = ToolContext(task_id, parsed.scene.value, self.storage.workspace(task_id))
                result = await self.registry.execute(step.tool_name, step.params, context)
                self.ledger.record_tool_result(task_id, step_id, step.tool_name, step.params, result)
                self.repository.update_step(step_id, "success" if result.success else "failed")
                if not result.success:
                    raise RuntimeError(result.error or result.summary)
            critic, critic_call = await self.critic.review(task_id, parsed)
            self.ledger.record_model_response(task_id, ModelStage.CRITIC, critic_call)
            if not critic.is_complete:
                raise RuntimeError(f"missing evidence: {critic.missing_evidence}")
            report, report_call = await self.reporter.render(task_id, parsed)
            self.ledger.record_model_response(task_id, ModelStage.REPORT, report_call)
            is_demo = any(call.is_demo for call in (parse_call, plan_call, critic_call, report_call))
            self.repository.save_report(task_id, report, is_demo=is_demo)
            self.repository.set_task_status(task_id, TaskStatus.COMPLETED, is_demo=is_demo)
            return TaskRunResult(task_id=task_id, status=TaskStatus.COMPLETED, is_demo=is_demo, report=report)
        except Exception as exc:
            self.ledger.record_error(task_id, type(exc).__name__, str(exc))
            self.repository.set_task_status(task_id, TaskStatus.FAILED_RETRYABLE)
            raise
```

Implement `TaskParser`, `Planner`, `Critic`, and `Reporter` as small classes that call `ModelRouter.complete()` with `ParsedTask.model_json_schema()`, `list[PlanStep]` wrapped in a Pydantic model, `CriticDecision.model_json_schema()`, and a report-section schema. Validate `ModelResponse.data` before returning. Update `MockProvider` to return deterministic stage-specific fixtures based on a required `request.response_schema["title"]`. Add a registered low-risk `demo_evidence` tool that returns one evidence item with source `demo:1`. Reporter renders only ledger snapshot data and prefixes `# 演示结果：` when any call is demo-derived.

```python
# replacement complete() in backend/secagent/providers/mock.py
import json


async def complete(self, request: ModelRequest) -> ModelResponse:
    payload = json.loads(request.user)
    title = request.response_schema.get("title")
    if title == "ParsedTask":
        hint = payload.get("scene_hint")
        goal = payload["goal"]
        scene = hint or ("source_audit" if "源码" in goal else "web_analysis" if "Web" in goal or payload.get("target_url") else "incident_response")
        data = {"scene": scene, "goal": goal, "inputs": payload.get("inputs", []), "constraints": [payload["authorization_scope"]], "authorization_scope": payload["authorization_scope"], "risk_level": "medium" if scene == "web_analysis" else "low", "expected_outputs": ["证据链", "处置建议", "报告"]}
    elif title == "PlanDocument":
        data = {"steps": [{"name": f"执行 {name}", "purpose": f"获取 {name} 的结构化证据", "tool_name": name, "params": payload["params_by_tool"].get(name, {}), "risk_level": payload["risk_by_tool"][name], "need_human_confirm": payload["risk_by_tool"][name] == "medium"} for name in payload["allowed_tools"]]}
    elif title == "CriticDecision":
        data = {"is_complete": bool(payload["evidence_count"]), "confidence": 0.9 if payload["evidence_count"] else 0.0, "reason": "存在可追溯工具证据" if payload["evidence_count"] else "缺少工具证据", "missing_evidence": [] if payload["evidence_count"] else ["工具证据"]}
    elif title == "ReportSections":
        data = {"summary": payload["goal"], "findings": payload["findings"], "recommendations": payload["recommendations"], "uncertainties": payload["errors"]}
    else:
        raise ValueError(f"unsupported mock schema: {title}")
    return ModelResponse(provider="mock", model="deterministic-mock", data=data, latency_ms=0, is_demo=True)
```

Expose run as a deterministic synchronous await inside the 202 response for this task; Task 6 moves it behind the lifecycle service. Extend task detail serialization with `steps`, `model_calls`, `tool_calls`, `evidences`, and report metadata.

- [ ] **Step 4: Verify the complete Mock loop**

Run: `python -m pytest backend/tests/integration/test_mock_agent_loop.py -v`

Expected: `1 passed` and no external HTTP requests.

- [ ] **Step 5: Commit the first vertical closed loop**

```bash
git add backend/secagent/domain.py backend/secagent/providers/mock.py backend/secagent/agents backend/secagent/services/task_service.py backend/secagent/api/tasks.py backend/tests/integration/test_mock_agent_loop.py
git commit -m "feat: complete mock agent evidence loop"
```

---

### Task 6: Safe Uploads and Full Task Lifecycle

**Files:**
- Modify: `backend/secagent/config.py`
- Create: `backend/secagent/security/__init__.py`
- Create: `backend/secagent/security/redaction.py`
- Create: `backend/secagent/security/files.py`
- Create: `backend/secagent/services/storage.py`
- Modify: `backend/secagent/services/task_service.py`
- Modify: `backend/secagent/api/tasks.py`
- Modify: `backend/secagent/main.py`
- Test: `backend/tests/unit/test_file_security.py`
- Test: `backend/tests/integration/test_task_lifecycle.py`

**Interfaces:**
- Consumes: task status/repository and AgentRunner from Tasks 2 and 5.
- Produces: task-isolated uploads, safe archive extraction, pause/resume/approve/retry/cancel endpoints, and interrupted-task recovery.

- [ ] **Step 1: Write failing file and lifecycle tests**

```python
# backend/tests/unit/test_file_security.py
import io
import zipfile

import pytest

from secagent.security.files import UnsafeArchive, extract_zip_safely


def test_zip_path_traversal_is_rejected(tmp_path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.py", "print('bad')")
    with pytest.raises(UnsafeArchive):
        extract_zip_safely(archive, tmp_path / "out", max_files=20, max_bytes=1024)


def test_safe_zip_is_extracted_under_destination(tmp_path) -> None:
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("src/app.py", "print('not executed')")
    files = extract_zip_safely(archive, tmp_path / "out", max_files=20, max_bytes=4096)
    assert files == [tmp_path / "out" / "src" / "app.py"]
```

```python
# backend/tests/integration/test_task_lifecycle.py
from secagent.domain import TaskStatus


def test_pause_resume_cancel_and_recover(client, repository) -> None:
    task = client.post("/api/tasks", json={"goal": "分析示例日志", "authorization_scope": "仅示例", "route_mode": "auto"}).json()
    task_id = task["id"]
    assert client.post(f"/api/tasks/{task_id}/pause").json()["status"] == "paused"
    assert client.post(f"/api/tasks/{task_id}/resume").status_code == 202
    assert client.post(f"/api/tasks/{task_id}/cancel").json()["status"] == "cancelled"
    repository.set_task_status(task_id, TaskStatus.RUNNING)
    assert repository.recover_interrupted_tasks() == 1
    assert repository.get_task(task_id).status is TaskStatus.FAILED_RETRYABLE
```

- [ ] **Step 2: Run tests and verify security/lifecycle behavior is missing**

Run: `python -m pytest backend/tests/unit/test_file_security.py backend/tests/integration/test_task_lifecycle.py -v`

Expected: collection FAIL because `secagent.security.files` does not exist.

- [ ] **Step 3: Implement bounded storage and lifecycle controls**

```python
# backend/secagent/security/files.py
import stat
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


class UnsafeArchive(ValueError):
    pass


def extract_zip_safely(archive: Path, destination: Path, *, max_files: int, max_bytes: int) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as source:
        members = [item for item in source.infolist() if not item.is_dir()]
        if len(members) > max_files or sum(item.file_size for item in members) > max_bytes:
            raise UnsafeArchive("archive exceeds extraction limits")
        outputs: list[Path] = []
        root = destination.resolve()
        for item in members:
            if stat.S_ISLNK(item.external_attr >> 16):
                raise UnsafeArchive("archive symbolic link detected")
            logical = PurePosixPath(item.filename)
            if logical.is_absolute() or ".." in logical.parts:
                raise UnsafeArchive("archive path traversal detected")
            target = (destination / Path(*logical.parts)).resolve()
            if root not in target.parents:
                raise UnsafeArchive("archive target escapes workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as reader, target.open("wb") as writer:
                writer.write(reader.read())
            outputs.append(target)
        return outputs
```

```python
# backend/secagent/security/redaction.py
import re
from typing import Any

SECRET = re.compile(r"(?i)(sk-|api[_-]?key[=: ]+|bearer )[A-Za-z0-9._-]{8,}")


def redact_text(value: str) -> str:
    return SECRET.sub(lambda match: match.group(0)[:4] + "****" + match.group(0)[-4:], value)


def redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value
```

`StorageService` must write uploads to `<data_dir>/tasks/<task_id>/uploads`, generate server-side names, retain original name/hash/size as metadata, reject files and archives over configured limits before extraction, and expose `workspace(task_id) -> Path`.

`TaskService` must enforce legal transitions, set a cooperative pause/cancel flag checked between tool calls, resume from the first unfinished step, create `ApprovalRow` records, and recover startup `running` rows to `failed_retryable`. Add multipart upload to create-task without removing JSON support. Add pause, resume, approve, retry, and cancel routes with 409 on illegal transitions. Run recovery during FastAPI lifespan startup.

```python
# transition policy in backend/secagent/services/task_service.py
TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PARSED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PLANNED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.WAITING_HUMAN, TaskStatus.PAUSED, TaskStatus.COMPLETED, TaskStatus.FAILED_RETRYABLE, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.WAITING_HUMAN: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.PAUSED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.FAILED_RETRYABLE: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def require_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise ValueError(f"illegal task transition: {current.value} -> {target.value}")
```

```python
# JSON/multipart request parser in backend/secagent/api/tasks.py
from fastapi import UploadFile


async def parse_task_create(request: Request) -> tuple[TaskCreate, UploadFile | None]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return TaskCreate.model_validate(await request.json()), None
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw_payload = form.get("payload")
        upload = form.get("file")
        if not isinstance(raw_payload, str):
            raise HTTPException(status_code=422, detail="multipart payload is required")
        if upload is not None and not isinstance(upload, UploadFile):
            raise HTTPException(status_code=422, detail="file must be an upload")
        return TaskCreate.model_validate_json(raw_payload), upload
    raise HTTPException(status_code=415, detail="use application/json or multipart/form-data")
```

The create route calls `StorageService.save_upload(task_id, upload)` after the task row exists and rolls back the task plus workspace if file validation fails. Lifecycle routes delegate to `TaskService`, translate illegal transitions to HTTP 409, and return the updated TaskRead or 202 run acknowledgement.

- [ ] **Step 4: Verify archive isolation and lifecycle transitions**

Run: `python -m pytest backend/tests/unit/test_file_security.py backend/tests/integration/test_task_lifecycle.py -v`

Expected: `3 passed`.

- [ ] **Step 5: Commit storage and lifecycle safety**

```bash
git add backend/secagent/config.py backend/secagent/security backend/secagent/services/storage.py backend/secagent/services/task_service.py backend/secagent/api/tasks.py backend/secagent/main.py backend/tests/unit/test_file_security.py backend/tests/integration/test_task_lifecycle.py
git commit -m "feat: secure uploads and task lifecycle"
```

---

### Task 7: Log Incident-Response Vertical Slice

**Files:**
- Create: `backend/secagent/tools/log_tools.py`
- Modify: `backend/secagent/agents/scenes.py`
- Modify: `backend/secagent/main.py`
- Create: `backend/tests/fixtures/access_attack.log`
- Test: `backend/tests/unit/test_log_tools.py`
- Test: `backend/tests/integration/test_log_scene.py`
- Create: `demo_cases/logs/access_attack.log`

**Interfaces:**
- Consumes: `BaseTool`, `ToolContext`, `ToolResult`, registry, scene policy, and AgentRunner.
- Produces: `log_type_detector`, `log_analyzer`, `attack_pattern_detector`, `timeline_builder`, and a complete LogAgent policy.

- [ ] **Step 1: Write failing deterministic log-tool tests**

```python
# backend/tests/unit/test_log_tools.py
import pytest

from secagent.tools.base import ToolContext
from secagent.tools.log_tools import AttackPatternDetector, LogAnalyzer, LogTypeDetector, TimelineBuilder


@pytest.mark.asyncio
async def test_log_chain_detects_known_scan(tmp_path) -> None:
    log = tmp_path / "access.log"
    log.write_text(
        '203.0.113.24 - - [10/Aug/2026:12:00:00 +0000] "GET /admin HTTP/1.1" 404 10\n'
        '203.0.113.24 - - [10/Aug/2026:12:00:01 +0000] "GET /.git/config HTTP/1.1" 404 10\n'
        '203.0.113.24 - - [10/Aug/2026:12:00:02 +0000] "GET /backup.zip HTTP/1.1" 404 10\n',
        encoding="utf-8",
    )
    context = ToolContext("t1", "incident_response", tmp_path)
    assert (await LogTypeDetector().run({"file_path": str(log)}, context)).findings[0]["log_type"] == "nginx_combined"
    analysis = await LogAnalyzer().run({"file_path": str(log)}, context)
    patterns = await AttackPatternDetector().run({"events": analysis.evidence}, context)
    timeline = await TimelineBuilder().run({"events": analysis.evidence}, context)
    assert patterns.findings[0]["rule_id"] == "WEB-SCAN-002"
    assert timeline.evidence[0]["source"].startswith("access.log:")
```

- [ ] **Step 2: Run the test and verify log tools are absent**

Run: `python -m pytest backend/tests/unit/test_log_tools.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'secagent.tools.log_tools'`.

- [ ] **Step 3: Implement four log tools and scene policy**

```python
# core parser in backend/secagent/tools/log_tools.py
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from secagent.domain import RiskLevel, ToolResult
from secagent.tools.base import BaseTool, ToolContext

ACCESS_RE = re.compile(r'(?P<ip>\S+) \S+ \S+ \[(?P<time>[^]]+)] "(?P<method>\S+) (?P<path>\S+) [^"]+" (?P<status>\d{3}) (?P<size>\S+)')
SENSITIVE = ("/admin", "/.git", "/backup", "/.env", "/wp-login")


def parse_access(path: Path) -> list[dict]:
    events = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        match = ACCESS_RE.match(raw)
        if match:
            event = match.groupdict() | {"line": line_number, "raw_line": raw, "source": f"{path.name}:{line_number}"}
            events.append(event)
    return events


class LogTypeDetector(BaseTool):
    name, scene, risk_level, idempotent = "log_type_detector", "incident_response", RiskLevel.LOW, True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        events = parse_access(Path(params["file_path"]))
        return ToolResult(success=bool(events), summary="识别访问日志", findings=[{"log_type": "nginx_combined" if events else "unknown"}], evidence=[])


class LogAnalyzer(BaseTool):
    name, scene, risk_level, idempotent = "log_analyzer", "incident_response", RiskLevel.LOW, True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        events = parse_access(Path(params["file_path"]))
        return ToolResult(success=True, summary=f"解析 {len(events)} 条日志", metrics={"top_ips": Counter(e["ip"] for e in events).most_common(10), "status_codes": Counter(e["status"] for e in events)}, evidence=events)


class AttackPatternDetector(BaseTool):
    name, scene, risk_level, idempotent = "attack_pattern_detector", "incident_response", RiskLevel.LOW, True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        events = params["events"]
        grouped = Counter(e["ip"] for e in events if any(marker in e["path"] for marker in SENSITIVE))
        findings = [{"rule_id": "WEB-SCAN-002", "ip": ip, "count": count, "reason": "短时间访问多个敏感路径", "confidence": min(0.99, 0.6 + count / 20)} for ip, count in grouped.items() if count >= 3]
        return ToolResult(success=True, summary=f"发现 {len(findings)} 个扫描来源", findings=findings, evidence=events)


class TimelineBuilder(BaseTool):
    name, scene, risk_level, idempotent = "timeline_builder", "incident_response", RiskLevel.LOW, True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        ordered = sorted(params["events"], key=lambda e: datetime.strptime(e["time"], "%d/%b/%Y:%H:%M:%S %z"))
        return ToolResult(success=True, summary=f"生成 {len(ordered)} 项时间线", evidence=ordered)
```

Register all four tools. Replace the incident-response scene policy with the ordered chain and completion evidence `raw_line`, `rule_id`, and `timeline`. Copy the same stable fixture into `demo_cases/logs/access_attack.log`. The integration test must upload this file, run in Mock mode, and assert a completed report containing `WEB-SCAN-002` and at least one `access_attack.log:<line>` source.

- [ ] **Step 4: Verify unit and complete-scene behavior**

Run: `python -m pytest backend/tests/unit/test_log_tools.py backend/tests/integration/test_log_scene.py -v`

Expected: all tests PASS and the report contains traceable raw-line evidence.

- [ ] **Step 5: Commit the log slice**

```bash
git add backend/secagent/tools/log_tools.py backend/secagent/agents/scenes.py backend/secagent/main.py backend/tests/fixtures/access_attack.log backend/tests/unit/test_log_tools.py backend/tests/integration/test_log_scene.py demo_cases/logs/access_attack.log
git commit -m "feat: add explainable log incident analysis"
```

---

### Task 8: Static Source-Audit Vertical Slice

**Files:**
- Create: `backend/secagent/tools/source_tools.py`
- Modify: `backend/secagent/agents/scenes.py`
- Modify: `backend/secagent/main.py`
- Create: `backend/tests/fixtures/vulnerable_app/app.py`
- Create: `backend/tests/fixtures/vulnerable_app/config.py`
- Test: `backend/tests/unit/test_source_tools.py`
- Test: `backend/tests/integration/test_source_scene.py`
- Create: `demo_cases/source_audit/vulnerable_app/app.py`
- Create: `demo_cases/source_audit/vulnerable_app/config.py`

**Interfaces:**
- Consumes: safe extraction, ToolRegistry, scene policy, and report pipeline.
- Produces: `project_detector`, `source_scanner`, `secret_scanner`, `config_checker`, and CodeAuditAgent policy without executing source.

- [ ] **Step 1: Write failing static-analysis tests**

```python
# backend/tests/unit/test_source_tools.py
import pytest

from secagent.tools.base import ToolContext
from secagent.tools.source_tools import ConfigChecker, ProjectDetector, SecretScanner, SourceScanner


@pytest.mark.asyncio
async def test_static_tools_find_seeded_python_risks_without_execution(tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text('import os\nAPI_KEY = "sk-demo-not-real-123456"\ndef run(cmd):\n    return os.system(cmd)\n', encoding="utf-8")
    config = tmp_path / "config.py"
    config.write_text("DEBUG = True\n", encoding="utf-8")
    context = ToolContext("t1", "source_audit", tmp_path)
    assert (await ProjectDetector().run({"project_path": str(tmp_path)}, context)).findings[0]["language"] == "python"
    source_result = await SourceScanner().run({"project_path": str(tmp_path)}, context)
    secret_result = await SecretScanner().run({"project_path": str(tmp_path)}, context)
    config_result = await ConfigChecker().run({"project_path": str(tmp_path)}, context)
    assert source_result.findings[0]["rule_id"] == "PY-CMD-001"
    assert "sk-demo-not-real-123456" not in str(secret_result.model_dump())
    assert config_result.findings[0]["rule_id"] == "CFG-DEBUG-001"
```

- [ ] **Step 2: Run the test and verify source tools are absent**

Run: `python -m pytest backend/tests/unit/test_source_tools.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'secagent.tools.source_tools'`.

- [ ] **Step 3: Implement bounded static scanners**

```python
# rule definitions in backend/secagent/tools/source_tools.py
import re
from pathlib import Path

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.tools.base import BaseTool, ToolContext

SOURCE_RULES = {
    ".py": [("PY-CMD-001", re.compile(r"\b(os\.system|eval|exec|pickle\.loads|subprocess\.(run|Popen))\s*\("), "高风险执行函数")],
    ".php": [("PHP-CMD-001", re.compile(r"\b(eval|exec|system|shell_exec)\s*\("), "高风险执行函数")],
    ".js": [("JS-DOM-001", re.compile(r"\.innerHTML\s*="), "潜在 DOM 注入")],
    ".ts": [("TS-DOM-001", re.compile(r"\.innerHTML\s*="), "潜在 DOM 注入")],
}
SECRET_RE = re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"]([^'\"]{8,})['\"]")


def source_files(root: Path, *, max_files: int = 2000, max_file_bytes: int = 1_000_000):
    count = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > max_file_bytes or any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue
        count += 1
        if count > max_files:
            raise ValueError("source file limit exceeded")
        yield path


def finding(path: Path, line_number: int, rule_id: str, evidence: str, suggestion: str) -> dict:
    return {"file": str(path), "line": line_number, "rule_id": rule_id, "risk_level": "high", "evidence": redact_text(evidence.strip()), "suggestion": suggestion, "source": f"{path.name}:{line_number}"}
```

Implement four `BaseTool` classes using `source_files()`: ProjectDetector counts supported suffixes; SourceScanner applies `SOURCE_RULES` line by line; SecretScanner stores only `redact_text(raw)` and never the captured secret; ConfigChecker detects `DEBUG=True`, wildcard CORS, and disabled TLS verification. All tools are low risk, idempotent, and return a source location per finding.

Register the tools and replace the source-audit policy with the ordered chain. Add exact fixture files from Step 1 to both test fixtures and `demo_cases`. The integration test uploads a ZIP created in test memory, asserts extraction under the task workspace, completes the report, and confirms `PY-CMD-001`, `CFG-DEBUG-001`, masked secret text, and file:line evidence.

- [ ] **Step 4: Verify static tools and complete source scene**

Run: `python -m pytest backend/tests/unit/test_source_tools.py backend/tests/integration/test_source_scene.py -v`

Expected: all tests PASS; no fixture module is imported or executed.

- [ ] **Step 5: Commit the source-audit slice**

```bash
git add backend/secagent/tools/source_tools.py backend/secagent/agents/scenes.py backend/secagent/main.py backend/tests/fixtures/vulnerable_app backend/tests/unit/test_source_tools.py backend/tests/integration/test_source_scene.py demo_cases/source_audit
git commit -m "feat: add static source audit scene"
```

---

### Task 9: SSRF-Safe Passive Web-Analysis Vertical Slice

**Files:**
- Modify: `backend/secagent/config.py`
- Create: `backend/secagent/security/url_guard.py`
- Create: `backend/secagent/tools/web_tools.py`
- Modify: `backend/secagent/agents/scenes.py`
- Modify: `backend/secagent/main.py`
- Test: `backend/tests/unit/test_url_guard.py`
- Test: `backend/tests/unit/test_web_tools.py`
- Test: `backend/tests/integration/test_web_scene.py`
- Create: `web-demo/nginx.conf`
- Create: `web-demo/html/index.html`

**Interfaces:**
- Consumes: ToolRegistry, medium-risk approval flow, scene policy, and Evidence Ledger.
- Produces: `UrlGuard.check(url)`, `url_guard`, `http_fetch`, `header_check`, `form_extract`, and a passive WebAgent policy.

- [ ] **Step 1: Write failing SSRF and redirect tests**

```python
# backend/tests/unit/test_url_guard.py
import pytest

from secagent.security.url_guard import BlockedUrl, UrlGuard


def test_guard_blocks_private_loopback_and_metadata_addresses() -> None:
    guard = UrlGuard(allowed_hosts=set(), resolver=lambda host: ["127.0.0.1"])
    with pytest.raises(BlockedUrl):
        guard.check("http://example.test/")
    for url in ("http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "http://user:pass@example.com/"):
        with pytest.raises(BlockedUrl):
            guard.check(url)


def test_explicit_demo_host_is_allowed_but_not_arbitrary_private_host() -> None:
    guard = UrlGuard(allowed_hosts={"web-demo"}, resolver=lambda host: ["172.20.0.10"])
    assert guard.check("http://web-demo/").hostname == "web-demo"
    with pytest.raises(BlockedUrl):
        guard.check("http://internal-admin/")
```

```python
# backend/tests/unit/test_web_tools.py
import httpx
import pytest

from secagent.security.url_guard import UrlGuard
from secagent.tools.base import ToolContext
from secagent.tools.web_tools import HttpFetch


@pytest.mark.asyncio
async def test_fetch_revalidates_redirect_target(tmp_path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}))
    tool = HttpFetch(UrlGuard(set(), resolver=lambda host: ["93.184.216.34"]), transport=transport)
    with pytest.raises(Exception, match="blocked"):
        await tool.run({"url": "https://example.com"}, ToolContext("t1", "web_analysis", tmp_path))
```

- [ ] **Step 2: Run tests and verify URL security modules are absent**

Run: `python -m pytest backend/tests/unit/test_url_guard.py backend/tests/unit/test_web_tools.py -v`

Expected: collection FAIL because `secagent.security.url_guard` does not exist.

- [ ] **Step 3: Implement URL guard and passive tools**

```python
# backend/secagent/security/url_guard.py
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import SplitResult, urlsplit


class BlockedUrl(ValueError):
    pass


def default_resolver(host: str) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


class UrlGuard:
    def __init__(self, allowed_hosts: set[str], resolver: Callable[[str], list[str]] = default_resolver) -> None:
        self.allowed_hosts, self.resolver = {host.lower() for host in allowed_hosts}, resolver

    def check(self, url: str) -> SplitResult:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise BlockedUrl("blocked URL syntax or scheme")
        host = parsed.hostname.lower().rstrip(".")
        if host in self.allowed_hosts:
            return parsed
        for value in self.resolver(host):
            address = ipaddress.ip_address(value)
            if not address.is_global or address.is_loopback or address.is_link_local or address.is_private or address.is_reserved:
                raise BlockedUrl(f"blocked non-public address for {host}")
        return parsed
```

```python
# guarded fetch core in backend/secagent/tools/web_tools.py
from urllib.parse import urljoin

import httpx

from secagent.domain import RiskLevel, ToolResult
from secagent.security.url_guard import BlockedUrl, UrlGuard
from secagent.tools.base import BaseTool, ToolContext


class HttpFetch(BaseTool):
    name, scene, risk_level, idempotent = "http_fetch", "web_analysis", RiskLevel.MEDIUM, True

    def __init__(self, guard: UrlGuard, transport=None, max_redirects: int = 3, max_body_bytes: int = 1_000_000) -> None:
        self.guard, self.transport, self.max_redirects, self.max_body_bytes = guard, transport, max_redirects, max_body_bytes

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        current = params["url"]
        async with httpx.AsyncClient(transport=self.transport, follow_redirects=False, timeout=10.0) as client:
            for _ in range(self.max_redirects + 1):
                self.guard.check(current)
                response = await client.get(current, headers={"User-Agent": "SecAgent-X/0.1 Passive Analyzer"})
                if response.is_redirect:
                    current = urljoin(current, response.headers["location"])
                    continue
                body = response.content[: self.max_body_bytes]
                return ToolResult(success=True, summary=f"HTTP {response.status_code}", evidence=[{"source": current, "status_code": response.status_code, "headers": dict(response.headers), "body_preview": body.decode(response.encoding or "utf-8", errors="replace")}], warnings=["响应体已截断"] if len(response.content) > len(body) else [])
        raise BlockedUrl("blocked redirect limit exceeded")
```

Add low-risk `url_guard`, `header_check`, and `form_extract` tools. `header_check` consumes only the recorded response headers and reports missing CSP, HSTS, X-Content-Type-Options, and frame policy as observations rather than confirmed vulnerabilities. `form_extract` parses the recorded HTML with a standard-library `HTMLParser`, returns action/method/input names, and never submits a request. Register all four tools; Web scene uses ordered `url_guard → http_fetch → header_check → form_extract` and requires one medium-risk approval before the first fetch.

Create a static `web-demo/html/index.html` with one GET search form and intentionally missing two security headers; `nginx.conf` serves only that page. The integration test sets `WEB_ALLOWED_HOSTS=web-demo`, uses a local mock transport, approves the task, and asserts the report records the final URL, status, headers, and form fields. It must also assert that a redirect to `127.0.0.1` fails before a second request.

- [ ] **Step 4: Verify URL security and complete Web scene**

Run: `python -m pytest backend/tests/unit/test_url_guard.py backend/tests/unit/test_web_tools.py backend/tests/integration/test_web_scene.py -v`

Expected: all tests PASS; no test performs an external network request.

- [ ] **Step 5: Commit the passive Web slice**

```bash
git add backend/secagent/config.py backend/secagent/security/url_guard.py backend/secagent/tools/web_tools.py backend/secagent/agents/scenes.py backend/secagent/main.py backend/tests/unit/test_url_guard.py backend/tests/unit/test_web_tools.py backend/tests/integration/test_web_scene.py web-demo
git commit -m "feat: add SSRF-safe passive web analysis"
```

---

### Task 10: Vue Console Shell, Task Creation, and Task List

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/router.ts`
- Create: `frontend/src/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/stores/tasks.ts`
- Create: `frontend/src/views/DashboardView.vue`
- Create: `frontend/src/views/TaskCreateView.vue`
- Create: `frontend/src/views/TaskListView.vue`
- Test: `frontend/tests/task-create.spec.ts`

**Interfaces:**
- Consumes: task CRUD and model/tool status API from backend Tasks 2–9.
- Produces: routed Vue shell, typed REST client, task store, creation form, and list page.

- [ ] **Step 1: Write a failing task-creation component test**

```typescript
// frontend/tests/task-create.spec.ts
import { mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import TaskCreateView from '../src/views/TaskCreateView.vue'

const push = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

beforeEach(() => { push.mockReset(); vi.restoreAllMocks() })

it('submits goal, authorization, and automatic routing', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 't1', status: 'created' }) }))
  const wrapper = mount(TaskCreateView)
  await wrapper.get('[data-test="goal"]').setValue('分析 access.log 中的异常行为')
  await wrapper.get('[data-test="authorization"]').setValue('仅分析上传日志')
  await wrapper.get('form').trigger('submit')
  expect(fetch).toHaveBeenCalledWith('/api/tasks', expect.objectContaining({ method: 'POST' }))
  expect(push).toHaveBeenCalledWith('/tasks/t1')
})
```

- [ ] **Step 2: Install frontend dependencies and verify the view is absent**

Run: `cd frontend && npm install && npm run test -- --run tests/task-create.spec.ts`

Expected: FAIL because `src/views/TaskCreateView.vue` does not exist.

- [ ] **Step 3: Implement the typed shell and creation flow**

```json
// frontend/package.json
{
  "name": "secagent-x-frontend",
  "private": true,
  "type": "module",
  "scripts": {"dev": "vite", "build": "vue-tsc -b && vite build", "test": "vitest"},
  "dependencies": {"@element-plus/icons-vue": "^2.3.1", "element-plus": "^2.9.1", "pinia": "^2.3.0", "vue": "^3.5.13", "vue-router": "^4.5.0"},
  "devDependencies": {"@vitejs/plugin-vue": "^5.2.1", "@vue/test-utils": "^2.4.6", "jsdom": "^25.0.1", "typescript": "~5.7.2", "vite": "^6.0.5", "vitest": "^2.1.8", "vue-tsc": "^2.2.0"}
}
```

```typescript
// frontend/src/types.ts
export type TaskStatus = 'created' | 'parsed' | 'planned' | 'running' | 'waiting_human' | 'paused' | 'completed' | 'failed_retryable' | 'failed' | 'cancelled'
export type RouteMode = 'auto' | 'manual'
export interface Task { id: string; goal: string; authorization_scope: string; route_mode: RouteMode; preferred_model?: string; scene_hint?: string; target_url?: string; scene?: string; status: TaskStatus; is_demo: boolean }
export interface TaskCreate { goal: string; authorization_scope: string; route_mode: RouteMode; preferred_model?: string; scene_hint?: string; target_url?: string }
```

```typescript
// frontend/src/api/client.ts
import type { Task, TaskCreate } from '../types'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`)
  return response.json() as Promise<T>
}

export const api = {
  createTask: (payload: TaskCreate, file?: File) => {
    if (!file) return request<Task>('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    const body = new FormData()
    body.set('payload', JSON.stringify(payload))
    body.set('file', file)
    return request<Task>('/api/tasks', { method: 'POST', body })
  },
  listTasks: () => request<Task[]>('/api/tasks'),
  getTask: (id: string) => request<Task>(`/api/tasks/${id}`),
}
```

```vue
<!-- frontend/src/views/TaskCreateView.vue -->
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'

const router = useRouter()
const submitting = ref(false)
const error = ref('')
const upload = ref<File>()
const form = reactive({ goal: '', authorization_scope: '', route_mode: 'auto' as 'auto' | 'manual', preferred_model: '', scene_hint: '', target_url: '' })

async function submit() {
  submitting.value = true; error.value = ''
  try { const task = await api.createTask({ ...form, preferred_model: form.preferred_model || undefined, scene_hint: form.scene_hint || undefined, target_url: form.target_url || undefined }, upload.value); await router.push(`/tasks/${task.id}`) }
  catch (value) { error.value = value instanceof Error ? value.message : '创建任务失败' }
  finally { submitting.value = false }
}
</script>

<template>
  <section><h1>创建安全任务</h1>
    <form @submit.prevent="submit">
      <label>任务目标<textarea data-test="goal" v-model="form.goal" required minlength="3" /></label>
      <label>授权范围<textarea data-test="authorization" v-model="form.authorization_scope" required minlength="3" /></label>
      <label>场景<select v-model="form.scene_hint"><option value="">自动识别</option><option value="incident_response">日志应急</option><option value="source_audit">源码审计</option><option value="web_analysis">Web 分析</option></select></label>
      <label>模型模式<select v-model="form.route_mode"><option value="auto">自动协同</option><option value="manual">手动指定</option></select></label>
      <label v-if="form.route_mode === 'manual'">指定模型<select v-model="form.preferred_model"><option value="deepseek">DeepSeek</option><option value="glm">GLM</option></select></label>
      <label>授权 URL<input v-model="form.target_url" type="url" /></label>
      <label>上传材料<input aria-label="上传材料" type="file" @change="upload = ($event.target as HTMLInputElement).files?.[0]" /></label>
      <p v-if="error" role="alert">{{ error }}</p>
      <button :disabled="submitting" type="submit">{{ submitting ? '创建中' : '创建任务' }}</button>
    </form>
  </section>
</template>
```

Create `App.vue` with left navigation for 总览、创建任务、任务中心、报告、模型与工具; create routes `/`, `/tasks/new`, `/tasks`, `/tasks/:id`; initialize Vue, Pinia, Router, and Element Plus in `main.ts`. `TaskListView` loads `api.listTasks()` and renders status, scene, route mode, demo badge, and a detail link. `DashboardView` shows only API-backed task counts and service status, without invented metrics.

- [ ] **Step 4: Verify component tests and production build**

Run: `cd frontend && npm run test -- --run && npm run build`

Expected: all Vitest tests PASS and Vite exits 0 with `dist/` output.

- [ ] **Step 5: Commit the first UI slice**

```bash
git add frontend
git commit -m "feat: add Vue task creation console"
```

---

### Task 11: Task Detail Timeline, Evidence, Approval, and Reports UI

**Files:**
- Extend: `frontend/src/types.ts`
- Extend: `frontend/src/api/client.ts`
- Extend: `frontend/src/stores/tasks.ts`
- Create: `frontend/src/views/TaskDetailView.vue`
- Create: `frontend/src/views/ReportsView.vue`
- Create: `frontend/src/views/SystemView.vue`
- Create: `frontend/src/components/StepTimeline.vue`
- Create: `frontend/src/components/EvidencePanel.vue`
- Create: `frontend/src/components/ModelRoutePanel.vue`
- Create: `frontend/src/components/ApprovalDialog.vue`
- Test: `frontend/tests/task-detail.spec.ts`
- Test: `frontend/tests/task-store.spec.ts`

**Interfaces:**
- Consumes: enriched task detail, lifecycle routes, report, provider status, and tool status.
- Produces: approved task-detail information hierarchy, safe action controls, polling, and Markdown report view.

- [ ] **Step 1: Write failing timeline/evidence rendering test**

```typescript
// frontend/tests/task-detail.spec.ts
import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import StepTimeline from '../src/components/StepTimeline.vue'
import EvidencePanel from '../src/components/EvidencePanel.vue'

it('shows model route, tool, source, and confidence without hiding demo state', () => {
  const step = { id: 's1', name: '检测攻击模式', status: 'success', model_provider: 'deepseek', tool_name: 'attack_pattern_detector', route_reason: '技术复核' }
  const evidence = { id: 'e1', evidence_type: 'raw_line', source: 'access.log:42', content: 'GET /admin', confidence: 0.86 }
  const timeline = mount(StepTimeline, { props: { steps: [step], isDemo: true } })
  const panel = mount(EvidencePanel, { props: { evidences: [evidence] } })
  expect(timeline.text()).toContain('演示结果')
  expect(timeline.text()).toContain('DeepSeek')
  expect(panel.text()).toContain('access.log:42')
  expect(panel.text()).toContain('0.86')
})
```

- [ ] **Step 2: Run tests and verify detail components are absent**

Run: `cd frontend && npm run test -- --run tests/task-detail.spec.ts tests/task-store.spec.ts`

Expected: FAIL because `StepTimeline.vue`, `EvidencePanel.vue`, and the polling store contract do not exist.

- [ ] **Step 3: Implement task polling and approved information hierarchy**

```typescript
// additions to frontend/src/types.ts
export interface TaskStep { id: string; name: string; purpose?: string; status: string; model_provider?: string; tool_name?: string; route_reason?: string; risk_level?: string }
export interface Evidence { id: string; evidence_type: string; source: string; content: string; confidence: number }
export interface ModelCall { id: string; provider: string; model: string; stage: string; route_reason: string; latency_ms: number; is_demo: boolean }
export interface TaskDetail extends Task { steps: TaskStep[]; evidences: Evidence[]; model_calls: ModelCall[]; pending_approval?: { step_id: string; tool_name: string; risk_level: string; params_summary: string } }
```

```typescript
// lifecycle additions to frontend/src/api/client.ts
export const lifecycle = {
  run: (id: string) => request(`/api/tasks/${id}/run`, { method: 'POST' }),
  pause: (id: string) => request(`/api/tasks/${id}/pause`, { method: 'POST' }),
  resume: (id: string) => request(`/api/tasks/${id}/resume`, { method: 'POST' }),
  retry: (id: string) => request(`/api/tasks/${id}/retry`, { method: 'POST' }),
  cancel: (id: string) => request(`/api/tasks/${id}/cancel`, { method: 'POST' }),
  approve: (id: string, approved: boolean, reason: string) => request(`/api/tasks/${id}/approve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved, reason }) }),
  report: (id: string) => fetch(`/api/tasks/${id}/report`).then(async r => { if (!r.ok) throw new Error(await r.text()); return r.text() }),
}
```

`TaskDetailView` must match the approved layout: task header and status summary, left execution timeline, right evidence ledger with tabs for evidence/tool/model records, lifecycle buttons derived from legal states, and ApprovalDialog showing exact target/tool/risk before confirmation. Poll every two seconds only while status is `running`; stop on unmount or terminal status. `StepTimeline` renders purpose, provider, route reason, tool, risk, and status. `EvidencePanel` renders source and confidence. `ModelRoutePanel` renders provider/model/stage/reason/latency/demo. ReportsView renders escaped Markdown text or a sanitized Markdown renderer; SystemView displays only `/api/models/status` and `/api/tools` results.

- [ ] **Step 4: Verify UI tests and build**

Run: `cd frontend && npm run test -- --run && npm run build`

Expected: all tests PASS; build exits 0; no TypeScript errors.

- [ ] **Step 5: Commit explainable task UI**

```bash
git add frontend/src frontend/tests
git commit -m "feat: show task decisions evidence and reports"
```

---

### Task 12: Docker Deployment, Three-Scene E2E, Coverage Gate, and Operator Docs

**Files:**
- Create: `.env.example`
- Create: `.gitignore`
- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Create: `docker-compose.yml`
- Create: `package.json`
- Create: `playwright.config.ts`
- Create: `scripts/build_demo_archives.py`
- Create: `e2e/three-scenes.spec.ts`
- Create: `README.md`
- Create: `docs/deployment.md`
- Create: `docs/demo-script.md`
- Modify: `web-demo/nginx.conf`

**Interfaces:**
- Consumes: complete backend/frontend and fixed Demo data from Tasks 1–11.
- Produces: one-command CPU deployment, health checks, three stable Demo flows, coverage enforcement, and reproducible operator instructions.

- [ ] **Step 1: Write the failing three-scene Playwright acceptance test**

```typescript
// e2e/three-scenes.spec.ts
import { expect, test } from '@playwright/test'

const cases = [
  { name: '日志应急', goal: '分析示例日志中的攻击行为', fixture: 'demo_cases/logs/access_attack.log', expected: 'WEB-SCAN-002' },
  { name: '源码审计', goal: '审计示例源码', fixture: 'demo_cases/source_audit/vulnerable_app.zip', expected: 'PY-CMD-001' },
  { name: 'Web 分析', goal: '被动分析授权测试站点', url: 'http://web-demo', expected: '响应头' },
]

for (const item of cases) {
  test(`${item.name} produces a traceable report`, async ({ page }) => {
    await page.goto('/tasks/new')
    await page.getByLabel('任务目标').fill(item.goal)
    await page.getByLabel('授权范围').fill('仅限内置 Demo 材料和 web-demo')
    if (item.fixture) await page.getByLabel('上传材料').setInputFiles(item.fixture)
    if (item.url) await page.getByLabel('授权 URL').fill(item.url)
    await page.getByRole('button', { name: '创建任务' }).click()
    await page.getByRole('button', { name: '开始执行' }).click()
    if (item.name === 'Web 分析') {
      await page.getByRole('button', { name: '确认执行' }).click()
    }
    await expect(page.getByText('已完成')).toBeVisible({ timeout: 120_000 })
    await page.getByRole('link', { name: '查看报告' }).click()
    await expect(page.getByText(item.expected)).toBeVisible()
    await expect(page.getByText(/证据来源|证据链/)).toBeVisible()
  })
}
```

- [ ] **Step 2: Run acceptance commands and verify deployment artifacts are absent**

Run: `docker compose config && npx playwright test e2e/three-scenes.spec.ts`

Expected: FAIL because `docker-compose.yml` and Playwright configuration do not exist.

- [ ] **Step 3: Add reproducible Docker and operator configuration**

```yaml
# docker-compose.yml
services:
  backend:
    build:
      context: .
      dockerfile: backend/Dockerfile
    env_file: .env
    environment:
      DATABASE_URL: sqlite:////data/secagent.db
      DATA_DIR: /data
      WEB_ALLOWED_HOSTS: web-demo
    volumes: ["secagent-data:/data"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 5s
      timeout: 3s
      retries: 20
  frontend:
    build:
      context: .
      dockerfile: frontend/Dockerfile
    ports: ["8080:80"]
    depends_on:
      backend: { condition: service_healthy }
  web-demo:
    image: nginx:1.27-alpine
    volumes:
      - ./web-demo/html:/usr/share/nginx/html:ro
      - ./web-demo/nginx.conf:/etc/nginx/conf.d/default.conf:ro

volumes:
  secagent-data:
```

```dockerfile
# backend/Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY backend ./backend
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 secagent && mkdir -p /data && chown -R secagent:secagent /app /data
USER secagent
EXPOSE 8000
CMD ["uvicorn", "secagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```dockerfile
# frontend/Dockerfile
FROM node:24-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM nginx:1.27-alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
```

```json
// package.json
{
  "name": "secagent-x-e2e",
  "private": true,
  "scripts": {"build:fixtures": "python scripts/build_demo_archives.py", "test:e2e": "playwright test"},
  "devDependencies": {"@playwright/test": "^1.49.1"}
}
```

```python
# scripts/build_demo_archives.py
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
source = root / "demo_cases" / "source_audit" / "vulnerable_app"
target = root / "demo_cases" / "source_audit" / "vulnerable_app.zip"
with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(source.parent).as_posix())
```

```typescript
// playwright.config.ts
import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  retries: process.env.CI ? 1 : 0,
  use: { baseURL: 'http://127.0.0.1:8080', trace: 'on-first-retry' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'docker compose up --build',
    url: 'http://127.0.0.1:8080/api/health',
    timeout: 180_000,
    reuseExistingServer: !process.env.CI,
  },
})
```

`frontend/nginx.conf` must serve the SPA and proxy `/api/` to `http://backend:8000`. `.env.example` lists DeepSeek/GLM keys as empty, base URLs/models, `MODEL_MODE=mock`, upload/archive/step/re-plan/time limits, and `WEB_ALLOWED_HOSTS=web-demo`; it contains no real secret. `.gitignore` excludes `.env`, Python caches, `.venv`, `node_modules`, `dist`, Playwright artifacts, SQLite, task data, uploads, reports, and `.superpowers/`.

Run `npm run build:fixtures` before E2E so the source ZIP is deterministic. The frontend Nginx `/api/` proxy makes the configured health URL reachable through port 8080.

README must contain exact Windows local commands, Docker commands, model modes, safety limitations, and all test commands. `docs/deployment.md` must cover environment variables, volume backup, health checks, and recovery of `failed_retryable` tasks. `docs/demo-script.md` must give a 6–8 minute sequence for the three scenes and explicitly distinguish Mock from live results.

- [ ] **Step 4: Run the full verification matrix**

Run backend: `python -m pytest --cov=secagent --cov-report=term-missing --cov-fail-under=80`

Expected: all backend tests PASS and total coverage is at least 80%.

Run frontend: `cd frontend && npm run test -- --run && npm run build`

Expected: all Vitest tests PASS and production build exits 0.

Run containers: `docker compose config && docker compose up --build -d && docker compose ps`

Expected: backend healthy; frontend and web-demo running.

Run E2E: `npx playwright test e2e/three-scenes.spec.ts`

Expected: `3 passed`, each under 120 seconds.

Run safety regressions: `python -m pytest backend/tests/unit/test_file_security.py backend/tests/unit/test_url_guard.py backend/tests/unit/test_tool_registry_and_risk.py -v`

Expected: all safety tests PASS.

- [ ] **Step 5: Commit the deployable MVP**

```bash
git add .env.example .gitignore backend/Dockerfile frontend/Dockerfile frontend/nginx.conf docker-compose.yml package.json playwright.config.ts scripts/build_demo_archives.py e2e README.md docs/deployment.md docs/demo-script.md web-demo/nginx.conf
git commit -m "feat: deliver Dockerized three-scene MVP"
```

---

## Final Release Checklist

- [ ] `git status --short` is empty.
- [ ] `python -m pytest --cov=secagent --cov-fail-under=80` passes.
- [ ] `cd frontend && npm run test -- --run && npm run build` passes.
- [ ] `docker compose config` passes.
- [ ] `docker compose up --build -d` reports backend healthy.
- [ ] `npx playwright test e2e/three-scenes.spec.ts` reports three passing scenarios.
- [ ] No tracked file contains a real DeepSeek or GLM key.
- [ ] Mock tasks and reports visibly show `is_demo=true` and “演示结果”.
- [ ] Web redirects are revalidated and private addresses are blocked except exact `WEB_ALLOWED_HOSTS` entries.
- [ ] Uploaded source and archives are never executed.
- [ ] Every key report conclusion contains a tool result or original source location.

## Spec Coverage Matrix

| Approved specification section | Implemented by plan tasks |
|---|---|
| Goals, scope, and technical stack | Tasks 1–12 and Global Constraints |
| Overall architecture and core components | Tasks 2–5 |
| Model routing and Mock labeling | Tasks 3 and 5 |
| Task state machine and lifecycle | Tasks 5 and 6 |
| Log incident response | Task 7 |
| Static source audit | Tasks 6 and 8 |
| Passive Web analysis | Task 9 |
| Risk control, upload safety, SSRF, and redaction | Tasks 4, 6, 8, and 9 |
| SQLite data model and Evidence Ledger | Tasks 2 and 4 |
| REST API | Tasks 2, 4, 5, and 6 |
| Approved Vue console layout | Tasks 10 and 11 |
| Failure handling and recovery | Tasks 3, 5, and 6 |
| Unit, integration, security, and E2E testing | Every task, with release gate in Task 12 |
| CPU Docker deployment and operator documentation | Task 12 |
