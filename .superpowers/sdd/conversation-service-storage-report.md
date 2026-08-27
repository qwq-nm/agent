# Conversation service and attachment storage checkpoint report

## Status

DONE

Implementation commit: `69cdae2e` (`feat: add conversation service and attachment storage`)

Baseline: `53aecc94b5d95927e5fc6c2300878014f125227f`

Scope stayed within the checkpoint: domain/config, typed conversation events,
conversation attachment storage, `ConversationService`, the minimum authorized
idempotency lookup, archive locking needed for archive/send linearization, and
focused unit/integration tests. No HTTP, multipart route, SSE, DAG/Subtask,
provider, queue, worker, Job publication, or frontend changes were added.

## Changed files and contracts

- `backend/secagent/conversation_domain.py`
  - Adds strict `MessageSubmission`, `MessageWithAttachments`,
    `ConversationDetailRead`, and `MessageSendRead` DTOs.
  - Keeps `UserMessageCreate` as a strict compatibility subclass.
  - Adds the strict, non-trimming `IdempotencyKey` type.
- `backend/secagent/config.py`
  - Adds all nine server-owned conversation/storage defaults from the brief.
  - Rejects booleans and non-positive values and caps global conversation
    parallelism at three while retaining normal uppercase environment loading.
- `backend/secagent/conversation_repository.py`
  - Adds the owner-authorized, read-only
    `get_message_by_idempotency_key(...)` lookup.
  - Makes archive use the existing per-conversation write lock so archive/send
    ordering has the same linearization boundary as message append.
- `backend/secagent/services/conversation_events.py`
  - Adds the typed `append_message_created(...)` boundary with the exact
    seven-field payload whitelist and `commit=False` composition.
  - Adds recursive redaction plus canonical JSON encoding with an exact
    16,384-byte inclusive limit.
- `backend/secagent/services/conversation_storage.py`
  - Adds async multi-upload staging with bounded streaming, SHA-256, per-file
    and aggregate limits, NFC/single-segment filename validation, safe client
    relative paths, and server-derived content types.
  - Preserves ZIP inputs, detects by name/signature, uses the existing safe ZIP
    extraction path, and adds per-entry NFC/case-fold collision, Windows alias,
    ADS, trailing-dot/space, control-character, segment-length, traversal, and
    symlink checks with one extraction directory per archive.
  - Publishes through one atomic workspace move to
    `conversations/<conversation>/messages/<message>` using suffix-free opaque
    stored names and POSIX-relative validated storage refs.
  - Adds idempotent capability-owned staging cleanup and canonical UUID
    published-workspace cleanup with exact-depth and symlink/junction checks.
- `backend/secagent/services/conversation_service.py`
  - Validates exact writer Session identity across ConversationRepository,
    TaskRepository, typed event writer, and audit writer at construction.
  - Enforces a clean writer Session at every public write entry and uses the
    separate factory for short read/preflight/refresh sessions.
  - Implements create/list/detail/patch/archive with owner/admin authorization,
    strict DTO returns, soft archive, ordered detail composition, and same-root
    success audits.
  - Implements async send with preflight-before-staging, one root write
    transaction, legacy Task/Turn composition, immutable budget snapshots,
    deterministic goal/scope/title rules, attachment persistence, typed event,
    audit, exact replay identity, race replay, archive recheck, and synchronous
    filesystem compensation.
  - Treats connection-invalidated or post-COMMIT exceptions as uncertain,
    invalidates the writer Session, preserves the published workspace, and
    raises `ConversationCommitOutcomeUnknown` for future reconciliation.
- `backend/tests/unit/test_conversation_service_contract.py`
- `backend/tests/unit/test_conversation_events_service.py`
- `backend/tests/unit/test_conversation_storage.py`
- `backend/tests/unit/test_conversation_service.py`
  - Cover the contract matrix from the brief, including threaded file-backed
    SQLite and live PostgreSQL READ COMMITTED same-key barriers.

## TDD RED to GREEN evidence

All production behavior was introduced after a focused failing test. Meaningful
observed cycles included:

1. DTO/config contract
   - RED: collection failed because `ConversationDetailRead` and the other new
     DTOs were absent.
   - GREEN: DTO/config plus existing domain/config tests: `31 passed`.
2. Typed event writer
   - RED: `secagent.services.conversation_events` did not exist.
   - GREEN attempt exposed a second RED: passing the strict payload model into
     recursive redaction encoded `[UNSUPPORTED]` instead of the whitelist.
   - GREEN: `3 passed`, including deep redaction and exact 16,384/16,385-byte
     boundaries.
3. Attachment storage
   - RED: `secagent.services.conversation_storage` did not exist.
   - GREEN: initial storage matrix `31 passed`.
   - Additional security RED/GREEN cycles found and fixed:
     - a caller mutating an opaque batch ID could bypass the issued-capability
       cleanup check;
     - `asyncio.CancelledError` during partial staging left a workspace;
     - a broken directory symlink was not rejected because `exists()` is false.
4. Conversation service
   - RED: `secagent.services.conversation_service` did not exist.
   - GREEN iteration fixed post-commit refresh so returned DTO timestamps match
     committed SQLite reads, then reached `19 passed` for the initial service
     matrix.
   - Additional fail-closed RED/GREEN cycles found and fixed:
     - a key already owned by a non-`user/user_text` message replayed instead of
       conflicting;
     - a connection-invalidated COMMIT error was initially treated as an
       explicit non-commit rather than an uncertain outcome.
5. PostgreSQL barrier
   - RED prerequisite was satisfied by the task-owned PostgreSQL 16 container
     and explicit URL environment variable.
   - GREEN: the isolated live test passed with two simultaneous same-key sends,
     duplicate attachment multiplicity, order-independent identity, one winner,
     one replay, exact row/counter counts, and an explicit
     `transaction_isolation = read committed` assertion.

## Verification evidence

Environment variable used only for the live test:

```text
SECAGENT_TEST_POSTGRES_URL=postgresql+psycopg://codex_test:codex_test_pw@127.0.0.1:55432/codex_agent_test
```

The test reads this exact variable and skips ordinary local runs when it is
absent. The task-owned container was deliberately left running for parent
review/replication.

Live PostgreSQL barrier command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:SECAGENT_TEST_POSTGRES_URL='postgresql+psycopg://codex_test:codex_test_pw@127.0.0.1:55432/codex_agent_test'
.venv\Scripts\python.exe -m pytest backend/tests/unit/test_conversation_service.py::test_concurrent_same_key_postgresql_read_committed_barrier -p no:cacheprovider -vv
```

Result: `1 passed in 4.33s`.

Final focused command (with the same PostgreSQL variable):

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/unit/test_conversation_service_contract.py backend/tests/unit/test_conversation_events_service.py backend/tests/unit/test_conversation_storage.py backend/tests/unit/test_conversation_service.py backend/tests/unit/test_conversation_repository.py backend/tests/unit/test_conversation_domain.py -p no:cacheprovider -q
```

Result: `101 passed, 3 warnings in 23.36s`.

Final full backend command (with `PYTHONDONTWRITEBYTECODE=1`, the PostgreSQL
variable above, and cache provider disabled):

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -p no:cacheprovider -q
```

Result: `410 passed, 3 warnings in 183.48s`.

`git diff --check` result: exit 0, no whitespace errors. Git printed only the
repository's Windows LF-to-CRLF advisory for three pre-existing tracked files.

The three pytest warnings are existing dependency/deprecation warnings: one
FastAPI TestClient/httpx warning and two Python 3.12 SQLite datetime-adapter
warnings from existing repository tests.

## Self-review and unresolved risks

- Requirements were checked section-by-section against the brief after the
  final implementation. A reviewer subagent was not used because this task
  explicitly prohibited spawning subagents; review was performed inline.
- The service guarantees synchronous compensation only where the database
  explicitly did not commit. As required, an uncertain COMMIT preserves files
  and surfaces a reconciliation-required error. There is intentionally no
  persistent publish-intent/orphan recovery protocol in this checkpoint, so it
  does not claim global database/filesystem atomicity across process crashes.
- The live PostgreSQL test drops/recreates only the project tables in the
  dedicated `codex_agent_test` database before/after its run. The task-owned
  container `codex-agent-conv-pg` was not stopped or removed so the parent can
  review and rerun it.
- No HTTP/multipart/SSE mapping exists yet for the dedicated archive,
  attachment-idempotency, corrupt-replay, or reconciliation-required errors;
  that belongs to the next checkpoint.
