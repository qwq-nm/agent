# Conversation HTTP, multipart, and SSE checkpoint

Implement the public HTTP boundary over the reviewed conversation service at
HEAD `2fc0fa0c`. This checkpoint exposes conversation CRUD/detail, message
submission with multiple attachments, an independently scoped conversation
stream ticket, and resumable SSE. Do not add stop/failure/approval decisions,
DAG/Subtask/Worker, provider/model calls, queue jobs, synthesis, or frontend
changes. Do not change the legacy `/api/tasks` route or task-event wire contract.

## Expected files and composition

The expected production surface is:

- add `backend/secagent/api/conversations.py` for REST and message routes;
- add `backend/secagent/api/conversation_events.py` for ticket/SSE routes and
  the independently testable async stream generator;
- extend `backend/secagent/auth/stream_tickets.py` with a conversation-specific
  ticket service and claims without weakening legacy task tickets;
- add a narrowly scoped access-log redactor (location chosen by the
  implementer) for `ticket` query-parameter values and install it for the
  configured Uvicorn access logger;
- extend `backend/secagent/api/errors.py` only with stable conversation API
  error types/mappings needed below;
- extend `backend/secagent/main.py` to build the conversation ticket service
  and include both routers;
- add focused integration/unit tests under `backend/tests/`.

Do not modify `ConversationService`, `ConversationRepository`, storage, schema,
Task runtime, or frontend unless a new failing boundary test proves a real
integration defect. If that occurs, make the smallest TDD fix and document it.

Each request-scoped `ConversationService` must be constructed from one fresh
writer `Session`; `ConversationRepository`, `TaskRepository`,
`ConversationEventService`, and `AuditService` share that exact object. Pass the
app `session_factory` separately for preflight/read refresh, and construct a
fresh `ConversationStorageService(settings)` for the request. Authentication
uses its existing separate dependency/session. Do not query through the writer
session before invoking a public service write, because its clean-root gate is
intentional.

## REST contract

Add these authenticated routes under `/api/conversations`:

| Method/path | Request | Response |
|---|---|---|
| `POST /api/conversations` | strict `ConversationCreate` JSON | `ConversationRead`, HTTP 201 |
| `GET /api/conversations` | `limit=100`, range 1..200 | ordered `list[ConversationRead]`, HTTP 200 |
| `GET /api/conversations/{id}` | pagination query below | `ConversationDetailRead`, HTTP 200 |
| `PATCH /api/conversations/{id}` | strict `ConversationPatch` JSON | `ConversationRead`, HTTP 200 |
| `DELETE /api/conversations/{id}` | no body | archived `ConversationRead`, HTTP 200 |

Detail query parameters are exact:

- `after_sequence=0`, nonnegative integer;
- `message_limit=100`, range 1..500;
- `after_plan_version=0`, nonnegative integer;
- `turn_limit=100`, range 1..500.

FastAPI validation failures remain the installed structured 422 envelope.
Missing conversations use a stable 404 `conversation_not_found`; an existing
but unauthorized conversation preserves `ForbiddenResource("conversation")`
and HTTP 403. Admin authorization follows the repository/service contract.
DELETE is soft archive only: detail/history and SSE remain readable; rows/files
are never physically deleted. Repeated DELETE remains idempotently archived.

## Message submission contract

Add authenticated `POST /api/conversations/{id}/messages`. It requires the
exact, untrimmed `Idempotency-Key` header accepted by the strict `IdempotencyKey`
type: present, 1..255 characters, and not whitespace-only. Missing/invalid keys
return HTTP 400 code `invalid_idempotency_key`; never echo the key.

Accept exactly two content types:

1. `application/json`: body is strict `MessageSubmission`. It is valid only
   when `relative_paths` is empty because there are no upload parts.
2. `multipart/form-data`: exactly one string form field named `payload`, whose
   value is strict `MessageSubmission` JSON, plus zero or more repeated upload
   fields named `files`. Reject missing/duplicate/non-string payload, non-upload
   `files`, and any unknown form field. Preserve file list order. The payload's
   `relative_paths` count must equal file count; the service/storage performs
   the authoritative safe-path and size validation.

Compare the parsed base media type case-insensitively for exact equality with
`application/json` or `multipart/form-data`; parameters are allowed, while
suffix/prefix lookalikes are not. Other media types return HTTP 415 code
`unsupported_media_type`. Catch JSON decode errors, multipart parser/structure
errors, and manually raised Pydantic `ValidationError` explicitly and return
`ApiError(422, "validation_error", "Request validation failed", fields=None)`;
never serialize `exc.errors()` or an input value. For JSON, nonempty
`relative_paths` is this same transport validation error and `send_message` is
not called. For structurally valid multipart, pass the ordered uploads and
relative paths to `send_message` exactly once; a count mismatch or
safe-path/ZIP/size/storage failure raised as `AttachmentStorageError` uses the
mapping below.

`AttachmentStorageError` returns HTTP 422 code
`attachment_validation_failed` with a generic message that contains no client
filename, storage path, content, hash, secret, or raw exception. Every parsed
upload has an explicit ownership rule: once `request.form()` returns a
`FormData`, enter `try/finally` immediately and `await form.close()` in the
`finally`. This closes every `UploadFile` from `form.multi_items()`, including
uploads carried by unknown or rejected fields, on success, replay, and every
subsequent validation/service/unexpected failure. If Starlette raises before
returning `FormData`, map the parser failure to the sanitized 422 envelope and
rely on Starlette's parser-owned cleanup because the application owns no
returned `UploadFile`. Tests must distinguish these two ownership cases.

Call `ConversationService.send_message(...)` once. A new message returns
`MessageSendRead` with HTTP 201; an exact replay returns the original DTO with
`replayed=true` and HTTP 200. Do not enqueue or call a model/provider.

Map service outcomes to exact structured errors without leaking content,
filenames, idempotency keys, raw database errors, or filesystem paths:

- missing conversation / `KeyError`: 404 `conversation_not_found`;
- `ArchivedConversationError`: 409 `conversation_archived`;
- `MessageIdempotencyConflict`: 409 `message_idempotency_conflict`;
- `AttachmentIdempotencyConflict`: 409 `attachment_idempotency_conflict`;
- `ConversationReplayCorruptState`: 409 `conversation_replay_corrupt`;
- `ConversationCommitOutcomeUnknown`: 503
  `conversation_commit_outcome_unknown`, message instructing the client to
  retry the same idempotency key; do not expose the underlying exception;
- `AttachmentStorageError`: the 422 mapping above.

The API must not catch `ForbiddenResource` into a 404 or 409. Programming/state
errors not listed here remain the generic installed 500 response.

## Conversation stream tickets

Add a conversation-specific ticket/claims API in
`auth/stream_tickets.py`. It may share private encoding helpers with the legacy
task service, but callers cannot supply a purpose or claim name. Preserve the
existing `StreamTicketService`, `StreamTicketClaims`, task purpose/claim names,
method signatures, and all legacy tests.

The new token contains only required claims `sub`, `conversation_id`, `exp`,
`jti`, and `purpose="conversation_events"`; its claims DTO is strict. It uses
the existing signing algorithm, exact 60-second TTL, random JTI, and the same
`TicketReplayStore` instance as task tickets. Application code must never
persist, audit, or explicitly log a raw JWT or raw JTI; the replay store
receives only the SHA-256 JTI digest. Because the browser-compatible public
contract carries `ticket` in the query string, install a Uvicorn access-logger
filter that redacts the complete value of every query parameter whose
percent-decoded name is exactly `ticket`, including repeated or encoded-name
forms, before formatting/writing the record. Add an automated log-record test
proving the issued JWT is absent and the replacement marker is present.
Deployment documentation must additionally require every upstream reverse
proxy/access logger to disable logging for this route or apply equivalent
redaction; the application cannot configure an external proxy.

A ticket is single-use. Validate signature/expiry/required claims, purpose,
conversation ID, and optional expected user before consuming the JTI, so a
wrong-scope attempt does not burn an otherwise valid ticket. A task ticket must
never work on a conversation stream and vice versa.

Define separate conversation-ticket exceptions for invalid tickets and
unavailable ticket infrastructure. Missing/unreadable signing configuration,
JWT signing failure, and an exception raised by `TicketReplayStore.consume`
are infrastructure failures and map to HTTP 503 code
`event_stream_unavailable` on both issuance and SSE consumption.
Signature/expiry/required-claim/purpose/user/resource mismatch and
`consume(...) == False` are invalid-ticket outcomes and map to HTTP 401 code
`invalid_stream_ticket`. Never translate infrastructure failure into
`invalid_stream_ticket`, expose its raw exception, or automatically retry a
replay-store consume whose write outcome may be unknown. After a replay-store
exception the client must obtain a fresh ticket rather than retrying that JWT;
its server-side burn state is deliberately indeterminate. A missing/unreadable
signing secret must not make `create_app()` crash: construct both ticket
services with the unavailable key while preserving legacy task route contracts.

In `main.py`, instantiate one replay store, pass it to both the existing task
ticket service and the new conversation ticket service, and expose the latter
as `app.state.conversation_stream_ticket_service`.

## Event ticket and SSE routes

Add:

- authenticated `POST /api/conversations/{id}/event-ticket`;
- `GET /api/conversations/{id}/events?ticket=...&after=...` returning
  `text/event-stream`.

Ticket issuance first authorizes the conversation. It returns exactly
`{"ticket": <jwt>, "expires_in": 60}`. Signing configuration failure returns
503 `event_stream_unavailable`; missing is 404; forbidden remains 403. JWT
signing exceptions use the same sanitized 503 mapping.

The SSE route parses the cursor before consuming the ticket. Query `after`,
when supplied, takes precedence over the `Last-Event-ID` header; otherwise use
that header, default 0. The value must be a nonnegative base-10 integer or
return HTTP 400 `invalid_event_cursor`. An invalid cursor must not consume the
ticket.

Absence of the `Authorization` header selects ticket-only authentication. If
the header is present, it must occur exactly once and contain exactly one
syntactically valid Bearer credential. A different scheme, empty/malformed or
duplicate Bearer credential, or `AuthenticationError` returns HTTP 401 code
`invalid_stream_ticket` and does not consume the ticket.
`AuthenticationConfigurationError` returns HTTP 503 code
`event_stream_unavailable` and does not consume it. A valid bearer binds the
ticket to that user; without a bearer, the ticket subject is the actor.

The fixed order is: parse cursor; validate the optional bearer;
cryptographically validate all ticket claims and URL/user scope; atomically
consume the JTI; load the subject and require active; authorize/read the
conversation. Invalid cursor, bearer, signature/expiry/claim/scope, or signing
infrastructure before replay-store access does not burn the ticket. An inactive
subject response, post-consume conversation 404, and post-consume RBAC 403 do
burn it. A replay-store exception returns sanitized 503 without a retry and has
the explicitly indeterminate burn state above. Bad
signature/expiry/replay/purpose/resource/user scope returns HTTP 401 code
`invalid_stream_ticket`; an existing conversation forbidden to the ticket
subject remains HTTP 403.

Implement `stream_conversation_events(...)` as an independently testable async
generator. It:

- accepts session factory, authorized actor, conversation ID, starting cursor,
  disconnect callback, and testable poll/heartbeat/max-polls controls;
- checks disconnect before each poll and supports sync or awaitable callbacks;
- opens and closes a fresh read Session per poll;
- reads at most 1,000 ordered events through
  `ConversationRepository.events_after(actor, id, cursor=..., limit=1000)`;
- advances the cursor only after each yielded event;
- emits exact frames
  `id: <cursor>\nevent: <event_type>\ndata: <canonical-json-payload>\n\n`;
- emits `: heartbeat\n\n` after 15 seconds without output;
- sleeps one second between production polls and never busy-loops;
- ends promptly on disconnect.

Use canonical compact UTF-8-safe JSON encoding from the conversation domain;
do not add message content or any fields outside the already persisted typed
payload. Archived conversation streams remain readable. Return headers
`Cache-Control: no-cache` and `X-Accel-Buffering: no`.

For deterministic integration tests, read an optional
`app.state.conversation_event_stream_max_polls`; production default is
unbounded.

## TDD and verification

Write tests first and observe meaningful RED before production changes. At
minimum cover:

1. router/main wiring and unauthenticated 401 for every REST/ticket route;
2. owner/admin CRUD, stable 404 versus 403, strict bodies, query boundaries,
   ordered detail pagination, repeated soft archive, archived history;
3. JSON text send (no files), first-send 201, exact replay 200, one
   message/Task/Turn/event/audit, and strict/missing idempotency header;
4. multipart zero/multiple files, repeated `files`, ordered relative paths,
   response metadata, accurate files on disk, exact replay, and all parsed
   uploads closed;
5. malformed/duplicate/unknown multipart fields, JSON-with-relative-paths,
   unsupported media, count/path/ZIP/size failures, and safe structured errors
   with no filename/path/content/key leakage;
6. every dedicated service-error mapping above, including commit-unknown 503
   with same-key retry guidance and ForbiddenResource preservation;
7. conversation ticket claims, TTL, single-use hashing, wrong user/resource/
   purpose without ticket burn, task/conversation cross-purpose rejection, and
   inactive user rejection;
8. SSE exact frames/canonical JSON, query/header cursor precedence, invalid
   cursor without ticket burn, resume/de-duplication, heartbeat, disconnect,
   owner/admin/forbidden/missing behavior, and archived-stream readability;
9. legacy task CRUD/events/tickets remain unchanged and pass;
10. no queue enqueue and no provider/model call from any new route.
11. instrument the request-scoped service factory on create/patch/archive/send
    to prove one writer Session is shared by `ConversationRepository`,
    `TaskRepository`, `ConversationEventService`, and `AuditService`, differs
    from authentication/read sessions, and has no transaction/new/dirty/deleted
    state at public write entry; cover response validation and exception
    teardown;
12. for every multipart result path (success, replay, DTO/structure rejection
    after `FormData` return, each mapped service error, and unexpected error),
    assert `FormData.close()` closes accepted and rejected-field uploads.
    Separately induce a parser-before-return failure and prove the
    framework-owned temporary file is closed and the response is sanitized 422;
13. cover absent, unreadable, and malformed signing configuration; JWT
    encode/decode failure; replay store `False`; replay store exception before
    apply and outcome-unknown exception; exact 401/503 codes without
    JWT/JTI/Redis detail leakage; and configured Uvicorn access-log redaction of
    the issued JWT;
14. cover absent Authorization, Basic, empty/malformed/duplicate Bearer,
    invalid/valid Bearer, authentication configuration failure, and a
    ticket-reuse matrix proving every stated deterministic burn/no-burn result
    plus the indeterminate replay-store exception contract.

Run focused unit/integration tests, then the full backend suite with
`PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. Run `git diff --check`,
self-review against this brief, and commit. Write the detailed report to
`.superpowers/sdd/conversation-http-sse-report.md` with RED-to-GREEN and exact
test evidence. Do not spawn subagents.
