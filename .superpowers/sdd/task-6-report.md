# Task 6 Report: Durable Leases, Events, and SSE

## Outcome

Implemented durable worker leases, bounded recovery, idempotent step replay,
transactional task events, and authenticated resumable SSE without replacing the
Task 5 command `JobRunRow` model or changing its JobRun-to-Task lock order.

## TDD evidence

- Initial RED: the focused lease/event command failed during collection because
  `secagent.services.job_service` and `secagent.api.events` did not exist.
- Subsequent RED cases covered stale/expired finishes, recovery budget exhaustion,
  pause-request settlement, cancelled-task non-resurrection, incomplete/unlinked
  evidence hashes, ticket replay/scope, cursor validation, worker ownership, and
  transactional lifecycle events.
- Each behavior was implemented only after its corresponding failure was observed.

## Implementation notes

- `JobService.claim`, `heartbeat`, `is_active`, `finish`, and `recover_expired`
  persist `worker_id`, `attempt`, and lease timestamps. Claims and terminal changes
  use the existing JobRun-to-Task ordering. A stale or expired worker cannot finish.
- Recovery reuses the durable command id and broker idempotency contract. It
  increments attempts and requeues within budget, settles outstanding pause/cancel
  requests, marks exhausted work failed, and records an audit plus event. It never
  requeues a Task that is no longer running.
- Final Task status and its event are committed inside `JobService.finish`, after
  validating the active lease. This closes the runner-finish race found during
  self-review.
- Step keys are canonical SHA-256 values over task, index, and full step content.
  Retries update stored step content and increment `attempt`. Replay occurs only
  for a successful result whose non-empty evidence hashes resolve through a
  completed ToolCall belonging to that exact step.
- `TaskEventService` recursively redacts before compact JSON serialization and
  rejects payloads larger than 16 KiB.
- SSE honors `Last-Event-ID`, polls once per second, emits 15-second comment
  heartbeats, and checks disconnects. Its generator accepts a bounded poll count
  for tests while production remains continuous.
- Stream tickets are HS256 JWTs with `sub`, `task_id`, `exp`, `jti`, and
  `purpose=task_events`. Only SHA-256(jti) reaches the replay store. Redis uses
  `SET NX EX 60`; SQLite tests use the fake replay store and never start Redis.
  Wrong user/task/purpose, expiry, and replay are rejected through the common API
  error envelope. Ownership is checked both when issuing and consuming a ticket.

## Verification

- Focused Task 6 plus lifecycle suite: `34 passed`.
- Full suite: `133 passed`, `0 failed`, one pre-existing TestClient deprecation
  warning.
- Core coverage: `91%` overall (`JobService 93%`, `TaskEventService 100%`,
  stream tickets `90%`, SSE API `85%`).
- `python -m compileall -q backend/secagent`: passed.
- `git diff --check`: passed (Git only reported the repository's CRLF conversion
  notices).
- Ruff was not installed in the project virtual environment, so no Ruff result is
  claimed.

## Scope checks

- Existing pause/cancel and broker publication semantics remain covered by the
  Task 5 regression tests.
- No GPT provider or production fallback was added; existing model routing remains
  unchanged.
- Existing limits of ten users and three workers were not altered.

## Review fixes

The post-implementation review identified three Important durability gaps. They
were reproduced as failing tests before the fixes were applied:

- A controlled worker interleaving expired and recovered the old lease while its
  tool was blocked. Before the fix, the old worker still committed the step,
  ToolCall, and Evidence after the replacement worker claimed the job.
- A successful step accepted a stored 64-character value without proving that it
  was the SHA-256 digest of the persisted evidence content, and evidence from an
  older step attempt could satisfy a newer attempt.
- Retrying a same-attempt partial ToolCall/Evidence commit had no atomic recovery
  API and could collide with the canonical Evidence uniqueness contract.

All critical Runner/Executor/Ledger writes now carry the immutable `JobLease`.
Each write transaction first locks and validates JobRun ownership, job attempt,
`running` status, and expiration, then locks the Task, preserving the established
JobRun-to-Task order. Tool outcome, canonical Evidence binding, and step terminal
result are committed atomically; a stale fence rolls the transaction back before
any stale result, report, or runtime error can be written.

Canonical Evidence retains the existing `(task_id, sha256, source)` uniqueness
constraint. Migration `20260815_02` adds `tool_call_evidences` so an observation
is bound to the ToolCall and exact step attempt that observed it. This permits a
same-attempt partial record to be adopted safely and permits a later attempt to
reference the same canonical Evidence without falsifying its original provenance.
Replay now recomputes SHA-256 from persisted content and only accepts bindings
from the current step attempt.

Review-fix verification:

- Exact RED/GREEN cases for forged hashes, old-attempt evidence, same-attempt
  partial recovery, and stale-worker database writes: `4 passed`.
- Task 6 focused, Task 5 lifecycle/enqueue regressions, and schema contract:
  `51 passed`.
- Full suite: `136 passed`, `0 failed`.
- Core coverage: `88.55%` across repository, Runner, Executor, Ledger, JobService,
  TaskEventService, SSE, and stream tickets (required minimum: 85%).
- A fresh SQLite database completed `upgrade head -> downgrade 20260814_01 ->
  upgrade head`; `alembic current` reported `20260815_02 (head)` and
  `alembic check` reported no upgrade operations (no ORM drift).
- `python -m compileall -q backend/secagent migrations` and `git diff --check`
  passed. Git emitted only the repository's line-ending conversion notices.

## Repeated runtime-error retry fix

A follow-up review found that two legitimate retries producing the same redacted
runtime error attempted two inserts against the canonical Evidence uniqueness
constraint. The second insert raised `IntegrityError`, poisoned the worker
session, and prevented `JobService.finish` from settling the second JobRun.

The regression test exercises two complete run/failed/retry cycles with two valid
leases. The initial RED reproduced the unique violation followed by
`PendingRollbackError`. `record_error` now performs redaction and hashing before a
specialized repository write. Inside the existing JobRun-to-Task fenced
transaction, SQLite and PostgreSQL use `ON CONFLICT DO NOTHING`; the canonical row
is then re-read and its source, content, and recomputed SHA-256 are verified before
commit. This is race-safe without weakening the unique constraint or reversing
lock order.

Verification for this fix:

- Exact RED/GREEN retry regression: `1 passed`; both JobRuns settled `failed`, the
  Task settled `failed_retryable`, both failure events/audits were present, and one
  redacted canonical runtime-error Evidence remained.
- Task 5/6 focused regressions: `46 passed`.
- Full suite: `137 passed`, `0 failed`; compileall and diff-check passed.
- No schema changed, so the previously verified migration head and drift result
  remain applicable.
