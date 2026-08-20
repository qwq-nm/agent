# Task 5 Report: Job Queue Boundary and Celery/Redis Worker

## Scope and baseline

- Baseline: `ed4c71e`.
- Implemented only Task 5: the `JobQueue` boundary, fake and Celery adapters, the Celery worker actor, durable command idempotency, queued lifecycle commands, and broker-failure recovery.
- Reused the existing `job_runs` schema. Task 6 leases/heartbeats/SSE and Task 10 Compose deployment remain out of scope.

## Design delivered

- `JobQueue.enqueue(task_id, command_id)` is the only API-to-broker boundary. `FakeJobQueue` records immutable `QueuedJob` values and deduplicates by `command_id`; `CeleryJobQueue` submits only `[task_id, command_id]` and uses the command ID as the Celery task ID.
- Celery accepts and emits JSON only, ignores results, uses late acknowledgement, prefetch 1, startup broker retry, and `settings.worker_concurrency` (default 3). Production application construction defaults to `CeleryJobQueue`; fake execution is injected only by tests.
- The `secagent.run_task` actor receives exactly two strings, creates a fresh SQLAlchemy session and `TaskService`, rebuilds runtime providers/tools from settings, and never receives an ORM row, secret, upload content, or prompt through the broker.
- `run`, `resume`, `retry`, and approval acceptance require an `Idempotency-Key`. The raw key is not persisted or audited; a deterministic SHA-256 command ID scopes it by task and action. Replays return 202 without a second successful publish.
- New commands atomically persist `job_runs.pending_publish`, task `queued`, and a redacted audit event. A conditional JobRun update owns publication and holds the database transaction lock until broker success/failure records the final result. Concurrent callers wait for and return the same outcome.
- Broker failure conditionally moves only the owned publishing job to `enqueue_failed`, moves a still-queued task to `failed_retryable`, records only the exception type, and returns 503. The same key can claim and retry that durable command.
- Worker delivery conditionally claims JobRun then Task in a consistent lock order. Duplicate/stale messages do not execute. Pause/cancel atomically invalidate pending commands before changing Task state, so an old delivery cannot consume a later resume.
- Approval rejection atomically records the decision and sets `cancelled`. Acceptance atomically records the approval, creates a new queued command, returns 202, and is replay-safe.

## RED evidence

- Initial queue slice: 5 failures showed missing queue modules/configuration, synchronous `/run` completion, no required idempotency key, and no broker-failure response/retry path.
- Cancellation RED showed a queued broker message resurrecting a cancelled task to `completed`.
- Publish recovery RED showed a DB-committed `publishing` command never being republished.
- Pause/resume RED showed the old command executing against the replacement queued state.
- Real two-client concurrency RED deliberately released the publisher transaction lock: both success and all-failure cases admitted a second publisher.
- Unique-insert concurrency RED returned `[503, 202]` when the winning publisher failed, proving the uniqueness loser returned before the durable outcome.

## GREEN implementation and tests

- Required focused queue/lifecycle suite: 16 passed.
- Real concurrency tests use two independent `TestClient`/SQLAlchemy sessions and a blocking broker double. Success yields two 202 responses with one publish; total broker failure yields two 503 responses with one serialized retry and no false acceptance.
- Existing mock, log, source, and approved passive-Web scenes explicitly drain the fake queue through the worker helper; no real Celery worker starts in tests.
- Existing authentication, task ownership/RBAC, approval expiry, audit redaction, upload safety, GET-only Web behavior, model routing, and reporting tests remain covered by the full suite.

## Self-review and independent review

- Self-review added durable JobRun/broker-ID/audit assertions, stale-delivery cancellation, interrupted-publish recovery, and broker-failure retry checks.
- Independent review found stranded `publishing` rows, stale pause/resume commands, concurrent publisher state corruption, uniqueness-loser false 202 responses, and PostgreSQL JobRun/Task lock inversion.
- Fixes use `pending_publish`, a conditional transaction-held publish claim, conditional failure demotion, uniqueness-loser re-entry into coordinated publishing, pending-job invalidation, and one JobRun-to-Task lock order.
- Final independent review: Approved; no remaining Critical or Important Task 5 findings.

## Verification

- Backend full suite: 105 passed.
- `python -m compileall -q backend/secagent`: passed.
- `git diff --check`: passed.
- Test output contains only the pre-existing Starlette/httpx deprecation warning.

## Residual risks and decisions

- Publication intentionally holds one JobRun database lock across the broker call to make duplicate API outcomes consistent. With the approved maximum of three workers and at most ten users this is bounded, but broker connection timeouts must remain finite operationally.
- Broker acknowledgement can be ambiguous if Redis accepts a message and the client then raises. The durable command ID and worker-side claim prevent duplicate execution; a retry may still place duplicate envelopes in the broker, which is normal at-least-once delivery behavior.
- SQLite exercises conditional-update serialization but not PostgreSQL row-level deadlock behavior. The implementation uses one JobRun-to-Task lock order; PostgreSQL-backed concurrency CI remains desirable.
- Worker leases, heartbeat recovery, SSE, and deployment services are deliberately deferred to their later tasks.

## Main-review running-command and approval fixes

- Main review reproduced a claimed running job that remained `running` after pause/cancel. A premature resume could create a second command while the old worker continued and later overwrote terminal state.
- Running jobs now persist `pause_requested` or `cancel_requested`. Resume/new execution checks and locks unsettled JobRun rows first and returns 409 until the old worker atomically finalizes the request to `paused` or `cancelled`; queued-but-unclaimed jobs are cancelled directly.
- Agent execution checks a freshly reloaded task state before and after model/tool boundaries. Executor checks again after a tool returns and before any tool/evidence ledger write. Completion and failure use conditional RUNNING transitions, so stale workers cannot revive paused/cancelled tasks.
- RED: controlled tools blocked after worker claim; pause followed by resume admitted a second queued job, cancel was overwritten, and the old runner continued into critic/error handling. A held SQLAlchemy identity-mapped TaskRow also proved `expire_on_commit=False` could hide a cross-session pause.
- GREEN: resume is 409 while `pause_requested`, succeeds only after the old job becomes `paused`, cancel remains terminal, and both paths record no post-boundary tool calls/reports (only pre-tool parse/plan model calls). `get_task(..., populate_existing=True)` forces fresh boundary reads.
- Concurrent same-key approval acceptance originally returned `[202, 409]`. On decision conflict it now rolls back, detects the winner's identical durable command, and enters the same coordinated publisher path. The Barrier regression returns `[202, 202]` with one broker publish.
- A distinct new idempotency key against an already queued `/run` now returns the standard 409 conflict envelope instead of propagating `ValueError` as a server exception.
- Final fix-focused regression set: 42 passed. Final backend full suite: 110 passed. Final independent review: Approved, with no remaining Critical or Important findings.
