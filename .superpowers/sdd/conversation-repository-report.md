# Conversation repository implementation report

## RED evidence

Command (with `PYTHONDONTWRITEBYTECODE=1`):

```text
.\.venv\Scripts\python.exe -m pytest backend\tests\unit\test_conversation_repository.py -q
```

Observed before production code existed:

```text
ModuleNotFoundError: No module named 'secagent.conversation_repository'
1 warning, 1 error in 0.32s
```

The failure was the expected missing persistence boundary, not a test typo. The
existing `StarletteDeprecationWarning` remained visible.

## GREEN evidence

Focused repository suite:

```text
14 passed, 3 warnings in 6.93s
```

The two additional warnings are Python 3.12's documented sqlite3 default
datetime-adapter deprecation from tests that force identical timestamps to
prove deterministic ID tie-breaking.

## Concurrency evidence

The three concurrency tests were rerun alone with independent sessions,
file-backed SQLite, `Barrier`, and `ThreadPoolExecutor`:

```text
3 passed, 1 warning in 1.10s
```

They assert, rather than merely run without exceptions:

- 12 concurrent independent messages receive exactly sequences 1 through 12,
  create 12 rows, and leave `next_message_sequence == 13`;
- two concurrent same-key/same-identity appends return one row ID, one sequence,
  exactly one `created=True`, and leave the next counter at 2; a different
  immutable identity raises `MessageIdempotencyConflict` without advancing it;
- 10 concurrent turns receive exactly plan versions 1 through 10 and leave the
  active pointer on version 10; two concurrent creators for one trigger produce
  exactly one turn and one invariant conflict without consuming a second plan
  version.

The allocator-statement test also captures SQL and proves the first DML is an
owner-conditional no-op `UPDATE conversations ... RETURNING`, before any
`SELECT`/allocation, and both allocators use database-side counter increments.

## Transaction evidence

The composition test creates a message, two attachments, a turn, and an event
with `commit=False`. After an injected late cross-conversation event failure,
all earlier rows and allocated counters remain present in the caller transaction,
proving that only the failing method savepoint rolled back. Explicit repository
`rollback()` then removes all composed rows, active pointer changes, and counter
increments.

## Full backend suite

Command (with `PYTHONDONTWRITEBYTECODE=1`):

```text
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

Result:

```text
335 passed, 3 warnings in 170.41s (0:02:50)
```

Warnings were not filtered: one existing Starlette/httpx deprecation and the
two sqlite3 datetime-adapter warnings described above.

## Self-review

- Added only the dedicated repository and its unit contract tests; legacy
  `TaskRepository`, API, frontend, DAG, worker, jobs, and upload I/O are untouched.
- Every public conversation/child operation receives an authenticated actor;
  creation derives owner only from that actor, analysts are owner-scoped, and
  admins use the same authenticated boundary.
- Child lookup follows successful conversation authorization, and cross-
  conversation message/turn/attachment/event/active references fail before
  counter allocation or insertion.
- Trigger ownership/link state and legacy Task ownership are rechecked after
  acquiring the conversation write lock. New turns allocate, insert, link the
  trigger, and update the active pointer in one method savepoint.
- Explicit active set accepts only the highest plan version; clear uses an
  expected-turn compare-and-set and cannot clear a newer pointer.
- DTO conversion uses `canonical_json_loads` for settings, budget, and payload;
  malformed or structurally invalid JSON propagates failure closed.
- Pagination validators reject booleans, negative cursors, nonpositive limits,
  and values over the exact contract maxima; every list has the required stable
  ordering and strict after-boundary.
- Event IDs remain the schema's global monotonic cursor while each query also
  filters the authorized conversation.
- `git diff --check` completed with exit code 0 before delivery.

## Concerns

- The repository intentionally leaves successful `commit=False` nested
  transactions open until the caller invokes `commit()` or `rollback()`. This
  preserves correct SQLite rollback semantics and method-level failure
  isolation, but callers must not abandon a live session without resolving the
  transaction.
- The third-party deprecation warnings are pre-existing/runtime warnings and
  are intentionally documented rather than suppressed in this checkpoint.

## Review-fix loop on base `81a96b32`

### RED evidence

All commands used the project `.venv` with `PYTHONDONTWRITEBYTECODE=1` and
`-p no:cacheprovider`.

- Pending caller state: the first targeted run failed during collection because
  the required dedicated `ConversationRepositoryStateError` did not exist.
  After adding only the error/check boundary, the six message/turn ×
  new/dirty/deleted cases passed and proved that zero SQL was emitted and the
  caller-owned state remained pending.
- Commit lifecycle: injected final `session.commit()` failure produced
  `rollback_calls == 0` instead of 1. After widening the exception boundary,
  the regression and the original caller-level composition rollback test both
  passed (`2 passed, 1 warning`).
- Stale trigger: Session B's cached `turn_id=None` allowed a second Turn and the
  RED failed with `DID NOT RAISE ConversationInvariantError`. After a fresh
  post-lock read plus conditional `turn_id IS NULL` link, the stale-cache and
  original concurrent-turn tests both passed (`2 passed, 1 warning`).
- Event serialization: the structural RED observed a conversation `SELECT`
  before event insert, and the eight-session same-conversation test raised
  `sqlite3.OperationalError: database is locked`. Acquiring the existing
  authenticated conversation write lock before turn validation/insertion made
  both tests pass (`2 passed, 1 warning`).

### GREEN and regression evidence

Focused repository suite:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend\tests\unit\test_conversation_repository.py -q
24 passed, 3 warnings in 5.82s
```

The controller independently reran the same focused suite and observed
`24 passed, 3 warnings in 9.00s`.

Full backend suite:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend\tests -q
345 passed, 3 warnings in 139.77s (0:02:19)
```

The warnings remain visible and unchanged in kind: one Starlette/httpx
deprecation plus two Python 3.12 sqlite3 datetime-adapter deprecations.

### Fix rationale and self-review

- `_write()` rejects meaningful pending `Session.new`, `Session.dirty`, or
  `Session.deleted` state before `begin_nested()` can autoflush. The rejection
  performs neither SQL nor rollback. Earlier successful repository
  `commit=False` operations remain composable because each method flushes its
  own state.
- The write lifecycle now encloses savepoint creation, operation/flush, and the
  final commit. Any `commit=True` exception performs full Session rollback;
  `commit=False` still rolls back only its method savepoint, and the existing
  caller-level rollback composition contract remains green.
- Turn creation refreshes the trigger after acquiring the conversation lock and
  links it with a conditional database update. A lost link race raises inside
  the method savepoint, restoring the allocated plan counter and inserted Turn.
- Event append reuses the owner-conditional per-conversation lock. It does not
  introduce a global lock, so different conversations may safely interleave
  global cursor IDs while conversation-scoped readers cannot skip an earlier
  same-conversation commit.
- The tracked fix remains limited to the dedicated repository and its focused
  tests; schema, API, DAG, worker, upload, frontend, and legacy TaskRepository
  remain untouched.
