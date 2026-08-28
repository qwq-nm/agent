# Conversation repository review findings

Base: `81a96b32`

All four findings below are accepted and blocking. Keep the fix limited to the
repository and its focused tests. Do not add API, DAG, worker, upload, or schema
work.

## 1. Pending Session state can flush before the lock

`Session.begin_nested()` performs an unconditional pre-savepoint flush. With
caller-owned pending ORM state, `_write()` can therefore emit an unrelated
INSERT/UPDATE before an allocator's owner-conditional conversation lock UPDATE.

Required behavior:

- Add a RED test for both message and turn allocator entry with pending
  `new`/meaningfully `dirty`/`deleted` caller state.
- Reject such a Session state with a dedicated repository-state error before
  any SQL is emitted. Do not silently flush or roll back caller state.
- Preserve the existing clean-Session assertion that the first allocator DML
  is the owner-conditional no-op conversation UPDATE.
- Successful earlier repository `commit=False` calls remain composable because
  each method flushes its own changes.

## 2. `commit=True` failures outside `operation()` are not rolled back

The current exception boundary excludes `begin_nested()`, `nested.commit()`,
and `session.commit()`.

Required behavior:

- Cover savepoint setup/flush or final commit failure with a RED focused test.
- Enclose the entire write lifecycle in one exception boundary.
- On any `commit=True` failure, call `session.rollback()` and leave the Session
  usable with no partial rows/counter changes.
- On `commit=False` failure, roll back only the method savepoint and never call
  full `session.rollback()`.
- Preserve the existing caller-level rollback composition test. Do not broaden
  successful `commit=False` semantics unless required by the fix.

## 3. A cached stale trigger can create a second Turn

After the conversation lock, `_require_message()` may return an identity-map
instance whose `turn_id=None` predates another Session's committed Turn.

Required behavior:

- Add a RED regression where Session B preloads the trigger, Session A commits
  the first Turn, then Session B attempts a Turn.
- Force a fresh post-lock trigger state and link with a conditional database
  update (`turn_id IS NULL`) or an equivalently race-safe mechanism.
- The second call raises `ConversationInvariantError`, creates no Turn, leaves
  the first link intact, and does not advance `next_turn_plan_version`.
- Do not add a migration in this repository checkpoint.

## 4. Same-conversation event commits can invert cursor visibility on PostgreSQL

An event ID may be allocated before commit while a later ID commits first; a
consumer can advance past the earlier uncommitted event permanently.

Required behavior:

- `append_event()` must acquire the authenticated conversation write lock before
  validating its optional Turn and inserting the event. This serializes event
  allocation/commit order within one conversation.
- A global lock is not required: `events_after` is conversation-scoped, so
  interleaving different conversations is safe and IDs may have gaps.
- Add focused structural/concurrency coverage proving the lock occurs before
  event insertion and retain global-cursor isolation tests.

## Delivery

- Follow TDD: record meaningful RED then GREEN for each regression.
- Run the focused repository suite and full backend suite.
- Append commands/results and fix rationale to
  `.superpowers/sdd/conversation-repository-report.md`.
- Run `git diff --check`, commit the fix, and return DONE with commit SHA and
  concise test evidence.
- Do not spawn a reviewer; the controller will dispatch re-review.
