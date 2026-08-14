# Task 10: Team Task Views and Resumable SSE

## Delivered

- Replaced active-task two-second polling with a task-scoped, resumable SSE
  watcher. Each connection and reconnect obtains a fresh authenticated event
  ticket in memory, then opens the same-origin stream URL with the most recent
  event ID as `after`.
- The stream uses bounded exponential backoff (one to ten seconds), resets that
  delay on open, closes on task switch, unmount, terminal state, or auth clear,
  and fences stale tickets/connections through generation IDs and abort signals.
- Every valid event triggers one serialized/coalesced authoritative task-detail
  refresh. Bad JSON is ignored; stale and overlapping detail responses cannot
  replace newer task state.
- Added deterministic FakeEventSource coverage for tickets, reconnect cursor,
  bad JSON, stop races, and terminal stream shutdown.
- Added analyst/admin labels (`我的任务` / `全部任务`), status and scene filters,
  worker queue/attempt/heartbeat/stage details, and complete safe model-call
  metrics while preserving the decision timeline and evidence ledger.
- Run, resume, retry, cancel, and approve actions create an in-memory UUID key
  per pending action. The API client sends it in `Idempotency-Key`; no key or
  stream ticket is written to storage, a URL other than the transient stream
  query, or logs.

## TDD evidence

- SSE RED initially failed because `useTaskEvents` did not exist and `watchTask`
  was absent. The focused event/store suite then passed after the minimum stream
  and store implementation.
- Team detail RED initially failed because `WorkerStatus` did not exist. The
  worker/model metrics suite passed after the component and panel updates.

## Verification

- Focused task stream/detail tests: 7 passed.
- Full frontend tests: 22 passed.
- `F:\nodejs\npm.cmd run build`: passed (`vue-tsc --noEmit` and Vite).
- `git diff --check`: passed.
