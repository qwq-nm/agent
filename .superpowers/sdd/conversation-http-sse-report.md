# Conversation HTTP, multipart, and SSE implementation report

## Scope and baseline

- Binding requirements: `.superpowers/sdd/conversation-http-sse-brief.md`.
- Recovery record: `.superpowers/sdd/conversation-http-sse-resume.md`.
- Baseline HEAD: `2fc0fa0cbb244d3338715e0f91c1898fac60738f`.
- Feature branch: `feature/conversational-multi-agent`.
- The interrupted implementation's entire dirty worktree was preserved and
  completed incrementally. No subagent was spawned.
- No changes were made to `ConversationService`, `ConversationRepository`,
  storage, schema, task runtime, the legacy `/api/tasks` wire contract, queue
  behavior, providers/models, or frontend code.

## Implemented production surface

- Added `backend/secagent/api/conversations.py`:
  - authenticated conversation create/list/detail/patch/soft-archive routes;
  - exact detail pagination limits and structured missing/forbidden behavior;
  - strict untrimmed `Idempotency-Key` validation;
  - exact base-media-type selection for JSON and multipart;
  - strict multipart structure, ordered upload forwarding, and unconditional
    `FormData.close()` ownership after a returned `FormData`;
  - sanitized transport, attachment, idempotency, replay, archive, not-found,
    and commit-outcome-unknown mappings;
  - first-send 201 and exact-replay 200 behavior.
- Added `backend/secagent/api/conversation_events.py`:
  - authenticated 60-second conversation event-ticket issuance;
  - cursor-first and optional-Bearer-first SSE admission ordering;
  - subject activation and post-consume authorization checks;
  - independently testable polling generator with a fresh read Session per
    poll, ordered batches of at most 1,000, canonical JSON frames, resumable
    cursors, heartbeats, disconnect handling, and bounded test polling;
  - no-cache / no-buffering response headers.
- Extended `backend/secagent/auth/stream_tickets.py`:
  - independent strict conversation claims and exceptions;
  - fixed purpose and claim names with no caller-controlled purpose;
  - exact TTL, random JTI, SHA-256-only replay-store input, and single use;
  - claim/scope validation before replay consumption;
  - separate invalid-ticket and infrastructure-failure outcomes;
  - the legacy task ticket public surface remains unchanged.
- Added `backend/secagent/security/access_log.py` and installed its filter on
  the configured `uvicorn.access` logger. It redacts every query value whose
  percent-decoded name is exactly `ticket`, including repeated/encoded names.
- Extended stable API error types and wired both routers in `main.py`.
- `main.py` now resolves an unavailable signing key without crashing, creates
  one replay store shared by legacy and conversation ticket services, and
  exposes `app.state.conversation_stream_ticket_service`.
- Updated `docs/deployment.md` to require every upstream proxy, ingress, load
  balancer, CDN, and access logger to disable logging for the conversation SSE
  route or apply equivalent complete ticket-value redaction.

## RED-to-GREEN evidence

### Inherited evidence (from the recovery record, not re-claimed as a rerun)

- The predecessor recorded ticket/access-log tests failing before the new
  conversation ticket surface existed, then 18 ticket/access-log plus legacy
  task event/ticket tests passing.
- The predecessor recorded 17 REST/message integration tests failing with
  route-level 404 before router wiring.
- The predecessor did not leave trustworthy SSE RED/GREEN evidence, so SSE was
  treated as unverified on takeover.

### Takeover baseline diagnosis

Command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/unit/test_conversation_stream_tickets.py `
  backend/tests/integration/test_conversation_api.py `
  backend/tests/integration/test_conversation_events_api.py `
  backend/tests/integration/test_task_events.py
```

Observed: **38 passed, 2 failed, 1 warning** out of 40. Both failures occurred
before the SSE generator ran: the tests used a hard-coded actor without a
matching `UserRow`, so SQLite rejected conversation creation on its owner
foreign key. The generator behavior did not have a meaningful RED at that
point. The test setup was corrected to use `seeded_analyst`; the SSE file then
ran **5 passed, 1 warning**.

### Replacement-implemented meaningful RED

A focused regression test signed a conversation ticket with a string `exp`
claim and required rejection before replay-store access:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/unit/test_conversation_stream_tickets.py::test_conversation_ticket_rejects_non_strict_claim_types_before_burn
```

Observed RED: **1 failed** because no
`ConversationStreamTicketError` was raised. Pydantic coerced the string `exp`,
and the replay store was reached. Root cause: the new claims DTO forbade extra
keys but did not enable strict type validation.

Minimal production fix: set
`ConversationStreamTicketClaims.model_config = ConfigDict(extra="forbid",
strict=True)`.

Immediate GREEN/regression command covered the new test, the complete
conversation ticket unit file, and legacy task events; observed **20 passed,
1 warning**. Subsequent expanded ticket coverage remained GREEN.

## Focused coverage and final evidence

The final focused suite contains 111 cases:

- conversation ticket/access-log unit tests: 17;
- conversation REST/message/multipart integration tests: 61;
- conversation event-ticket/SSE integration and generator tests: 21;
- unchanged legacy task event/ticket integration tests: 12.

Final focused command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/unit/test_conversation_stream_tickets.py `
  backend/tests/integration/test_conversation_api.py `
  backend/tests/integration/test_conversation_events_api.py `
  backend/tests/integration/test_task_events.py
```

Result: **111 passed, 1 warning in 52.61s**.

Focused coverage includes:

1. router/main wiring and anonymous rejection for every authenticated REST and
   event-ticket route;
2. owner/admin CRUD and stream access, stable 404 versus preserved 403,
   strict bodies/query boundaries, ordered detail pagination, repeated archive,
   archived history, and archived SSE readability;
3. JSON first-send/replay with one durable message/Task/Turn/event/audit and no
   queue enqueue;
4. multipart zero/multiple ordered files, exact bytes/metadata on disk, replay,
   and closure on success/replay;
5. missing/duplicate/non-string/unknown multipart fields, JSON relative-path
   rejection, media-type lookalikes, count/path/ZIP/size failures, and sanitized
   envelopes;
6. every dedicated message-service error mapping, Forbidden preservation, and
   same-key retry guidance for commit-outcome-unknown;
7. strict ticket claims, exact TTL, random JTI, hash-only replay input,
   single-use, invalid crypto/claims, wrong purpose/user/resource before burn,
   and task/conversation cross-purpose rejection;
8. exact canonical/resumable SSE frames, query/header cursor precedence,
   heartbeat, sync/async disconnect, one fresh closed Session per poll, limit
   1,000, cursor advancement after yield, and sleep between polls;
9. malformed/duplicate/valid/absent Bearer handling and authentication
   configuration failure before burn;
10. deterministic burn/no-burn behavior for invalid cursor, Bearer, claims,
    scope, inactive subject, post-consume missing/forbidden resources, replay,
    and replay-store infrastructure exceptions;
11. absent/unreadable/malformed signing configuration, JWT encode/decode
    failure, replay-store false/exception, shared app replay store, and exact
    sanitized 401/503 mappings;
12. configured Uvicorn log-record redaction proving an issued JWT is absent and
    `[REDACTED]` is present;
13. request-scoped service instrumentation proving one fresh clean writer
    Session shared by ConversationRepository, TaskRepository,
    ConversationEventService, and AuditService, distinct from auth/read
    sessions, with closure after normal response, mapped exception, unexpected
    multipart failure, and response-validation failure;
14. `FormData.close()` closure of all parsed accepted/rejected-field uploads on
    structure/DTO rejection, success, replay, every mapped service error, and
    unexpected errors, plus separately simulated framework-owned cleanup for a
    parser-before-return failure.

## Full backend verification

Command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests
```

Result: **512 passed, 1 skipped, 3 warnings in 233.73s** (513 collected).
The warnings are the existing Starlette TestClient/httpx deprecation warning
and two Python 3.12 SQLite datetime-adapter deprecation warnings. There were no
test failures or errors.

## Diff and self-review

- `git diff --check` passed before final staging. After force-adding this
  intentionally ignored report, `git diff --cached --check` also passed and
  covered every tracked/new task file.
- Secret scan of the production files and deployment documentation found no
  JWT, JTI, API key, Redis credential, client content, filename, or filesystem
  path embedded in production output/logging code. Test-only sentinel strings
  are asserted absent from responses.
- Review against all brief sections found no Critical, Important, or Minor
  defects. The review confirmed route ordering, session ownership, replay burn
  ordering, strict claim scope, sanitized errors, access-log redaction,
  upstream deployment guidance, and preservation of the legacy task surface.
- The normal review skill recommends a reviewer subagent, but this brief
  explicitly forbids subagents. A template-equivalent self-review was performed
  instead.

## Fix round 1

Reviewed base: `980270ad220c3028e86e392e183430c2b035e905`.
Authority remained `.superpowers/sdd/conversation-http-sse-brief.md`; all four
Important findings in
`.superpowers/sdd/conversation-http-sse-review-findings.md` were addressed.
No subagent was spawned.

### 1. Inactive subject with a matching Bearer burns the ticket

Root cause: the optional SSE Bearer preflight reused
`AuthService.authenticate_access()`, which rejected an inactive database user
before conversation-ticket replay consumption.

RED command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/integration/test_conversation_events_api.py::test_inactive_ticket_subject_with_matching_bearer_burns_ticket
```

Observed meaningful RED: **1 failed, 1 warning in 0.88s**. The first inactive
request returned the expected 401, but after reactivation the same ticket
returned 200 instead of replay 401, proving that the first request had not
burned it.

Minimal fix: optional SSE Bearer preflight now validates syntax plus the signed
access-token claims and returns only the cryptographically bound `sub`; it does
not load/check user activity. Conversation-ticket validation/consume follows,
then the existing post-consume subject load enforces active status. General
authentication routes were not changed.

GREEN command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/integration/test_conversation_events_api.py::test_inactive_ticket_subject_with_matching_bearer_burns_ticket `
  backend/tests/integration/test_conversation_events_api.py::test_invalid_cursor_and_invalid_bearer_do_not_burn_ticket `
  backend/tests/integration/test_conversation_events_api.py::test_authentication_configuration_failure_does_not_burn_ticket
```

Output summary: **3 passed, 1 warning in 1.23s**.

### 2. Real signing configuration failures use the issuance-specific 503

Root cause: event-ticket issuance used the general `current_user` dependency,
which translated absent/unreadable/malformed JWT configuration to generic
`service_unavailable` before the issuance route ran.

RED command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/integration/test_conversation_events_api.py::test_real_signing_configuration_failure_maps_only_event_ticket_to_stream_503
```

Observed meaningful RED: **3 failed, 1 warning in 1.11s**. Absent, missing-file,
and invalid-UTF8 signing configurations all returned HTTP 503 with code
`service_unavailable`, not `event_stream_unavailable`.

Minimal fix: event-ticket issuance now uses a narrowly scoped authentication
dependency that preserves normal 401 behavior and maps only
`AuthenticationConfigurationError` to `EventStreamUnavailable`. Conversation
REST and legacy task routes keep the general dependency and their existing
codes.

GREEN command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/integration/test_conversation_events_api.py::test_real_signing_configuration_failure_maps_only_event_ticket_to_stream_503 `
  backend/tests/integration/test_conversation_api.py::test_every_authenticated_conversation_route_rejects_anonymous `
  backend/tests/integration/test_conversation_events_api.py::test_conversation_ticket_owner_admin_forbidden_and_missing
```

Output summary: **11 passed, 1 warning in 4.75s**. The route-level test also
proves conversation REST and legacy `/api/tasks` remain `service_unavailable`
under the same real configuration failures.

### 3. Arbitrarily long and DB-overflow decimal cursors are deterministic

Root cause: `_event_cursor` accepted arbitrary ASCII digits and called
`int(raw, 10)` without bounding the result. A 5,000-digit query exceeded
CPython's integer-string conversion limit; `2**63` from `Last-Event-ID` reached
the repository outside the database BIGINT range.

RED command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/integration/test_conversation_events_api.py::test_decimal_cursor_beyond_db_bigint_is_safe_and_burns_normally
```

Observed meaningful RED: **2 failed, 1 warning in 2.79s**. The query path raised
the 5,000-digit `ValueError`; the header path passed `9223372036854775808` to
the repository instead of a safe database cursor.

Minimal fix: after the existing nonnegative base-10 regex, the parser strips
leading zeros and performs a length/lexical comparison without converting an
unbounded string. Values above signed BIGINT maximum normalize to
`9223372036854775807`, which is a safe no-further-events cursor. They remain
valid base-10 cursors, follow normal ticket consume/burn ordering, and never
reach the database as an overflow value.

GREEN command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/integration/test_conversation_events_api.py::test_decimal_cursor_beyond_db_bigint_is_safe_and_burns_normally `
  backend/tests/integration/test_conversation_events_api.py::test_invalid_cursor_and_invalid_bearer_do_not_burn_ticket `
  backend/tests/integration/test_conversation_events_api.py::test_last_event_id_header_resumes_when_query_cursor_is_absent
```

Output summary: **4 passed, 1 warning in 3.00s**.

### 4. Mandatory matrix additions

- Added route-level spies over `ProviderRuntimeFactory.build`,
  `ModelRouter.complete`, every configured provider's `complete`, and the fake
  queue while exercising create/list/detail/patch/send/ticket/SSE/archive.
  Command and output:

  ```powershell
  $env:PYTHONDONTWRITEBYTECODE='1'
  .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests/integration/test_conversation_api.py::test_all_new_routes_make_zero_queue_provider_or_model_calls
  # 1 passed, 1 warning in 0.41s
  ```

  The spies recorded zero calls/enqueues.
- Expanded the two-attachment multipart test to execute exact replay and assert
  identical DTO IDs/metadata/storage refs (except `replayed`), exactly one
  message/Task/Turn/event/audit, exactly two attachment rows, and exactly the
  two persisted files/bytes. Command and output:

  ```powershell
  $env:PYTHONDONTWRITEBYTECODE='1'
  .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests/integration/test_conversation_api.py::test_multipart_preserves_file_order_metadata_and_bytes
  # 1 passed, 1 warning in 0.48s
  ```

- Added syntactically valid but cryptographically invalid Bearer no-burn, plus
  distinct replay-store before-apply and outcome-unknown exceptions. The latter
  both return sanitized 503, invoke `consume` exactly once, and do not retry
  automatically. Command and output:

  ```powershell
  $env:PYTHONDONTWRITEBYTECODE='1'
  .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
    backend/tests/integration/test_conversation_events_api.py::test_cryptographically_invalid_bearer_does_not_burn_ticket `
    backend/tests/integration/test_conversation_events_api.py::test_distinct_replay_store_exceptions_are_503_without_automatic_retry
  # 3 passed, 1 warning in 0.99s
  ```

- The real absent/unreadable/malformed signing issuance cases are the route-level
  three-case test documented in section 2 above.

### Fix round 1 final verification

Amended focused command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  backend/tests/unit/test_conversation_stream_tickets.py `
  backend/tests/integration/test_conversation_api.py `
  backend/tests/integration/test_conversation_events_api.py `
  backend/tests/integration/test_task_events.py
```

Result: **121 passed, 1 warning in 64.27s** (121 collected): conversation
ticket/access-log 17, conversation API 62, conversation SSE 30, and unchanged
legacy task events/tickets 12.

Full backend command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests
```

Result: **522 passed, 1 skipped, 3 warnings in 247.85s** (523 collected). The
warnings remain the existing Starlette TestClient/httpx warning and two Python
3.12 SQLite datetime-adapter warnings; there were no failures or errors.

Fix round 1 self-review found no remaining Critical, Important, or Minor issue
against the four reviewer findings or the binding brief. Both the tracked and
staged forms passed `git diff --check` with no whitespace errors (only the
workspace's expected LF-to-CRLF notices).
