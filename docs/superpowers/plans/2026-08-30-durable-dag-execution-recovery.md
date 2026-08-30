# Durable DAG Execution and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the decomposition result as a subtask DAG, schedule GLM/DeepSeek subtask jobs with dependency and concurrency control, route tool requests through the existing safety chain, expose user failure decisions and mid-turn replanning, and produce the DeepSeek-only streaming final answer — all durable across restarts.

**Architecture:** A `TurnOrchestratorService` turns each conversation turn's persisted trigger message into a decompose job; the existing in-memory `CoordinatorAgent` stays pure and unchanged. New `DagRepository` owns conditional state transitions for subtasks/attempts/failures. `DagScheduler` computes dependency-ready subtasks and claims per-conversation running slots inside one transaction. `SubtaskRunner` executes one subtask through `ToolGateway` (ToolRegistry + RiskGate + Approval + Ledger) with the turn's budget snapshot. `SynthesisService` renders the final assistant answer with DeepSeek only. Jobs ride the existing `JobRunRow`/`JobService` lease machinery and Celery.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2 strict models, SQLAlchemy 2, Alembic, Celery/Redis (FakeJobQueue for tests), pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-26-conversational-multi-agent-workflow-design.md` (slices 4–7)

## Global Constraints

- `decompose` and `synthesize` stay fixed to provider `deepseek` with model exactly `deepseek-v4-flash`; live/auto never falls back to GLM or Mock, and route mismatch fails the turn into `waiting_model_decision` — never silent substitution.
- Only explicit `MODEL_MODE=mock` may emulate stages, with `emulated_provider=deepseek`, `emulated_model=deepseek-v4-flash`, `is_demo=true`, and visible 演示结果 marking.
- The only automatic model retry is the existing single transport-level retry in `providers/http_client.py`; no automatic cross-provider fallback and no additional model-layer auto retries.
- Running subtask concurrency per conversation defaults to `max_parallel_subtasks_per_conversation` (3, admin-lowered only); the model can never raise it.
- All state changes go through repository conditional updates guarded by expected prior states, each paired with a conversation event; the frontend never assigns states directly.
- Model-facing and model-returned mappings are redacted and strictly validated before persistence; raw prompts/responses, credentials, and hidden reasoning never enter messages, events, or audit payloads.
- Legacy task pipeline (`task_parse/plan/critic/report`), its API, and its tests must keep passing; no changes to `CoordinatorAgent`, `AssignmentPolicy`, or `conversation_decomposition.py` contracts.
- Every job is idempotent by unique `command_id`; a worker restart never duplicates a completed subtask or creates a second synthesize job.
- This plan does not add a third model, execute uploaded code, register new high tools, or rewrite the legacy frontend task pages.

## Cross-Task Interfaces

These names are fixed for every task:

```python
# backend/secagent/dag_domain.py (new)
class JobKind(StrEnum):
    TURN_DECOMPOSE = "turn_decompose"
    SUBTASK_EXECUTE = "subtask_execute"
    TURN_SYNTHESIZE = "turn_synthesize"

class SubtaskStatus(StrEnum):
    PENDING_DEPENDENCY = "pending_dependency"; QUEUED = "queued"; RUNNING = "running"
    WAITING_TOOL_APPROVAL = "waiting_tool_approval"; WAITING_MODEL_DECISION = "waiting_model_decision"
    COMPLETED = "completed"; INCOMPLETE = "incomplete"; SKIPPED = "skipped"
    SUPERSEDED = "superseded"; FAILED = "failed"; CANCELLED = "cancelled"

class ModelFailureStage(StrEnum):
    DECOMPOSE = "decompose"; SUBTASK = "subtask"; SYNTHESIZE = "synthesize"

class ModelFailureDecision(StrEnum):
    RETRY_SAME = "retry_same"; REASSIGN = "reassign"; SKIP_AND_REPLAN = "skip_and_replan"; TERMINATE_TURN = "terminate_turn"

class SubtaskResultDocument(_StrictModel):  # spec 7.3, redacted before persistence
    status: Literal["completed", "incomplete", "failed"]
    summary: str
    claims: list[ClaimDocument]          # each factual claim carries evidence_ref | attachment_ref | upstream_key
    evidence_refs: list[str]
    inference_notes: list[str]
    unresolved: list[str]

class ModelFailureCreate(_StrictModel):
    turn_id: str; subtask_id: str | None; stage: ModelFailureStage
    provider: str; model: str; error_code: str; detail: str  # no raw response text
```

```python
# backend/secagent/repositories/dag_repository.py (new)
class DagRepository:
    def create_subtasks_from_document(turn_id, document, assignments) -> list[SubtaskRead]
    def list_subtasks(turn_id) -> list[SubtaskRead]
    def transition_subtask(subtask_id, target: SubtaskStatus, *, expected: set[SubtaskStatus], reason: str) -> SubtaskRead  # raises SubtaskTransitionError
    def create_attempt(subtask_id, *, provider, model, idempotency_key) -> SubtaskAttemptRead
    def finish_attempt(attempt_id, *, status, error_code=None) -> SubtaskAttemptRead
    def save_subtask_result(attempt_id, result: SubtaskResultDocument) -> SubtaskResultRead
    def record_model_failure(payload: ModelFailureCreate) -> ModelFailureRead
    def resolve_model_failure(failure_id, decision: ModelFailureDecision, actor) -> ModelFailureRead
    def ready_subtasks(turn_id) -> list[SubtaskRead]          # pending_dependency whose deps are terminal-success
    def running_subtask_count(conversation_id) -> int
    def mark_turn_state(turn_id, target: ConversationTurnStatus, *, expected: set[ConversationTurnStatus]) -> ConversationTurnRead
```

```python
# backend/secagent/queue/base.py additions
class DagJobQueue(Protocol):
    def enqueue_dag_job(self, kind: JobKind, ref_id: str, command_id: str) -> str: ...
# CeleryJobQueue routes to run_turn_decompose / run_subtask_execute / run_turn_synthesize
# FakeJobQueue records (kind, ref_id, command_id) with command-id dedup

# backend/secagent/worker.py additions
@celery.task(name="secagent.run_turn_decompose")   def run_turn_decompose(turn_id, command_id)
@celery.task(name="secagent.run_subtask_execute")  def run_subtask_execute(subtask_id, command_id)
@celery.task(name="secagent.run_turn_synthesize")  def run_turn_synthesize(turn_id, command_id)
```

```python
# backend/secagent/services/dag_orchestrator.py (new)
class TurnOrchestratorService:
    async def run_decompose_job(turn_id, command_id, worker_id) -> None
    async def run_synthesize_job(turn_id, command_id, worker_id) -> None

# backend/secagent/services/dag_scheduler.py (new)
class DagScheduler:
    def schedule_turn(self, turn_id) -> int        # fill free slots, returns jobs created
    def on_subtask_finished(self, subtask_id) -> int

# backend/secagent/services/subtask_runner.py (new)
class SubtaskRunner:
    async def run(self, subtask_id, command_id, worker_id) -> None

# backend/secagent/services/tool_gateway.py (new)
class ToolGateway:
    def submit(self, *, subtask, attempt, lease, tool_name, params) -> ToolRequestOutcome
```

Command-id scheme (sha256 hex like `TaskService._command_id`): `secagent:turn:{turn_id}:decompose:v{plan_version}`, `secagent:subtask:{subtask_id}:execute:a{attempt}`, `secagent:turn:{turn_id}:synthesize` (constant — the uniqueness of this key is what guarantees one synthesize job per turn).

---

### Task 1: DAG persistence schema and conditional repository

**Files:**
- Create: `backend/secagent/dag_domain.py`
- Create: `backend/secagent/repositories/__init__.py`, `backend/secagent/repositories/dag_repository.py`
- Modify: `backend/secagent/db_models.py` (new rows + nullable `job_kind/turn_id/subtask_id/parent_job_run_id` on `JobRunRow`; nullable `turn_id/subtask_id/attempt_id` on `ModelCallRow`, `ToolCallRow`, `EvidenceRow`, `ApprovalRow`)
- Create: `migrations/versions/20260830_10_dag_schema.py`
- Test: `backend/tests/unit/test_dag_domain.py`, `backend/tests/integration/test_dag_repository.py`

**Interfaces:**
- Produces all `dag_domain` enums/DTOs and `DagRepository` above, plus event appenders for `subtask.assigned|queued|started|completed|failed|superseded` and `model.failure.waiting_decision` reusing `ConversationEventService.encode_redacted_event_payload` (16 KiB cap).
- `SubtaskRead` carries: key, turn_id, title, objective, required_capabilities, proposed/assigned provider, route_reason_code/reason, effective allowed_tools, expected_output, required flag, status, status_version, result summary.
- Consumes: `DecompositionDocument`, `AssignmentDecision`, `ConversationTurnRow`, `ConversationEventRow`.

- [ ] **Step 1: Failing domain tests** — strict DTO bounds per spec §7/§9 (status enums exact, `SubtaskResultDocument` rejects a factual claim without any reference, extra="forbid", no coercion).
- [ ] **Step 2: Verify RED** (`ModuleNotFoundError: secagent.dag_domain`).
- [ ] **Step 3: Implement ORM rows** `SubtaskRow` (unique `(turn_id, key)`), `SubtaskDependencyRow` (unique `(subtask_id, dependency_subtask_id)`), `SubtaskAttemptRow` (unique idempotency key; lease columns mirrored from `JobRunRow`), `SubtaskResultRow`, `ModelFailureRow` (status `waiting_decision|resolved`, user decision fields), plus the nullable linkage columns. Index `subtasks(turn_id, status)` for the scheduler.
- [ ] **Step 4: Alembic migration** `20260830_10_dag_schema.py` upgrading and downgrading all of the above; verify `alembic upgrade head` on a scratch PostgreSQL and on SQLite.
- [ ] **Step 5: Implement `DagRepository`** — every transition is `UPDATE ... WHERE status IN expected` returning the fresh row or raising `SubtaskTransitionError`; each successful transition appends the matching conversation event in the same transaction; attempts reuse `JobService`-style lease columns so an expired attempt can be recovered exactly once.
- [ ] **Step 6: GREEN + commit** — integration tests cover: duplicate key rejection, illegal transition rejection, dependency rows, attempt idempotency, event pairing. `git commit -m "feat: add subtask DAG persistence"`

### Task 2: Turn decompose job

**Files:**
- Create: `backend/secagent/services/dag_orchestrator.py`, `backend/secagent/services/tool_authorization.py`
- Modify: `backend/secagent/queue/base.py`, `backend/secagent/queue/celery_queue.py`, `backend/secagent/queue/fake.py`
- Modify: `backend/secagent/services/conversation_service.py` (create `turn_decompose` job row in the send-message transaction; post-commit enqueue; failure leaves job `enqueue_failed` for startup re-publish)
- Modify: `backend/secagent/main.py` (startup recovery re-publishes `pending_publish`/`enqueue_failed` DAG jobs)
- Modify: `backend/secagent/worker.py`
- Test: `backend/tests/integration/test_turn_decompose_job.py`

**Interfaces:**
- Consumes: `CoordinatorAgent.decompose` unchanged; turn row (status `created`, `budget_json`, trigger message + attachments); `DagRepository`; `DagJobQueue`.
- Produces: `TurnOrchestratorService.run_decompose_job` which loads the bounded `DecompositionContext` from persisted state (recent messages, attachment metadata with scan summaries, settings, budget snapshot), calls the coordinator exactly once with `plan_version = turn.plan_version`, persists the document and assignment decisions as subtask rows, transitions `created → decomposing → scheduling → running`, then hands off to `DagScheduler.schedule_turn`. On `ProviderUnavailable`/validation failure it records a `ModelFailure` (stage `decompose`) and transitions the turn to `waiting_model_decision` — no GLM/Mock fallback, no retry beyond the transport layer.
- The registered tool set comes from `app.state.tool_registry.describe()`. `ConversationSettings` has no per-conversation tool allowlist, so `tool_authorization.py` derives the conversation-authorized set deterministically from `safety_mode`: `conservative` → low-risk tools only, `standard` → low + medium, `expert` → low + medium (high and forbidden tools are never authorized; no high tools exist in this phase). `allowed_targets` keep constraining web tools at the existing `UrlGuard` layer. Both coordinator input and the `ToolGateway` re-check use this same derived set — never the model's word.

- [ ] **Step 1: Failing test** — mock-mode send message → job row exists; running the job via `FakeJobQueue` consumer produces subtask rows mirroring the `DecompositionDocument` (providers/risk/assignments included), turn status `running`, events `turn.decomposition.started/completed` + `turn.plan.versioned` + `subtask.queued` batch; `ModelCallRow` records the decompose call with `turn_id`.
- [ ] **Step 2: Verify RED** (enqueue is a no-op today; turn stays `created`).
- [ ] **Step 3: Implement queue + orchestrator + send-message hook** per interfaces; reuse the legacy `pending_publish → publishing → queued` publish protocol for crash safety.
- [ ] **Step 4: Failure-path test** — live-mode router without DeepSeek configured → turn `waiting_model_decision`, `model_failures` row exists, no subtask rows, no mock call recorded.
- [ ] **Step 5: GREEN + commit** — `git commit -m "feat: run durable turn decomposition"`

### Task 3: DAG scheduler with concurrency slots

**Files:**
- Create: `backend/secagent/services/dag_scheduler.py`
- Modify: `backend/secagent/services/dag_orchestrator.py` (decompose and subtask completion call the scheduler in the same transaction that records the terminal state)
- Test: `backend/tests/integration/test_dag_scheduler.py`

**Interfaces:**
- Consumes: `DagRepository.ready_subtasks`, `running_subtask_count`, `conversation settings` concurrency cap, `DagJobQueue`.
- Produces: `schedule_turn(turn_id)` and `on_subtask_finished(subtask_id)`; both run in one transaction with `SELECT ... FOR UPDATE` on the conversation row (SQLite: serialized by the single connection) so two schedulers cannot oversubscribe slots; creates `subtask_execute` job rows with `command_id = sha256(secagent:subtask:{id}:execute:a{attempt})`; when all required subtasks are terminal it creates exactly one `turn_synthesize` job (the constant command-id makes this idempotent, verified by a concurrent double-schedule test); when only non-required subtasks remain it lets synthesis proceed and marks stragglers `skipped` with an event.
- Emits `subtask.queued` / `turn.synthesis.started` events.

- [ ] **Step 1: Failing tests** — dependency ordering (B stays `pending_dependency` until A `completed`), concurrency cap 3 with a 5-task flat DAG, slot release on completion, duplicate schedule returns 0 new jobs, unique synthesize job, required-failure blocks downstream and stops dispatch.
- [ ] **Step 2: Verify RED**, **Step 3: implement**, **Step 4: concurrency probe** — two threads scheduling the same turn on PostgreSQL must create ≤ cap jobs (ephemeral PG container per the established barrier ruling).
- [ ] **Step 5: GREEN + commit** — `git commit -m "feat: schedule subtask DAG with concurrency control"`

### Task 4: Subtask worker and tool gateway

**Files:**
- Create: `backend/secagent/services/subtask_runner.py`, `backend/secagent/services/tool_gateway.py`, `backend/secagent/subtask_prompts.py`
- Modify: `backend/secagent/worker.py` (`run_subtask_execute`), `backend/secagent/services/ledger.py` (accept optional `turn_id/subtask_id/attempt_id` linkage on existing record methods — additive, legacy call sites untouched), `backend/secagent/providers/mock.py` (deterministic `SubtaskResultDocument` fixture keyed on `response_schema["title"]`)
- Test: `backend/tests/integration/test_subtask_runner.py`, `backend/tests/unit/test_tool_gateway.py`

**Interfaces:**
- `SubtaskRunner.run` executes one subtask: claim job lease (existing `JobService`), mark attempt `running`, then loop up to `budget.max_model_calls_per_subtask` model calls and `budget.max_tool_calls_per_subtask` gateway calls under `budget.timeout_seconds`, checking `job_service.is_active(lease)` and turn status before every call (cooperative pause/cancel/supersede checkpoint, mirroring the legacy runner). Model stage is `subtask_execute` with preferred provider = the assigned logical provider; response must validate as `SubtaskResultDocument`; factual claims without resolvable evidence refs force status `incomplete`.
- `ToolGateway.submit` rejects unregistered, not-in-`allowed_tools`, or unauthorized tools; runs `RiskGate`: low → auto-execute, medium/high-by-policy → create `ApprovalRow` (with `turn_id/subtask_id/attempt_id`), transition subtask to `waiting_tool_approval` and return `wait` so the job finishes (approval decision re-enqueues); forbidden/out-of-scope → reject with event. Executed calls go through `ToolRegistry.execute` and the `Executor` ledger path.
- Transport-level failure after the built-in retry → `ModelFailure` (stage `subtask`), subtask `waiting_model_decision`, dispatch stop via turn state; attempt kept for `retry_same`.
- Mock mode: deterministic `SubtaskResultDocument` with `is_demo=true` evidence, labelled 演示结果.

- [ ] **Step 1: Failing unit tests for the gateway** (risk matrix, allowlist filtering, approval pause, forbidden rejection, redaction of params before `record_tool_call`).
- [ ] **Step 2: Failing integration tests**: happy path (mock) completes subtask and persists `SubtaskResultRow` + evidence with `subtask_id` linkage; budget exhaustion returns `incomplete` with unresolved notes; tool approval pauses only that subtask; transport failure records `ModelFailure` and waits; lease loss mid-run leaves attempt recoverable and the requeued run skips already-recorded calls via attempt idempotency.
- [ ] **Step 3: Implement** runner + gateway, **Step 4: verify restart recovery** (expire lease, run `JobService.recover_expired`, re-run job → completed once).
- [ ] **Step 5: GREEN + commit** — `git commit -m "feat: execute subtasks with gated tools"`

### Task 5: Failure decisions, stop, and mid-turn replan

**Files:**
- Create: `backend/secagent/api/model_failures.py`, `backend/secagent/api/approvals.py`, `backend/secagent/services/turn_control_service.py`
- Modify: `backend/secagent/services/conversation_service.py` (follow-up message while an active turn runs: persist message, transition active turn `replan_requested`, create the next turn with `replan_from_turn_id` and plan_version from `conversations.next_turn_plan_version`, enqueue its decompose job)
- Modify: `backend/secagent/main.py`, `backend/secagent/api/conversations.py` (`POST /{conversation_id}/stop`)
- Test: `backend/tests/integration/test_model_failure_decisions.py`, `backend/tests/integration/test_stop_and_replan.py`

**Interfaces:**
- `POST /api/approvals/{approval_id}/decision` (new router, owner/RBAC-checked) resolves subtask tool approvals created by the gateway: approve resumes by re-enqueuing the subtask job with a fresh attempt command id, reject transitions the subtask to `cancelled` (or `failed` when it is required and downstream depends on it) and triggers `DagScheduler.on_subtask_finished`. Legacy task-plan approvals keep their existing task-scoped route untouched; approval rows with `turn_id IS NULL` are rejected by the new route with 404 to keep the two flows disjoint.
- `POST /api/model-failures/{failure_id}/decision` with `{"decision": "retry_same" | "reassign" | "skip_and_replan" | "terminate_turn", "target_provider": "glm" | "deepseek" | null}`: `retry_same` creates a new attempt with the same provider; `reassign` (subtask stage only) validates the target through `AssignmentPolicy` before creating an attempt — decompose/synthesize failures reject reassignment with 422; `skip_and_replan` marks the subtask `skipped` and enqueues a re-decompose of the remaining graph into a new plan version; `terminate_turn` transitions the turn to `partial` (or `failed` if decomposition never succeeded) and jumps to synthesis over completed results only. All decisions append `model.failure.resolved` and audit rows; the system never auto-decides.
- `POST /api/conversations/{id}/stop`: requests a checkpoint stop; running workers see it between calls and finish; queued subtasks `cancelled`; turn `cancelled` (or `partial` when some subtasks completed and synthesis can run).
- Follow-up messages: completed subtasks and evidence stay valid; `pending_dependency`/`queued` subtasks become `superseded` with events; the new decompose context carries them as `completed_subtasks`/`evidence`/`unresolved_questions` so unchanged work is not repeated.

- [ ] **Step 1: Failing tests** for each of the four decisions (including decompose-stage `reassign` → 422 and unknown failure → 404), stop-at-checkpoint, and follow-up supersede/replan (old plan v1 subtasks superseded, new turn v2 created from `replan_from_turn_id`).
- [ ] **Step 2: Verify RED**, **Step 3: implement**, **Step 4: verify** replan respects `MAX_REPLANS_PER_TURN` (excess follow-ups wait as messages until the turn finishes).
- [ ] **Step 5: GREEN + commit** — `git commit -m "feat: add failure decisions and turn replanning"`

### Task 6: DeepSeek synthesizer and streaming answer

**Files:**
- Create: `backend/secagent/services/synthesis_service.py`
- Modify: `backend/secagent/services/dag_orchestrator.py` (`run_synthesize_job`), `backend/secagent/services/conversation_events.py` (typed payloads for `assistant.answer.delta/completed`, `turn.completed`), `backend/secagent/worker.py`, `backend/secagent/providers/mock.py` (deterministic synthesis fixture)
- Test: `backend/tests/integration/test_turn_synthesis.py`

**Interfaces:**
- `run_synthesize_job` calls `ModelStage.SYNTHESIZE` (fixed DeepSeek V4 Flash) with a redacted, bounded payload of subtask results, evidence refs, conflicts, and synthesis requirements; no tools are callable. The validated document distinguishes `facts` (each with evidence refs), `inference_notes`, and `unresolved`; conflicting subtask results are surfaced, not averaged.
- The assistant answer is persisted as a `conversation_messages` row with kind `assistant_answer`, status `streaming → completed`; each chunk appends an `assistant.answer.delta` event (monotonic sequence, redacted payload, ≤16 KiB) so SSE clients render incrementally and can recover from `Last-Event-ID`; completion emits `assistant.answer.completed` and `turn.completed` (status `completed` or `partial`).
- Missing critical evidence → synthesis returns a partial answer marked with unresolved items, or requests a new plan version via `replan_requested` — it never invents facts and never calls a tool.
- Mock mode streams deterministic segments with 演示结果 marking and demo evidence citations.

- [ ] **Step 1: Failing tests** — full mock turn: decompose → 3 parallel subtasks (cap observed) → dependency unlock → single synthesize job → streamed deltas in order → turn `completed` with evidence-referenced answer; `waiting_model_decision` → `terminate_turn` produces a `partial` answer from completed subtasks; restart between subtask and synthesis recovers with exactly one synthesize job and one assistant message (idempotency probe).
- [ ] **Step 2: Verify RED**, **Step 3: implement**, **Step 4: full backend suite + focused DAG suites + `git diff --check`**.
- [ ] **Step 5: GREEN + commit** — `git commit -m "feat: synthesize final answers from subtask evidence"`

## Verification Matrix

| Spec requirement | Task |
|---|---|
| §8.1 new tables, §8.2 linkage columns, Alembic | 1 |
| §5.2 coordinator consumed unchanged; §9.1 turn states | 2 |
| §5.4 / §10 slots, idempotency, single synthesize | 3 |
| §5.5 worker loop; §11 tool gateway; §14.1 waiting_model_decision | 4 |
| §4.4 replan; §12.2 stop + failure decisions; §9.2 supersede | 5 |
| §5.6 synthesis; §13 streaming events; §14.2 recovery | 6 |
| §19.1/§19.2 test matrix, §6.2 no-fallback invariants | all |

Legacy regression gate: every task ends with the focused suites of prior tasks plus, at Tasks 2/4/6 completion, the full backend suite (currently 607 passed + 1 environment skip) and `git diff --check`.
