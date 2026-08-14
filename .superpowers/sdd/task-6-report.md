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
