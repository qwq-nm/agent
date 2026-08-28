# Conversation repository checkpoint

Work in `F:\codex\agent\.worktrees\conversational-multi-agent` from clean
HEAD `2560a615`. Implement persistence behavior only; REST, upload I/O, SSE,
jobs, model calls, DAG rows, and frontend code remain out of scope.

## Public boundary

Add a dedicated `secagent.conversation_repository` module instead of extending
the legacy `TaskRepository`. Expose a `ConversationRepository(Session)` with
explicit `commit()` and `rollback()` plus these capabilities (method names may
vary slightly, behavior may not):

- create/get/list/patch/archive conversations;
- add/get/list messages, including `after_sequence` pagination;
- add/list attachment metadata for one message;
- create/get/list turns and set/clear the active turn;
- append events and query `events_after` by global cursor.

Every public read/write, including initial create, receives an
`AuthenticatedUser`. `create_conversation(actor, payload)` always sets
`owner_id = actor.id`; callers cannot choose or override the owner. Every other
method validates conversation ownership before reading child IDs or writing.
Admins may access every existing conversation; analysts only their own.
Use the existing `ForbiddenResource("conversation")` API error contract for
denied access. A child ID must never reveal or mutate another conversation.

Return the structured DTOs from `conversation_domain`, never ORM rows or raw
JSON. Corrupt settings/budget/payload JSON must fail closed through
`canonical_json_loads`; never silently replace it with `{}`.

## Atomic allocation and idempotency

Allocate `conversation_messages.sequence` and
`conversation_turns.plan_version` with one database-side conditional
`UPDATE ... SET counter = counter + 1 RETURNING counter` and derive the
allocated previous value. Do not use SELECT/max, Python read-plus-write, or
SQLite-ineffective `FOR UPDATE` as the allocator.

On an allocator path, the first database statement in the method must be a
conditional no-op `UPDATE conversations ... RETURNING` that both obtains the
SQLite write lock / PostgreSQL row lock and checks actor ownership (omit the
owner predicate only for an authenticated admin). Do not begin with a SELECT
and then upgrade a SQLite read transaction. If the update returns no row, only
then distinguish not-found from forbidden without mutating state. After the
lock, re-check idempotency and relationship invariants, then allocate.

Message append accepts the validated optional idempotency key. It returns both
the message and whether it was newly created (a small frozen result dataclass or
tuple is acceptable), so the later service can avoid replaying attachments,
turns, events, and jobs.

- sequential and concurrent same-key/same immutable message identity
  `(role, kind, content)` return the same row with `created=False` on replay;
- same key with a different immutable identity raises a dedicated conflict
  error;
- replay does not advance the message counter;
- unique-conflict recovery must not roll back unrelated caller work or leave a
  counter gap. Put the method's lock/check/allocation/insert inside its own
  savepoint/nested transaction. With `commit=False`, failure may roll back only
  that savepoint; it must never call `session.rollback()` and destroy earlier
  caller work. With `commit=True`, the method owns final commit/failure
  rollback. A late failure in a multi-method composition is rolled back by the
  service/caller through repository `rollback()`.

## Relationship invariants

Validate before allocating/writing, and leave no partial mutations on failure:

- message.turn_id, trigger message, replan source, attachment message, event
  turn, and active turn all belong to the supplied conversation;
- a trigger message already linked to a turn cannot create another turn. This
  must be checked again after the conversation write lock is acquired; two
  concurrent creators may both have observed it as free beforehand;
- optional legacy Task exists and has the same non-null owner as the
  conversation;
- creating a turn allocates its plan version, inserts it, links the trigger
  message, and makes it active in the same serialized transaction;
- explicit `set_active_turn` may set only the same conversation's highest
  `plan_version`; `clear_active_turn(expected_turn_id)` is CAS and clears only
  if the pointer still equals the expected turn, so an old caller cannot erase
  a newer active turn;
- plan/message counters stay unchanged when validation fails;
- event subtask_id remains an opaque validated string with no ownership lookup
  in this checkpoint.

Default `commit=True` should match existing repository conventions; every
write also supports `commit=False` and flushes enough to return its DTO. List
ordering and pagination are exact:

- conversations: `created_at DESC, id DESC`, default limit 100, max 200;
- messages: `sequence ASC`, strict `sequence > after_sequence`, default limit
  100, max 500;
- turns: `plan_version ASC`, strict `plan_version > after_plan_version`, default
  limit 100, max 500;
- attachments: `created_at ASC, id ASC`, default limit 100, max 500;
- events: global cursor `id ASC`, strict `id > cursor`, default limit 100, max
  1,000.

All after/cursor values are nonnegative integers and limits are positive
integers within the stated maxima; booleans are not integers for validation.

## TDD requirements

Write tests first and observe meaningful RED before production code. Cover:

1. structured create/patch/archive/list/read and corrupt JSON fail-closed;
2. analyst owner isolation for every child family and admin cross-owner access;
3. cross-conversation message/turn/attachment/event/active-turn references and
   foreign Task owner rejected without counter or partial-row changes;
4. 10+ independent SQLite sessions concurrently append messages: unique
   contiguous sequences and exact final counter;
5. concurrent turns: unique contiguous plan versions and active turn points to
   the highest committed version; concurrent same-trigger creation yields only
   one turn and does not consume a second version;
6. two concurrent same-key messages return one ID/row, consume one sequence;
   same key/different content conflicts;
7. message + multiple attachments + turn + event composed with `commit=False`
   commit atomically, and an injected late invariant failure rolls all of it
   back including counters;
8. interleaved events for two conversations prove global monotonic cursor,
   strict `> cursor`, ordered results, and no cross-conversation leakage;
9. row-to-DTO conversion for every DTO and deterministic pagination boundaries.

Also prove stale active-turn clear cannot erase a newer turn and that setting a
non-highest turn is rejected.

Use a file SQLite database, independent sessions and Barrier/ThreadPoolExecutor
for concurrency tests. Do not weaken concurrency assertions to “no exception”.

## Verification and delivery

- Run focused repository tests, then the complete backend suite.
- Set `PYTHONDONTWRITEBYTECODE=1`; do not hide the documented third-party
  warning; run `git diff --check`.
- Write RED/GREEN/concurrency/full-suite/self-review evidence to ignored
  `.superpowers/sdd/conversation-repository-report.md`.
- Commit only repository source/tests with a focused message.
- Return commit, verification summary, and concerns.

Authenticated actors are loaded by a trusted service/auth dependency. Do not
accept actor IDs or roles from job payloads, and do not introduce actorless or
admin-bypass repository methods. Worker-owned mutations remain deferred until
a conversation-specific lease/fence exists.
