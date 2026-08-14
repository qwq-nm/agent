# Task 8 Report: Fixed-Stage Orchestration, Budgets, and Model Metrics

## Outcome

Implemented durable fixed-stage orchestration for TASK_PARSE=GLM,
PLAN/CRITIC=DeepSeek, and REPORT=GLM with no live fallback. Task budgets now
cover model calls, input/output tokens, plan steps, and a persisted deadline.
Approval resume continues the pending step from validated checkpoints; retry
starts a new logical attempt and cannot reuse failed-attempt checkpoints.

## TDD evidence

- Budget RED failed because `secagent.agents.budget` did not exist. The minimal
  GREEN added atomic call/token/step consumption, timezone-aware injected clocks,
  deadline checks, and persisted-usage rehydration.
- Orchestration RED isolated three gaps: provider failures produced no model-call
  error row, max-model-call limits were ignored, and approval resume repeated
  parse/plan and incremented step attempts. All three are covered by integration
  tests and now pass.
- Citation/file-evidence RED proved that foreign evidence IDs were accepted and
  file-aware canonical hashing was absent. Reporter/repository validation and
  canonical JSON plus bounded upload-file hashing now reject those cases.
- Migration RED upgraded only through Task 6 and showed the new task columns were
  absent. Revision `20260815_03` now round-trips 02 -> 03 -> 02 -> 03 and passes
  Alembic drift checking.
- Retry RED showed a new retry command persisted `attempt=1`. Retry now increments
  the logical task attempt, while approval/resume retain the current attempt.
- Review RED covered all three budget preflight dimensions, cross-attempt model
  checkpoint rejection, and an unavailable fixed provider. It proved that an
  exactly consumed input/output budget still reached the provider, model calls
  lacked logical-attempt identity, and an unavailable provider left no audit
  row. The focused review suite now passes all six boundary cases.
- A full-suite run exposed one compatibility regression in a concurrent enqueue
  monkeypatch after the `add_job_run` signature extension. The initial-attempt
  call shape remains backward compatible; the reproducer and retry test pass.

## Implementation notes

- `TaskBudget` exposes `consume_call`, `consume_tokens`, `consume_step`, and
  `check_deadline`. Call, input-token, output-token capacity and the deadline are
  checked before a model call; the deadline is checked before each tool call.
  Successful model responses are persisted, then their exact call and token
  usage is consumed. A response that crosses a limit still enters the same
  atomic budget-terminal path.
- Budget state is reconstructed from durable model-call and task-step rows. The
  deadline and per-task limits live on `tasks`, so approval, resume, worker
  recovery, and explicit retry cannot reset the ledger.
- Budget exhaustion is fenced by the current JobRun lease and atomically records
  one allowlisted `task.budget_exhausted` dimension, fails the active pending
  step when present, fails the task, and settles the job as failed.
- Parsed task, plan, critic decision, report artifact, and current step index are
  durable. A checkpoint is reusable only when its logical attempt, task/upload
  fingerprint, successful exact model-call ID, stage, task association, and
  current lease fence validate. Derived `scene` is excluded from the input
  fingerprint so approval resume does not invalidate its own parse checkpoint.
- Existing successful Task 6 step results remain reusable only through their
  canonical evidence and step-attempt associations. Failed and partial results
  are never accepted as successful retry checkpoints.
- Model rows persist provider, model, stage, safe route reason, scrubbed request
  ID, latency, retries, normalized token counts, finish reason, status, error
  code, logical attempt, and demo flag. Raw prompts, reasoning, Authorization
  values, response bodies, and arbitrary route reasons are not stored. Provider
  errors persist only the stable error taxonomy and scrubbed request ID as error
  details. A missing fixed provider records exactly one safe error call using the
  fixed-stage provider name and a non-sensitive configured-model lookup; the
  error path never invokes routing a second time.
- Evidence hashing uses redacted canonical UTF-8 JSON content followed by bytes
  from an optional referenced file. File references must resolve beneath that
  task's upload root, must be regular files, and must stay within the size bound;
  files are read only and never executed. Legacy Task 6 content hashes remain
  verifiable for existing records.
- Reporter accepts only real evidence IDs from the current task, rejects
  duplicates/foreign/missing IDs, renders the IDs in the evidence chain, and
  repository validation persists the exact ordered list in
  `reports.evidence_ids_json`.
- Task creation snapshots configured call/token/step limits. The configured task
  timeout reaches the worker runner. Existing team limit and worker concurrency
  settings remain 10 users and 3 worker processes.

## Schema and migration

- Added `tasks.max_steps`, nullable `tasks.budget_deadline_at`, and non-null
  `tasks.orchestration_json`.
- Added Alembic revision `20260815_03_task_orchestration_budget.py`, based on
  `20260815_02`, with upgrade and downgrade paths and ORM-aligned defaults.
- Added non-null `model_calls.attempt` with default/backfill `1` in Alembic
  revision `20260815_04_model_call_attempt.py`, based on `20260815_03`.
- Migration contract tests verify upgrade from Task 6, the 03 -> 04 -> 03 -> 04
  round trip, re-upgrade to head, and `alembic check` with no drift.

## Verification

- Review boundary suite: `6 passed`, `0 failed`.
- Task 8/provider/Task 6 focused regression suite: `114 passed`, `0 failed`.
- Full backend suite with coverage: `207 passed`, `0 failed`.
- Global backend coverage: `91.55%` (required minimum: `85%`).
- Fresh SQLite `alembic upgrade head` and `alembic check`: passed with no new
  upgrade operations detected.
- `python -m compileall -q backend/secagent backend/tests migrations`: passed.
- `git diff --check`: passed; Git emitted only repository line-ending conversion
  notices.
- No real model provider or untrusted file was executed by tests. The suite still
  emits the pre-existing Starlette/httpx deprecation warning and SQLite
  ResourceWarnings during the coverage run; neither changes the zero exit status.

## Review findings fixed

- Exact model-call IDs now anchor stage checkpoints instead of selecting a latest
  row by timestamp.
- The task fingerprint excludes the parse-derived scene but includes immutable
  task input and bounded upload bytes.
- Exhausted call budgets stop before issuing another expensive model request.
- Exhausted input or output token budgets also stop before provider invocation,
  including the exact `used == limit` boundary, without adding a model-call row.
- Every model call is bound to its fenced logical JobRun attempt; checkpoint load
  and save reject successful calls from a different attempt.
- Missing fixed providers produce one sanitized, attempt-bound error metric and
  consume one call before normal retryable/failed job settlement.
- Initial JobRun creation preserves the established two-argument repository call
  shape used by concurrent publication tests; only non-initial attempts pass an
  explicit attempt.
- Runtime-error evidence uses the new canonical JSON digest while validation
  continues accepting Task 6 legacy digests.
