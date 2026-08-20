# SecAgent-X Team Production Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing three-scene SecAgent-X MVP into a recoverable, authenticated, observable intranet team service with PostgreSQL, Redis/Celery workers, and verified fixed-stage DeepSeek/GLM calls.

**Architecture:** Keep FastAPI, Vue, the three scene agents, RiskGate, ToolRegistry, and Evidence Ledger. Move durable state to PostgreSQL, submit executions through a `JobQueue` boundary backed by Celery/Redis, execute at most three jobs in workers, and persist task events for SSE. Add local accounts with short-lived JWT access tokens, rotating refresh sessions, owner/admin authorization, and provider-specific DeepSeek and GLM adapters.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, Redis 7, Celery 5.6, PyJWT, Argon2id, httpx, Vue 3, Pinia, TypeScript, Vitest, Playwright, Docker Compose.

## Global Constraints

- Deployment target is an intranet team of at most 10 users and exactly 3 maximum concurrent security tasks.
- Roles are `admin` and `analyst`; analysts can access only their own tasks and administrators can access all tasks.
- DeepSeek performs only `PLAN` and `CRITIC`; GLM performs only `TASK_PARSE` and `REPORT`.
- Production mode never falls back across providers and never falls back to Mock.
- Default live models are `deepseek-v4-pro` and `glm-5.2`, with environment overrides.
- GPT remains excluded.
- Medium-risk tools require human approval; high and forbidden actions remain rejected.
- Uploaded source is never imported, executed, or used to install dependencies.
- Web analysis remains passive GET-only with per-hop SSRF validation.
- Model keys and JWT signing keys never enter Git, browser responses, database fields, reports, audit payloads, or logs.
- Keep Windows development and Linux CPU Docker Compose support.
- Raise core backend coverage from 80% to 85%.

---

## File Structure Map

Backend boundaries to create:

- `backend/secagent/config_secrets.py`: file-first Secret loading and validation.
- `backend/secagent/auth/`: password hashing, access tokens, refresh sessions, authenticated-user dependency, and bootstrap CLI.
- `backend/secagent/queue/`: `JobQueue` protocol, Celery application, Celery adapter, and test fake.
- `backend/secagent/services/audit.py`: append-only audit writes.
- `backend/secagent/services/job_service.py`: durable job claim, heartbeat, retry, and orphan recovery.
- `backend/secagent/services/task_events.py`: durable monotonically ordered task events and SSE reads.
- `backend/secagent/providers/http_client.py`: pooled HTTP transport, retry classification, and response metadata.
- `backend/secagent/providers/deepseek.py`: DeepSeek-specific request and response behavior.
- `backend/secagent/providers/glm.py`: GLM-specific request and response behavior.
- `backend/secagent/worker.py`: Celery task entry point that creates its own database session and runs one task.
- `backend/secagent/cli.py`: initial administrator creation and provider connectivity checks.
- `migrations/`: Alembic environment and versioned PostgreSQL schema.
- `scripts/migrate_sqlite_to_postgres.py`: dry-run-first existing-data importer.

Frontend boundaries to create:

- `frontend/src/stores/auth.ts`: in-memory access token, user, login, refresh, and logout.
- `frontend/src/api/http.ts`: authenticated fetch with one refresh-and-retry cycle.
- `frontend/src/views/LoginView.vue`: login form.
- `frontend/src/views/TeamView.vue`: administrator user management.
- `frontend/src/views/AuditView.vue`: administrator audit log.
- `frontend/src/components/WorkerStatus.vue`: worker and queue summary.
- `frontend/src/composables/useTaskEvents.ts`: resumable task SSE subscription.

Keep existing scene tools focused in their current files. Split orchestration and infrastructure; do not rewrite deterministic scanners solely for style.

---

### Task 1: Production Settings and File-First Secrets

**Files:**
- Create: `backend/secagent/config_secrets.py`
- Modify: `backend/secagent/config.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Test: `backend/tests/unit/test_config_secrets.py`

**Interfaces:**
- Produces: `read_secret(value: str | None, file_path: Path | None) -> str | None`.
- Produces: `Settings.deepseek_key()`, `Settings.glm_key()`, and `Settings.jwt_key()` returning stripped Secret values.
- Produces settings for PostgreSQL, Redis, Celery concurrency, JWT lifetimes, job lease, heartbeat, and model budgets.

- [ ] **Step 1: Write failing Secret precedence tests**

```python
from pathlib import Path

import pytest

from secagent.config_secrets import read_secret


def test_secret_file_takes_precedence(tmp_path: Path):
    secret_file = tmp_path / "key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    assert read_secret("env-secret", secret_file) == "file-secret"


def test_missing_secret_file_is_an_error(tmp_path: Path):
    with pytest.raises(ValueError, match="secret file does not exist"):
        read_secret(None, tmp_path / "missing")


def test_blank_secret_is_treated_as_unconfigured():
    assert read_secret("   ", None) is None
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_config_secrets.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'secagent.config_secrets'`.

- [ ] **Step 3: Implement file-first Secret loading and settings**

```python
# backend/secagent/config_secrets.py
from pathlib import Path


def read_secret(value: str | None, file_path: Path | None) -> str | None:
    if file_path is not None:
        if not file_path.is_file():
            raise ValueError(f"secret file does not exist: {file_path}")
        value = file_path.read_text(encoding="utf-8")
    normalized = (value or "").strip()
    return normalized or None
```

Add these exact settings and accessors to `Settings`:

```python
database_url: str = "postgresql+psycopg://secagent:secagent@postgres/secagent"
redis_url: str = "redis://redis:6379/0"
worker_concurrency: int = 3
job_lease_seconds: int = 90
job_heartbeat_seconds: int = 15
job_auto_retries: int = 1
jwt_access_minutes: int = 15
jwt_refresh_days: int = 7
cookie_secure: bool = False
jwt_signing_key: str | None = None
jwt_signing_key_file: Path | None = None
deepseek_api_key_file: Path | None = None
glm_api_key_file: Path | None = None
max_model_calls_per_task: int = 8
max_input_tokens_per_task: int = 120_000
max_output_tokens_per_task: int = 24_000

def deepseek_key(self) -> str | None:
    return read_secret(self.deepseek_api_key, self.deepseek_api_key_file)

def glm_key(self) -> str | None:
    return read_secret(self.glm_api_key, self.glm_api_key_file)

def jwt_key(self) -> str | None:
    return read_secret(self.jwt_signing_key, self.jwt_signing_key_file)
```

Add `alembic>=1.14,<2`, `psycopg[binary]>=3.2,<4`, `redis>=6,<7`, `celery[redis]>=5.6,<5.7`, `PyJWT[crypto]>=2.10,<3`, and `argon2-cffi>=23.1,<26` to project dependencies.

- [ ] **Step 4: Run focused tests and import checks**

Run: `\.venv\Scripts\python.exe -m pip install -e ".[dev]"; \.venv\Scripts\python.exe -m pytest backend/tests/unit/test_config_secrets.py -v`

Expected: 3 passed.

- [ ] **Step 5: Commit the settings slice**

```powershell
git add pyproject.toml .env.example backend/secagent/config.py backend/secagent/config_secrets.py backend/tests/unit/test_config_secrets.py
git commit -m "feat: add production settings and secret loading"
```

---

### Task 2: PostgreSQL Schema and Alembic Migrations

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260814_01_team_schema.py`
- Modify: `backend/secagent/db.py`
- Modify: `backend/secagent/db_models.py`
- Modify: `backend/secagent/domain.py`
- Test: `backend/tests/unit/test_schema.py`

**Interfaces:**
- Produces ORM rows: `UserRow`, `RefreshSessionRow`, `JobRunRow`, `TaskEventRow`, and `AuditEventRow`.
- Extends `TaskRow` with `owner_id`, `status_version`, model/token budgets, and updated timestamp.
- Extends model/tool/evidence/approval/report rows with production metadata defined by the approved spec.
- Produces `TaskStatus.QUEUED = "queued"` while preserving `TaskStatus.CANCELLED = "cancelled"`.

- [ ] **Step 1: Write a failing schema contract test**

```python
from sqlalchemy import inspect

from secagent.db import Base, make_engine


def test_team_schema_has_required_tables(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    assert {
        "users", "refresh_sessions", "tasks", "job_runs", "task_events",
        "task_steps", "model_calls", "tool_calls", "evidences",
        "approvals", "reports", "audit_events",
    } <= tables
```

- [ ] **Step 2: Run the schema test and verify missing tables**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_schema.py -v`

Expected: FAIL because `users`, `refresh_sessions`, `job_runs`, `task_events`, and `audit_events` are absent.

- [ ] **Step 3: Add the engine boundary and ORM rows**

Replace implicit `create_all` startup behavior with explicit functions:

```python
def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    connect_args = {"check_same_thread": False} if url.drivername.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(make_engine(database_url), expire_on_commit=False)
```

Add rows with these required unique/index contracts:

```python
class UserRole(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"


class UserRow(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class TaskEventRow(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
```

Define these columns explicitly:

- `tasks`: `owner_id`, `status_version`, `current_step_index`, `max_model_calls`, `max_input_tokens`, `max_output_tokens`, `updated_at`;
- `job_runs`: `task_id`, `broker_id`, `command_id`, `worker_id`, `attempt`, `status`, `lease_expires_at`, `heartbeat_at`, `started_at`, `finished_at`, with unique `command_id`;
- `task_steps`: `idempotency_key`, `attempt`, `result_json`, and unique `(task_id, step_index)` plus `(task_id, idempotency_key)`;
- `model_calls`: `request_id`, `finish_reason`, `prompt_tokens`, `completion_tokens`, `retry_count`, `status`, `error_code`;
- `tool_calls`: `duration_ms`, `attempt`, `error_code`;
- `evidences`: `sha256`, `file_ref`, with unique `(task_id, sha256, source)`;
- `approvals`: `decided_by`, `expires_at`;
- `reports`: `version`, `evidence_ids_json`, with unique `(task_id, version)`;
- `audit_events`: `actor_id`, `action`, `resource_type`, `resource_id`, `outcome`, `ip_address`, `details_json`, `created_at`.

Define foreign keys with `ondelete="CASCADE"` for task-owned rows and `ondelete="RESTRICT"` for task owners and audit actors.

- [ ] **Step 4: Generate and verify the Alembic migration**

The migration `upgrade()` must create the 12 tables, indexes, foreign keys, unique constraints, and the `tasks.owner_id` relationship. `downgrade()` must drop them in reverse dependency order.

Run: `$env:DATABASE_URL='sqlite:///./data/migration-test.db'; \.venv\Scripts\python.exe -m alembic upgrade head; \.venv\Scripts\python.exe -m alembic current; Remove-Item Env:DATABASE_URL`

Expected: current revision is `20260814_01 (head)` against a configured disposable database.

- [ ] **Step 5: Run schema tests**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_schema.py backend/tests/unit/test_db.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit schema and migrations**

```powershell
git add alembic.ini migrations backend/secagent/db.py backend/secagent/db_models.py backend/secagent/domain.py backend/tests/unit/test_schema.py backend/tests/unit/test_db.py
git commit -m "feat: add PostgreSQL team schema and migrations"
```

---

### Task 3: Passwords, JWT Sessions, and Authentication API

**Files:**
- Create: `backend/secagent/auth/__init__.py`
- Create: `backend/secagent/auth/passwords.py`
- Create: `backend/secagent/auth/tokens.py`
- Create: `backend/secagent/auth/dependencies.py`
- Create: `backend/secagent/services/auth_service.py`
- Create: `backend/secagent/api/auth.py`
- Create: `backend/secagent/cli.py`
- Modify: `backend/secagent/main.py`
- Modify: `backend/secagent/repository.py`
- Test: `backend/tests/unit/test_tokens.py`
- Test: `backend/tests/integration/test_auth_api.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Produces `AuthenticatedUser(id: str, username: str, role: UserRole)`.
- Produces `AuthService.login`, `refresh`, `logout`, and `create_user`.
- Produces FastAPI dependencies `current_user` and `require_admin`.
- Produces CLI command `python -m secagent.cli create-admin --username admin` with interactive password input.
- Produces test fixtures `seeded_admin`, `seeded_analyst`, `admin_client`, and `analyst_client`; each client obtains a real access token through `/api/auth/login`.

- [ ] **Step 1: Write failing token and login tests**

```python
def test_access_token_round_trip():
    token = issue_access_token("user-1", "analyst", "secret", minutes=15)
    claims = decode_access_token(token, "secret")
    assert claims.sub == "user-1"
    assert claims.role == "analyst"


def test_login_sets_refresh_cookie(client, seeded_analyst):
    response = client.post("/api/auth/login", json={"username": "alice", "password": "Correct-Horse-9"})
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "alice"
    assert response.json()["access_token"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
```

- [ ] **Step 2: Run tests and verify missing auth modules/routes**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_tokens.py backend/tests/integration/test_auth_api.py -v`

Expected: FAIL because token helpers and `/api/auth/login` do not exist.

- [ ] **Step 3: Implement password and token primitives**

```python
# passwords.py
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

def hash_password(password: str) -> str:
    return _hasher.hash(password)

def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
```

```python
# tokens.py
class AccessClaims(BaseModel):
    sub: str
    role: Literal["admin", "analyst"]
    exp: int
    iat: int
    jti: str

def issue_access_token(user_id: str, role: str, key: str, minutes: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "role": role, "iat": now, "exp": now + timedelta(minutes=minutes), "jti": str(uuid4())}
    return jwt.encode(payload, key, algorithm="HS256")

def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hashlib.sha256(raw.encode()).hexdigest()
```

- [ ] **Step 4: Implement auth service, cookies, dependencies, and bootstrap CLI**

The login response is exactly:

```python
class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserRead
```

Set refresh cookies with `httponly=True`, `samesite="strict"`, `secure=settings.cookie_secure`, `path="/api/auth"`, and `max_age=settings.jwt_refresh_days * 86400`. Store only the SHA-256 refresh hash. Rotate on every refresh and revoke the prior row in the same transaction.

- [ ] **Step 5: Run authentication tests**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_tokens.py backend/tests/integration/test_auth_api.py -v`

Expected: all tests pass, including invalid password, disabled user, expired access token, refresh rotation, refresh replay rejection, and logout revocation.

- [ ] **Step 6: Commit authentication**

```powershell
git add backend/secagent/auth backend/secagent/services/auth_service.py backend/secagent/api/auth.py backend/secagent/cli.py backend/secagent/main.py backend/secagent/repository.py backend/tests/conftest.py backend/tests/unit/test_tokens.py backend/tests/integration/test_auth_api.py
git commit -m "feat: add local JWT authentication"
```

---

### Task 4: Task Ownership, RBAC, User Administration, and Audit

**Files:**
- Create: `backend/secagent/services/audit.py`
- Create: `backend/secagent/api/errors.py`
- Create: `backend/secagent/api/admin.py`
- Modify: `backend/secagent/api/tasks.py`
- Modify: `backend/secagent/api/system.py`
- Modify: `backend/secagent/repository.py`
- Modify: `backend/secagent/services/task_service.py`
- Test: `backend/tests/integration/test_rbac.py`
- Test: `backend/tests/integration/test_admin_users.py`
- Test: `backend/tests/integration/test_audit_events.py`
- Test: `backend/tests/integration/test_error_contract.py`

**Interfaces:**
- Produces `TaskRepository.get_authorized(task_id, actor)` and `list_authorized(actor)`.
- Produces `AuditService.record(actor_id, action, resource_type, resource_id, outcome, details)`.
- Produces `/api/admin/users` and `/api/admin/audit-events`.

- [ ] **Step 1: Write failing object authorization tests**

```python
def test_analyst_cannot_read_another_users_task(alice_client, bob_task):
    response = alice_client.get(f"/api/tasks/{bob_task.id}")
    assert response.status_code == 403


def test_admin_can_read_any_task(admin_client, bob_task):
    response = admin_client.get(f"/api/tasks/{bob_task.id}")
    assert response.status_code == 200


def test_forbidden_access_is_audited(alice_client, bob_task, repository):
    alice_client.get(f"/api/tasks/{bob_task.id}")
    event = repository.latest_audit_event("task.access_denied")
    assert event.resource_id == bob_task.id
    assert event.outcome == "denied"
```

- [ ] **Step 2: Run RBAC tests and verify unauthorized access currently succeeds**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_rbac.py -v`

Expected: FAIL because task routes do not require identity or ownership.

- [ ] **Step 3: Implement resource authorization and append-only audit**

Use this authorization rule in repository/service boundaries:

```python
def can_access_task(actor: AuthenticatedUser, owner_id: str) -> bool:
    return actor.role is UserRole.ADMIN or actor.id == owner_id

def require_task_access(actor: AuthenticatedUser, task: TaskRead) -> None:
    if not can_access_task(actor, task.owner_id):
        raise ForbiddenResource("task")
```

Every create, run, pause, resume, retry, cancel, approve, login failure, user change, provider check, and denied access records one audit event after redacting details.

Register exception handlers that return exactly `{"error":{"code":str,"message":str,"fields":list|None,"trace_id":str}}`. Map authentication to 401, authorization to 403, missing resources to 404, state conflicts to 409, validation to 422, and unexpected failures to 500 without stack traces or external response bodies.

- [ ] **Step 4: Implement administrator user routes**

`POST /api/admin/users` accepts `username`, `password`, and `role`; rejects duplicate usernames with 409. `PATCH /api/admin/users/{id}` accepts only `is_active` or a replacement password. Prevent an administrator from disabling the last active administrator.

Disabling a user or replacing a password revokes every active refresh session for that user in the same transaction. Pending approvals expire after 24 hours; attempting to decide an expired approval returns 409, records `approval.expired`, and leaves the task in `waiting_human` until it is cancelled or explicitly replanned.

- [ ] **Step 5: Run RBAC, admin, audit, and existing task tests**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_rbac.py backend/tests/integration/test_admin_users.py backend/tests/integration/test_audit_events.py backend/tests/integration/test_error_contract.py backend/tests/integration/test_task_crud.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit authorization and audit**

```powershell
git add backend/secagent/services/audit.py backend/secagent/api/admin.py backend/secagent/api/errors.py backend/secagent/api/tasks.py backend/secagent/api/system.py backend/secagent/repository.py backend/secagent/services/task_service.py backend/tests/integration/test_rbac.py backend/tests/integration/test_admin_users.py backend/tests/integration/test_audit_events.py backend/tests/integration/test_error_contract.py backend/tests/integration/test_task_crud.py
git commit -m "feat: enforce task ownership and audit actions"
```

---

### Task 5: JobQueue Boundary and Celery/Redis Worker

**Files:**
- Create: `backend/secagent/queue/__init__.py`
- Create: `backend/secagent/queue/base.py`
- Create: `backend/secagent/queue/fake.py`
- Create: `backend/secagent/queue/celery_app.py`
- Create: `backend/secagent/queue/celery_queue.py`
- Create: `backend/secagent/worker.py`
- Modify: `backend/secagent/api/tasks.py`
- Modify: `backend/secagent/services/task_service.py`
- Modify: `backend/secagent/main.py`
- Test: `backend/tests/unit/test_job_queue.py`
- Test: `backend/tests/integration/test_task_enqueue.py`

**Interfaces:**
- Produces `JobQueue.enqueue(task_id: str, command_id: str) -> str`.
- Produces `FakeJobQueue.enqueued: list[QueuedJob]` for tests.
- Produces Celery task `secagent.run_task(task_id: str, command_id: str)`.

- [ ] **Step 1: Write a failing enqueue idempotency test**

```python
def test_run_endpoint_enqueues_once(auth_client, owned_task, fake_queue):
    headers = {"Idempotency-Key": "run-001"}
    first = auth_client.post(f"/api/tasks/{owned_task.id}/run", headers=headers)
    second = auth_client.post(f"/api/tasks/{owned_task.id}/run", headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["status"] == "queued"
    assert [job.task_id for job in fake_queue.enqueued] == [owned_task.id]
```

- [ ] **Step 2: Run the enqueue test and verify inline execution behavior**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_task_enqueue.py -v`

Expected: FAIL because `/run` executes the agent immediately and no queue exists.

- [ ] **Step 3: Implement the queue protocol and fake**

```python
class JobQueue(Protocol):
    def enqueue(self, task_id: str, command_id: str) -> str: ...


@dataclass(frozen=True)
class QueuedJob:
    task_id: str
    command_id: str
    broker_id: str
```

`FakeJobQueue` deduplicates by `command_id`. `CeleryJobQueue` calls `run_task.apply_async(args=[task_id, command_id], task_id=command_id)` and returns the Celery ID.

- [ ] **Step 4: Configure Celery and worker entry**

Use JSON-only serialization and these exact settings:

```python
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_ignore_result=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.worker_concurrency,
    broker_connection_retry_on_startup=True,
)
```

The Celery actor opens a new SQLAlchemy session, constructs `TaskService`, and passes only `task_id` and `command_id`; no ORM object, Secret, upload content, or model prompt enters the broker message.

- [ ] **Step 5: Change lifecycle commands to enqueue**

`run`, approved `resume`, and `retry` transition to `queued` and call `JobQueue.enqueue`. Reject missing `Idempotency-Key` with 400 on mutating execution commands. Approval rejection transitions to `cancelled`; approval acceptance queues a new run command.

- [ ] **Step 6: Run queue and lifecycle tests**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_job_queue.py backend/tests/integration/test_task_enqueue.py backend/tests/integration/test_task_lifecycle.py -v`

Expected: all tests pass and no test starts a real Celery worker.

- [ ] **Step 7: Commit the queue slice**

```powershell
git add backend/secagent/queue backend/secagent/worker.py backend/secagent/api/tasks.py backend/secagent/services/task_service.py backend/secagent/main.py backend/tests/unit/test_job_queue.py backend/tests/integration/test_task_enqueue.py backend/tests/integration/test_task_lifecycle.py
git commit -m "feat: enqueue task execution through Celery"
```

---

### Task 6: Durable Job Leases, Idempotent Steps, Task Events, and SSE

**Files:**
- Create: `backend/secagent/services/job_service.py`
- Create: `backend/secagent/services/task_events.py`
- Create: `backend/secagent/api/events.py`
- Create: `backend/secagent/auth/stream_tickets.py`
- Modify: `backend/secagent/worker.py`
- Modify: `backend/secagent/repository.py`
- Modify: `backend/secagent/agents/runner.py`
- Modify: `backend/secagent/main.py`
- Test: `backend/tests/unit/test_job_leases.py`
- Test: `backend/tests/integration/test_task_events.py`
- Test: `backend/tests/integration/test_job_recovery.py`

**Interfaces:**
- Produces `JobService.claim`, `heartbeat`, `finish`, and `recover_expired`.
- Produces `TaskEventService.append(task_id, event_type, payload) -> int` and `after(task_id, event_id) -> list[TaskEvent]`.
- Produces `POST /api/tasks/{task_id}/event-ticket`, returning a one-time 60-second ticket scoped to the current user and task.
- Produces `GET /api/tasks/{task_id}/events` as `text/event-stream` with `Last-Event-ID` support.

- [ ] **Step 1: Write failing lease recovery and SSE tests**

```python
def test_expired_job_is_requeued_within_budget(job_service, fake_queue, running_job):
    running_job.lease_expires_at = utcnow() - timedelta(seconds=1)
    recovered = job_service.recover_expired()
    assert recovered == 1
    assert fake_queue.enqueued[-1].task_id == running_job.task_id


def test_events_resume_after_last_event(auth_client, owned_task, event_service):
    first = event_service.append(owned_task.id, "task.queued", {"attempt": 1})
    second = event_service.append(owned_task.id, "task.running", {"attempt": 1})
    response = auth_client.get(
        f"/api/tasks/{owned_task.id}/events",
        headers={"Last-Event-ID": str(first)},
    )
    assert f"id: {second}" in response.text
    assert "event: task.running" in response.text
    assert "task.queued" not in response.text
```

- [ ] **Step 2: Run tests and verify missing lease/event services**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_job_leases.py backend/tests/integration/test_task_events.py -v`

Expected: FAIL because the services and event route do not exist.

- [ ] **Step 3: Implement atomic claims and heartbeats**

`claim` performs a conditional update from `queued` to `running` using `status_version`; it creates one `JobRunRow`, sets `lease_expires_at`, and emits `task.running`. Heartbeat updates only the active `job_run` owned by the current Worker. A stale Worker that lost its lease cannot finish the run.

Use:

```python
@dataclass(frozen=True)
class JobLease:
    job_run_id: str
    task_id: str
    worker_id: str
    attempt: int
    lease_expires_at: datetime
```

- [ ] **Step 4: Add step idempotency and durable events**

Before executing a step, query `(task_id, idempotency_key)`. Return the prior successful result when the step and evidence hashes are complete. Insert each task event in the same transaction as its state change.

Event payloads are passed through `redact_value` and limited to 16 KiB serialized JSON.

- [ ] **Step 5: Implement resumable SSE**

Use `StreamingResponse` and produce exact SSE frames:

```python
yield f"id: {event.id}\nevent: {event.event_type}\ndata: {event.payload_json}\n\n"
```

Poll PostgreSQL once per second, send a comment heartbeat every 15 seconds, stop when the client disconnects, and enforce the same task ownership check as the detail route.

Sign stream tickets with the JWT signing key and claims `sub`, `task_id`, `exp`, `jti`, and `purpose="task_events"`. Store the SHA-256 of `jti` on first use for 60 seconds in Redis via `SET key value NX EX 60`; reject replay, wrong task, wrong user, wrong purpose, or expired tickets.

- [ ] **Step 6: Run lease, recovery, event, and runner tests**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_job_leases.py backend/tests/integration/test_job_recovery.py backend/tests/integration/test_task_events.py backend/tests/integration/test_mock_agent_loop.py -v`

Expected: all tests pass, including stale Worker rejection and duplicate step suppression.

- [ ] **Step 7: Commit reliable execution state**

```powershell
git add backend/secagent/services/job_service.py backend/secagent/services/task_events.py backend/secagent/api/events.py backend/secagent/auth/stream_tickets.py backend/secagent/worker.py backend/secagent/repository.py backend/secagent/agents/runner.py backend/secagent/main.py backend/tests/unit/test_job_leases.py backend/tests/integration/test_task_events.py backend/tests/integration/test_job_recovery.py backend/tests/integration/test_mock_agent_loop.py
git commit -m "feat: recover jobs and stream durable task events"
```

---

### Task 7: Provider-Specific DeepSeek and GLM Adapters

**Files:**
- Create: `backend/secagent/providers/http_client.py`
- Create: `backend/secagent/providers/deepseek.py`
- Create: `backend/secagent/providers/glm.py`
- Create: `backend/secagent/providers/validation.py`
- Create: `backend/tests/provider_fakes.py`
- Modify: `backend/secagent/providers/base.py`
- Modify: `backend/secagent/providers/__init__.py`
- Modify: `backend/secagent/providers/router.py`
- Modify: `backend/secagent/domain.py`
- Delete: `backend/secagent/providers/openai_compatible.py`
- Test: `backend/tests/unit/test_deepseek_provider.py`
- Test: `backend/tests/unit/test_glm_provider.py`
- Test: `backend/tests/unit/test_model_router.py`

**Interfaces:**
- Produces `DeepSeekProvider.complete(request) -> ModelResponse`.
- Produces `GLMProvider.complete(request) -> ModelResponse`.
- Extends `ModelResponse` with `request_id`, `finish_reason`, `prompt_tokens`, `completion_tokens`, `retry_count`, and no reasoning text.
- Produces fixed `ModelRouter.provider_for(stage)` mapping with no cross-provider fallback in live mode.
- Produces test helpers `deepseek_provider(payload)`, `glm_provider(payload)`, `plan_request()`, and `parse_request()` backed by `httpx.MockTransport`.

- [ ] **Step 1: Write failing provider contract tests with `httpx.MockTransport`**

```python
@pytest.mark.asyncio
async def test_deepseek_records_usage_and_never_exposes_reasoning():
    provider = deepseek_provider(returning={
        "id": "ds-req-1",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"steps": []}', "reasoning_content": "private"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    })
    response = await provider.complete(plan_request())
    assert response.request_id == "ds-req-1"
    assert response.data == {"steps": []}
    assert "private" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_glm_empty_content_is_retryable_failure():
    provider = glm_provider(returning={"id": "glm-1", "choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
    with pytest.raises(ProviderUnavailable, match="empty_content"):
        await provider.complete(parse_request())
```

- [ ] **Step 2: Run provider tests and verify missing adapters**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_deepseek_provider.py backend/tests/unit/test_glm_provider.py -v`

Expected: FAIL because provider-specific modules do not exist.

- [ ] **Step 3: Implement pooled HTTP transport and error taxonomy**

```python
class ProviderErrorCode(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    NETWORK = "network"
    EMPTY_CONTENT = "empty_content"
    TRUNCATED = "truncated"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"

class ProviderFailure(RuntimeError):
    def __init__(self, provider: str, code: ProviderErrorCode, retryable: bool, request_id: str | None = None):
        super().__init__(f"{provider}: {code.value}")
        self.code = code
        self.retryable = retryable
        self.request_id = request_id
```

Reuse one `httpx.AsyncClient` per Worker process. Retry 429/5xx/timeouts/network failures at most three transport attempts, honor `Retry-After`, and apply jittered exponential fallback delays.

- [ ] **Step 4: Implement DeepSeek and GLM payloads**

Both payloads include `response_format={"type":"json_object"}`, `stream=False`, stage `max_tokens`, and prompts containing the literal word `JSON` plus an example. DeepSeek PLAN/CRITIC includes `thinking={"type":"enabled"}`. GLM TASK_PARSE/REPORT uses its configured model and does not receive planning responsibility.

- [ ] **Step 5: Implement strict validation and one repair call**

Parse non-empty content, reject `finish_reason="length"`, validate with the stage Pydantic type, and on JSON/Schema failure issue one repair request to the same Provider containing only the invalid JSON, validation errors, and target schema. Do not send a repair request for auth or authorization failures.

- [ ] **Step 6: Make routing fixed in live mode**

```python
FIXED_PROVIDER = {
    ModelStage.TASK_PARSE: "glm",
    ModelStage.PLAN: "deepseek",
    ModelStage.CRITIC: "deepseek",
    ModelStage.REPORT: "glm",
}
```

If the fixed Provider is unconfigured in live mode, raise `ProviderUnavailable` before task execution. Mock mode remains explicit for automated tests and demos.

- [ ] **Step 7: Run provider and router tests**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_deepseek_provider.py backend/tests/unit/test_glm_provider.py backend/tests/unit/test_model_router.py -v`

Expected: all tests pass across success, bad JSON, repair success, repair failure, 401, 429, 5xx, timeout, empty content, and truncated output.

- [ ] **Step 8: Commit live Provider adapters**

```powershell
git add backend/secagent/providers backend/secagent/domain.py backend/tests/provider_fakes.py backend/tests/unit/test_deepseek_provider.py backend/tests/unit/test_glm_provider.py backend/tests/unit/test_model_router.py
git commit -m "feat: harden DeepSeek and GLM providers"
```

---

### Task 8: Fixed-Stage Orchestration, Budgets, and Model Metrics

**Files:**
- Create: `backend/secagent/agents/budget.py`
- Modify: `backend/secagent/agents/runner.py`
- Modify: `backend/secagent/agents/parser.py`
- Modify: `backend/secagent/agents/planner.py`
- Modify: `backend/secagent/agents/critic.py`
- Modify: `backend/secagent/agents/reporter.py`
- Modify: `backend/secagent/services/ledger.py`
- Modify: `backend/secagent/repository.py`
- Test: `backend/tests/unit/test_task_budget.py`
- Test: `backend/tests/integration/test_fixed_model_stages.py`
- Test: `backend/tests/integration/test_model_failure_state.py`

**Interfaces:**
- Produces `TaskBudget.consume_call`, `consume_tokens`, `consume_step`, and `check_deadline`.
- Persists complete model metrics without raw prompts, raw reasoning, Authorization values, or response bodies.
- Guarantees a resumed approval continues from the pending step instead of recreating completed plan rows.

- [ ] **Step 1: Write failing budget and fixed-stage tests**

```python
def test_budget_rejects_ninth_model_call():
    budget = TaskBudget(max_calls=8, max_input_tokens=120_000, max_output_tokens=24_000, max_steps=20, deadline=utcnow() + timedelta(minutes=5))
    for _ in range(8):
        budget.consume_call()
    with pytest.raises(BudgetExceeded, match="model_calls"):
        budget.consume_call()


def test_live_pipeline_uses_fixed_providers(run_live_stub_task):
    result = run_live_stub_task()
    assert [(call.stage, call.provider) for call in result.model_calls] == [
        ("task_parse", "glm"),
        ("plan", "deepseek"),
        ("critic", "deepseek"),
        ("report", "glm"),
    ]
```

- [ ] **Step 2: Run tests and verify budget/fixed-stage failures**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_task_budget.py backend/tests/integration/test_fixed_model_stages.py -v`

Expected: FAIL because no budget object exists and current routing can fall back.

- [ ] **Step 3: Implement budget enforcement around every expensive action**

Call `check_deadline` before every model/tool call; consume model counts and returned usage after each model response; consume steps before inserting a new plan step. On `BudgetExceeded`, append `task.budget_exhausted`, mark the active step failed, and transition to `failed` with the precise exhausted dimension.

- [ ] **Step 4: Make runner checkpoint-aware**

Persist parsed task, plan, current step index, and model stage completion. On resume, load the existing successful checkpoints. Never add a duplicate `(task_id, step_index)` row. A denied approval ends as `cancelled`; an approved step continues at that step.

- [ ] **Step 5: Persist model metrics and sanitized failures**

`LedgerService.record_model_response` stores provider, model, stage, request ID, route reason, latency, retries, prompt tokens, completion tokens, finish reason, status, and `is_demo`. `record_model_error` stores only the stable error code and request ID.

Every evidence insert computes SHA-256 from canonical UTF-8 JSON content plus referenced file bytes when a file exists. Reports persist the exact evidence ID list they cite; `Reporter` rejects any citation not present in the current task ledger.

- [ ] **Step 6: Run orchestration and all three scene integration tests**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_task_budget.py backend/tests/integration/test_fixed_model_stages.py backend/tests/integration/test_model_failure_state.py backend/tests/integration/test_log_scene.py backend/tests/integration/test_source_scene.py backend/tests/integration/test_web_scene.py -v`

Expected: all tests pass with no duplicate steps after approval or retry.

- [ ] **Step 7: Commit bounded fixed-stage orchestration**

```powershell
git add backend/secagent/agents backend/secagent/services/ledger.py backend/secagent/repository.py backend/tests/unit/test_task_budget.py backend/tests/integration/test_fixed_model_stages.py backend/tests/integration/test_model_failure_state.py backend/tests/integration/test_log_scene.py backend/tests/integration/test_source_scene.py backend/tests/integration/test_web_scene.py
git commit -m "feat: enforce model roles and task budgets"
```

---

### Task 9: Frontend Authentication and Protected Routing

**Files:**
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/api/http.ts`
- Create: `frontend/src/views/LoginView.vue`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/types.ts`
- Test: `frontend/tests/auth-store.spec.ts`
- Test: `frontend/tests/router-auth.spec.ts`

**Interfaces:**
- Produces `useAuthStore()` with `user`, `accessToken`, `login`, `refresh`, `logout`, and `bootstrap`.
- Produces `apiRequest<T>()` that adds Bearer authentication and performs one refresh retry after 401.
- Adds route metadata `requiresAuth` and `requiresAdmin`.

- [ ] **Step 1: Write failing auth-store tests**

```typescript
it('keeps access token in store and retries once after refresh', async () => {
  mockFetch
    .mockResolvedValueOnce(new Response('', { status: 401 }))
    .mockResolvedValueOnce(jsonResponse({ access_token: 'new-token', user: analyst }))
    .mockResolvedValueOnce(jsonResponse([{ id: 'task-1' }]))
  const auth = useAuthStore()
  const tasks = await apiRequest('/api/tasks')
  expect(auth.accessToken).toBe('new-token')
  expect(tasks).toEqual([{ id: 'task-1' }])
  expect(localStorage.getItem('access_token')).toBeNull()
})
```

- [ ] **Step 2: Run tests and verify missing auth store**

Run: `cd frontend; F:\nodejs\npm.cmd test -- --run tests/auth-store.spec.ts tests/router-auth.spec.ts`

Expected: FAIL because `stores/auth.ts` and protected route metadata do not exist.

- [ ] **Step 3: Implement in-memory auth and authenticated fetch**

`apiRequest` sets `credentials: "same-origin"`, attaches `Authorization: Bearer <token>` when present, and uses a shared in-flight refresh Promise so simultaneous 401 responses trigger only one refresh request. Retry the original request once; a second 401 clears auth and redirects to `/login`.

- [ ] **Step 4: Implement login page and guards**

Login uses username/password fields, does not offer registration, does not store passwords, and displays stable API error messages. Router bootstrap calls `/api/auth/refresh`; unauthenticated protected navigation redirects to `/login?redirect=<encoded path>`. Analyst navigation cannot enter `/team` or `/audit`.

- [ ] **Step 5: Run frontend auth tests and type check**

Run: `cd frontend; F:\nodejs\npm.cmd test -- --run tests/auth-store.spec.ts tests/router-auth.spec.ts; F:\nodejs\npm.cmd run build`

Expected: all tests pass and `vue-tsc --noEmit` exits 0.

- [ ] **Step 6: Commit frontend authentication**

```powershell
git add frontend/src/stores/auth.ts frontend/src/api/http.ts frontend/src/views/LoginView.vue frontend/src/api/client.ts frontend/src/router.ts frontend/src/App.vue frontend/src/types.ts frontend/tests/auth-store.spec.ts frontend/tests/router-auth.spec.ts
git commit -m "feat: add authenticated team console"
```

---

### Task 10: Team Task Views and Resumable SSE

**Files:**
- Create: `frontend/src/composables/useTaskEvents.ts`
- Create: `frontend/src/components/WorkerStatus.vue`
- Create: `frontend/tests/fakes/event-source.ts`
- Modify: `frontend/src/stores/tasks.ts`
- Modify: `frontend/src/views/DashboardView.vue`
- Modify: `frontend/src/views/TaskListView.vue`
- Modify: `frontend/src/views/TaskDetailView.vue`
- Modify: `frontend/src/components/ModelRoutePanel.vue`
- Modify: `frontend/src/types.ts`
- Test: `frontend/tests/task-events.spec.ts`
- Test: `frontend/tests/team-task-detail.spec.ts`

**Interfaces:**
- Produces `useTaskEvents(taskId, onEvent)` with reconnect and last-event tracking.
- Produces `installFakeEventSource()` for deterministic Vitest open/message/error/reconnect behavior.
- Extends task detail types with queue position, job attempt, Worker heartbeat, model usage, and evidence hash.
- Replaces two-second polling while a task is active with SSE plus one authoritative refresh after each event.

- [ ] **Step 1: Write failing SSE reconnection test**

```typescript
it('reconnects with the last event id and refreshes task detail', async () => {
  const source = installFakeEventSource()
  const store = useTasksStore()
  await store.watchTask('task-1')
  source.emit({ lastEventId: '41', type: 'task.running', data: '{"attempt":2}' })
  source.fail()
  await vi.runOnlyPendingTimersAsync()
  expect(source.latestUrl()).toContain('after=41')
  expect(api.getTask).toHaveBeenCalledWith('task-1')
})
```

- [ ] **Step 2: Run tests and verify polling-only behavior**

Run: `cd frontend; F:\nodejs\npm.cmd test -- --run tests/task-events.spec.ts tests/team-task-detail.spec.ts`

Expected: FAIL because no SSE composable exists.

- [ ] **Step 3: Implement SSE lifecycle**

Because native `EventSource` cannot add Authorization headers, request a short-lived same-origin stream ticket from `POST /api/tasks/{id}/event-ticket`, then connect to `/api/tasks/{id}/events?ticket=<one-time-ticket>&after=<last-id>`. The ticket expires in 60 seconds and is scoped to one user and task. Reconnect with exponential delays capped at 10 seconds and stop on unmount or terminal task state.

- [ ] **Step 4: Upgrade task list and detail UI**

Show `我的任务` for analysts and `全部任务` for administrators. Add status/scene filters, queue position, attempt, Worker heartbeat, and current stage. Preserve the left decision timeline and right evidence ledger. Model rows show provider, actual model, input/output tokens, latency, retries, request ID, and sanitized error code.

Every run/resume/retry/cancel/approve request creates one `crypto.randomUUID()` idempotency key and reuses it while that UI action is pending; the API client sends it as `Idempotency-Key`.

- [ ] **Step 5: Run task UI tests and production build**

Run: `cd frontend; F:\nodejs\npm.cmd test -- --run tests/task-events.spec.ts tests/team-task-detail.spec.ts tests/task-detail.spec.ts; F:\nodejs\npm.cmd run build`

Expected: all tests pass and Vite production build exits 0.

- [ ] **Step 6: Commit real-time task UI**

```powershell
git add frontend/src/composables/useTaskEvents.ts frontend/src/components/WorkerStatus.vue frontend/src/stores/tasks.ts frontend/src/views/DashboardView.vue frontend/src/views/TaskListView.vue frontend/src/views/TaskDetailView.vue frontend/src/components/ModelRoutePanel.vue frontend/src/types.ts frontend/tests/fakes/event-source.ts frontend/tests/task-events.spec.ts frontend/tests/team-task-detail.spec.ts frontend/tests/task-detail.spec.ts
git commit -m "feat: stream team task progress"
```

---

### Task 11: Administrator Users, Audit, and Runtime Health UI

**Files:**
- Create: `frontend/src/views/TeamView.vue`
- Create: `frontend/src/views/AuditView.vue`
- Create: `frontend/src/components/UserDialog.vue`
- Modify: `frontend/src/views/SystemView.vue`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/types.ts`
- Test: `frontend/tests/admin-views.spec.ts`

**Interfaces:**
- Adds admin API methods for users, audit events, workers, and provider checks.
- Adds admin-only routes `/team`, `/audit`, and `/system`.
- Displays Provider configuration as booleans only.

- [ ] **Step 1: Write failing admin view tests**

```typescript
it('shows worker capacity and never renders secret values', async () => {
  api.workers.mockResolvedValue({ online: 1, active: 2, capacity: 3, queued: 4 })
  api.modelStatus.mockResolvedValue([{ name: 'deepseek', configured: true, model: 'deepseek-v4-pro' }])
  const wrapper = mount(SystemView)
  await flushPromises()
  expect(wrapper.text()).toContain('2 / 3')
  expect(wrapper.text()).toContain('deepseek-v4-pro')
  expect(wrapper.text()).not.toMatch(/sk-[A-Za-z0-9]/)
})
```

- [ ] **Step 2: Run test and verify missing admin pages**

Run: `cd frontend; F:\nodejs\npm.cmd test -- --run tests/admin-views.spec.ts`

Expected: FAIL because admin pages and APIs do not exist.

- [ ] **Step 3: Implement team and audit pages**

Team page supports create, enable, disable, and password reset. It prevents disabling the current last administrator and requires confirmation text for disruptive changes. Audit page paginates by event ID and filters by actor, action, outcome, and time without rendering raw JSON Secrets.

- [ ] **Step 4: Implement runtime health page**

Show PostgreSQL/Redis readiness, online Worker count, active `0/3`, queue length, Provider configured/model/status, and a deliberate `连通性检查` button. Provider check response renders request ID, token counts, latency, and sanitized error code only.

- [ ] **Step 5: Run admin tests and full frontend suite**

Run: `cd frontend; F:\nodejs\npm.cmd test -- --run; F:\nodejs\npm.cmd run build; F:\nodejs\npm.cmd audit --audit-level=high`

Expected: all Vitest files pass, build exits 0, and audit reports 0 high/critical vulnerabilities.

- [ ] **Step 6: Commit administrator UI**

```powershell
git add frontend/src/views/TeamView.vue frontend/src/views/AuditView.vue frontend/src/components/UserDialog.vue frontend/src/views/SystemView.vue frontend/src/api/client.ts frontend/src/router.ts frontend/src/App.vue frontend/src/types.ts frontend/tests/admin-views.spec.ts
git commit -m "feat: add team administration and audit UI"
```

---

### Task 12: Dry-Run-First SQLite to PostgreSQL Importer

**Files:**
- Create: `scripts/migrate_sqlite_to_postgres.py`
- Create: `backend/tests/integration/test_sqlite_import.py`
- Modify: `docs/deployment.md`

**Interfaces:**
- Produces CLI `python scripts/migrate_sqlite_to_postgres.py --source <sqlite-url> --target <postgres-url> [--apply]`.
- Dry-run is the default; `--apply` is required for writes.
- Produces JSON summary with per-table source count, target count, inserted count, skipped count, and evidence hash mismatches.
- Produces `migrate(source_url: str, target_url: str, owner_username: str, apply: bool) -> ImportSummary` for direct integration tests.

- [ ] **Step 1: Write a failing dry-run safety test**

```python
def repository_count(database_url: str, table_name: str) -> int:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        return int(connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one())


def test_import_is_dry_run_without_apply(seeded_sqlite_url, empty_postgres_url):
    result = migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=False)
    assert result.mode == "dry-run"
    assert result.tables["tasks"].inserted == 0
    assert repository_count(empty_postgres_url, "tasks") == 0


def test_apply_preserves_evidence_hashes(seeded_sqlite_url, empty_postgres_url):
    result = migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=True)
    assert result.evidence_hash_mismatches == 0
    assert repository_count(empty_postgres_url, "tasks") == repository_count(seeded_sqlite_url, "tasks")
```

- [ ] **Step 2: Run test and verify missing importer**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_sqlite_import.py -v`

Expected: FAIL because the migration script does not exist.

- [ ] **Step 3: Implement transactional import**

Read source rows in dependency order, map legacy tasks to a required `--owner-username`, compute missing evidence SHA-256 values, and write all target rows in one transaction. On any count, relationship, or hash mismatch, roll back and exit nonzero. Re-running `--apply` skips rows with matching primary IDs and hashes.

- [ ] **Step 4: Run importer tests twice**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_sqlite_import.py -v; \.venv\Scripts\python.exe -m pytest backend/tests/integration/test_sqlite_import.py -v`

Expected: both runs pass; the idempotency test confirms the second apply inserts zero duplicate rows.

- [ ] **Step 5: Commit importer and deployment instructions**

```powershell
git add scripts/migrate_sqlite_to_postgres.py backend/tests/integration/test_sqlite_import.py docs/deployment.md
git commit -m "feat: migrate SQLite evidence to PostgreSQL"
```

---

### Task 13: Production Compose, Secrets, Health, and Recovery Drill

**Files:**
- Create: `docker-compose.prod.yml`
- Create: `docker-compose.test.yml`
- Create: `secrets/deepseek_api_key.txt.example`
- Create: `secrets/glm_api_key.txt.example`
- Create: `secrets/jwt_signing_key.txt.example`
- Create: `scripts/check_no_secrets.ps1`
- Create: `scripts/provider_smoke.py`
- Modify: `docker-compose.yml`
- Modify: `.gitignore`
- Modify: `.dockerignore`
- Modify: `backend/Dockerfile`
- Modify: `frontend/nginx.conf`
- Modify: `docs/deployment.md`
- Test: `backend/tests/integration/test_readiness.py`

**Interfaces:**
- Development Compose starts PostgreSQL, Redis, API, Worker, frontend, and web-demo in explicit Mock mode.
- Production override mounts three read-only Secrets and forces live mode.
- Produces `/api/health/live` and `/api/health/ready`.
- Produces provider smoke CLI that reports only Provider/model/request ID/tokens/latency/status.

- [ ] **Step 1: Write failing readiness tests**

```python
def test_live_is_process_only(client):
    assert client.get("/api/health/live").json() == {"status": "ok", "service": "secagent-x"}


def test_live_mode_without_both_keys_is_not_ready(live_client_without_keys):
    response = live_client_without_keys.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["model_configuration"] == "failed"
```

- [ ] **Step 2: Run readiness tests and verify routes are absent**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_readiness.py -v`

Expected: FAIL with 404 for the new health endpoints.

- [ ] **Step 3: Implement Compose topology and Secret mounts**

Use `postgres:16-alpine` with a health check, `redis:7-alpine` with `appendonly yes`, one API container, one Worker container launched with `celery -A secagent.queue.celery_app worker --loglevel=INFO --concurrency=3 --prefetch-multiplier=1`, frontend, and web-demo. API waits for healthy PostgreSQL/Redis and successful Alembic migration; Worker waits for PostgreSQL/Redis readiness.

Production Secret files are mounted to `/run/secrets/*`. Add `secrets/*.txt` to `.gitignore` and allow only `*.example` files.

The production override sets `MODEL_MODE=live`, `COOKIE_SECURE=true`, `WORKER_CONCURRENCY=3`, and all three `*_FILE` paths. Reject `WORKER_CONCURRENCY > 3` during Settings validation.

- [ ] **Step 4: Implement health routes and Secret scan**

Readiness checks database `SELECT 1`, Redis `PING`, both live Provider configurations, and JWT signing key presence. It does not call paid model APIs. `scripts/check_no_secrets.ps1` scans tracked and untracked project files excluding `.git`, `.venv`, and `node_modules`; it fails on nonempty key assignments or `sk-`-style values outside test fixtures.

- [ ] **Step 5: Build and run the development stack**

Run: `docker compose down; docker compose up --build -d; docker compose ps`

Expected: postgres, redis, api, worker, frontend, and web-demo are running; postgres, redis, and api report healthy.

- [ ] **Step 6: Execute the automated concurrency and Worker recovery drill**

`test_job_recovery.py` submits four blocking test tasks, asserts exactly three active leases and one queued task, terminates the test Worker, advances the lease clock, starts a replacement Worker, and asserts one bounded requeue with unchanged evidence hashes.

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_readiness.py backend/tests/integration/test_job_recovery.py -v`

Expected: all tests pass.

- [ ] **Step 7: Commit deployability and operational checks**

```powershell
git add docker-compose.yml docker-compose.prod.yml docker-compose.test.yml secrets scripts/check_no_secrets.ps1 scripts/provider_smoke.py .gitignore .dockerignore backend/Dockerfile frontend/nginx.conf docs/deployment.md backend/tests/integration/test_readiness.py
git commit -m "feat: deploy production team stack"
```

---

### Task 14: Full Security, E2E, Coverage, and Live Model Acceptance

**Files:**
- Create: `e2e/team-auth.spec.ts`
- Create: `e2e/team-admin.spec.ts`
- Create: `e2e/team-recovery.spec.ts`
- Create: `e2e/helpers.ts`
- Modify: `e2e/three-scenes.spec.ts`
- Modify: `playwright.config.ts`
- Modify: `README.md`
- Modify: `docs/demo-script.md`
- Modify: `docs/deployment.md`
- Modify: `package.json`

**Interfaces:**
- Produces one command for offline acceptance and one explicit command for paid live-model acceptance.
- No live key is required for ordinary CI.
- Produces `loginContext`, `createTaskAs`, `createAndRunFourTasks`, and `taskStatuses` in `e2e/helpers.ts`; credentials come only from E2E environment variables.

- [ ] **Step 1: Add failing team E2E tests**

```typescript
test('analyst cannot open another analyst task', async ({ browser }) => {
  const alice = await loginContext(browser, 'alice', process.env.E2E_ALICE_PASSWORD!)
  const bobTask = await createTaskAs('bob', '日志响应')
  const response = await alice.request.get(`/api/tasks/${bobTask.id}`)
  expect(response.status()).toBe(403)
})


test('fourth task queues behind three active workers', async ({ page }) => {
  const tasks = await createAndRunFourTasks(page)
  await expect.poll(() => taskStatuses(tasks)).toEqual(expect.arrayContaining(['running', 'running', 'running', 'queued']))
})
```

- [ ] **Step 2: Run team E2E tests**

Run: `F:\nodejs\npx.cmd playwright test e2e/team-auth.spec.ts e2e/team-admin.spec.ts e2e/team-recovery.spec.ts`

Expected: all team E2E tests pass. If any test fails, stop this task and invoke `systematic-debugging` before modifying code.

- [ ] **Step 3: Run complete backend verification**

Run: `\.venv\Scripts\python.exe -m pytest --cov=secagent --cov-report=term-missing --cov-fail-under=85`

Expected: all tests pass and total backend coverage is at least 85%.

- [ ] **Step 4: Run complete frontend and dependency verification**

Run: `cd frontend; F:\nodejs\npm.cmd test -- --run; F:\nodejs\npm.cmd run build; F:\nodejs\npm.cmd audit --audit-level=high; cd ..; F:\nodejs\npm.cmd audit --audit-level=high`

Expected: all Vitest files pass, production build exits 0, and both audits report zero high/critical vulnerabilities.

- [ ] **Step 5: Run security regression and Secret scan**

Run: `\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_file_security.py backend/tests/unit/test_url_guard.py backend/tests/unit/test_tool_registry_and_risk.py backend/tests/integration/test_rbac.py backend/tests/integration/test_auth_api.py -v; powershell -ExecutionPolicy Bypass -File scripts/check_no_secrets.ps1`

Expected: all tests pass and `NO_SECRETS_FOUND` is printed.

- [ ] **Step 6: Run full offline Docker E2E acceptance**

Run: `docker compose up --build -d; F:\nodejs\npx.cmd playwright test`

Expected: login, RBAC, administrator, recovery, log, source, and Web E2E suites all pass.

- [ ] **Step 7: Request local Secret configuration from the user**

Create untracked files from examples:

```powershell
Copy-Item secrets/deepseek_api_key.txt.example secrets/deepseek_api_key.txt
Copy-Item secrets/glm_api_key.txt.example secrets/glm_api_key.txt
Copy-Item secrets/jwt_signing_key.txt.example secrets/jwt_signing_key.txt
```

Ask the user to paste each value into the corresponding local file, not into chat. Verify `git status --short` does not list the Secret files before continuing.

- [ ] **Step 8: Run paid live-model acceptance after the user confirms**

Run: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d; $env:DEEPSEEK_API_KEY_FILE=(Resolve-Path 'secrets/deepseek_api_key.txt'); $env:GLM_API_KEY_FILE=(Resolve-Path 'secrets/glm_api_key.txt'); \.venv\Scripts\python.exe scripts/provider_smoke.py --all-stages; Remove-Item Env:DEEPSEEK_API_KEY_FILE; Remove-Item Env:GLM_API_KEY_FILE; F:\nodejs\npx.cmd playwright test e2e/three-scenes.spec.ts`

Expected: GLM handles TASK_PARSE/REPORT, DeepSeek handles PLAN/CRITIC, all calls report request IDs/tokens/latency, and all three real-model scenes complete without Mock calls.

- [ ] **Step 9: Update handoff documentation and commit acceptance**

Document startup, initial admin creation, analyst creation, backups, restore, queue recovery, model check, key rotation, migration, and shutdown. Do not include actual usernames, passwords, tokens, or keys.

```powershell
git add e2e playwright.config.ts README.md docs/demo-script.md docs/deployment.md package.json
git commit -m "test: verify team production workflows"
```

- [ ] **Step 10: Run final clean-worktree verification**

Run: `git diff --check; git status --short; git log -1 --oneline`

Expected: no tracked modifications, no Secret files listed, and the latest commit is `test: verify team production workflows`.

---

## Completion Gate

Do not call the upgrade complete until all of the following are evidenced in one fresh verification pass:

1. Backend tests pass with at least 85% coverage.
2. Frontend tests, type check, build, and dependency audits pass.
3. RBAC, refresh replay, Secret redaction, SSRF, ZIP, RiskGate, queue recovery, and migration tests pass.
4. Docker development stack is healthy with PostgreSQL, Redis, API, Worker, frontend, and web-demo.
5. Offline Playwright suite passes.
6. After local Secret configuration, both Provider smoke tests and all three real-model scene tests pass.
7. Git contains no Secret and the working tree contains no tracked modifications.
