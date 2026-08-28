# Conversation HTTP/SSE pre-flight review

结论：**NOT PASS**（Critical 2 / Important 4 / Minor 0）。

审查基线为干净的 HEAD `2fc0fa0cbb244d3338715e0f91c1898fac60738f`。本次只读核对了 brief、现有 task REST/SSE/ticket/error/main 边界、conversation service/repository/domain/storage/event/audit/auth 组合点及相关测试；除本报告外未改文件。

## Critical

### C1. query-string ticket 与“raw JWT never logged”的绝对要求互相矛盾

brief 同时规定 `GET .../events?ticket=...`，又规定 raw JWT “never ... logged”。标准 Uvicorn access logger 使用包含 query string 的完整请求路径，因此默认部署会把 JWT 写入 access log；仅仅保证新 router 自己不调用 logger 无法满足绝对表述。expected production surface 也没有给出反向代理/access logger 的脱敏边界。这是隐私要求，不应留给实现者猜测。

建议用以下文字替换 brief 中 “Raw JWTs and raw JTIs are never persisted, audited, or logged” 一句：

> Application code must never persist, audit, or explicitly log a raw JWT or raw JTI. Because the public contract carries `ticket` in the query string, the deployed HTTP access logger and every reverse proxy must either disable access logging for this route or redact the complete value of the `ticket` query parameter before writing a log. Add an automated test against the configured server/access-log path proving that the issued JWT is absent. The replay store still receives only the SHA-256 JTI digest.

如果无法把 access-log 配置纳入本 checkpoint，则必须把 ticket 改为不会进入 URL 的 header；否则不能继续声称“never logged”。

### C2. multipart parser 抛错前产生的 UploadFile 无法由应用异步 close

brief 要求“Every parsed `UploadFile` is asynchronously closed ... on every failure”。当 `await request.form()` 成功返回 `FormData` 时，router 可以在 `finally` 中 `await form.close()`，并能覆盖 accepted、unknown 和 wrong-field uploads；但当 Starlette multipart parser 在返回 `FormData` 之前抛错时，应用拿不到 parser 已创建的 `UploadFile` 对象，无法逐一 `await upload.close()`。当前 Starlette 会在 parser 的异常路径同步关闭内部文件句柄，但这不等价于 brief 的字面要求，且无法由 boundary test 要求 router 异步调用。

建议用以下文字替换 UploadFile close 段落：

> Once `request.form()` returns a `FormData`, enter a `try/finally` immediately and `await form.close()` in the `finally`; this must close every `UploadFile` in `form.multi_items()`, including uploads carried by unknown or rejected fields, on success, replay, and every subsequent validation/service failure. If Starlette raises before returning `FormData`, map the parser failure to the required 422 envelope and rely on Starlette's parser-owned error cleanup; no application-owned `UploadFile` is available to await-close on that path. Tests must distinguish these two ownership cases.

## Important

### I1. ticket infrastructure failure lacks a deterministic exception/HTTP contract

The existing `StreamTicketService.consume()` calls `replay_store.consume()` without wrapping backend errors. A Redis connection/timeout error therefore becomes the installed generic 500. The brief specifies 503 only for issuance signing configuration failure, and 401 for invalid/replayed/scope tickets, but says nothing about signing-key unavailability during consume, JWT encode failure, Redis failure, or a Redis write whose outcome is unknown. Implementations can reasonably return 401, 500, or 503, so this is not precisely testable.

Add this text to the conversation-ticket section:

> Define separate conversation-ticket exceptions for invalid tickets and unavailable ticket infrastructure. Missing/unreadable signing configuration, JWT signing failure, and an exception raised by `TicketReplayStore.consume` are infrastructure failures and map to HTTP 503 code `event_stream_unavailable` on both issuance and SSE consumption. Signature/expiry/required-claim/purpose/user/resource mismatch and `consume(...) == False` are invalid-ticket outcomes and map to HTTP 401 code `invalid_stream_ticket`. Never translate an infrastructure failure into `invalid_stream_ticket`, never expose its raw exception, and do not automatically retry a replay-store consume whose write outcome may be unknown. A missing/unreadable signing secret must not make `create_app()` crash; construct both ticket services with an unavailable key while preserving legacy task route codes.

### I2. optional bearer parsing and post-consume failure burn semantics are underspecified

“If an Authorization bearer is supplied” does not say whether a present Basic header, an empty/duplicate Bearer header, or authentication configuration failure is treated as absent, 401, or 503. The prescribed order consumes the JTI before the active-user and conversation authorization reads, but the brief only explicitly promises no burn for invalid cursor and wrong ticket scope. It does not state whether inactive-user, missing-conversation, and RBAC-forbidden responses burn the ticket. This affects security and retry behavior.

Add this text to the SSE route section:

> Absence of the `Authorization` header selects ticket-only authentication. If the header is present, it must contain exactly one syntactically valid Bearer credential; a different scheme, empty/malformed/duplicate Bearer credential, or `AuthenticationError` returns 401 `invalid_stream_ticket` and does not consume the ticket. `AuthenticationConfigurationError` returns 503 `event_stream_unavailable` and does not consume it. The fixed order is: parse cursor; validate an optional bearer; cryptographically validate all ticket claims and URL/user scope; atomically consume JTI; load the subject and require active; authorize/read the conversation. Consequently inactive-user 401, post-consume conversation 404, and post-consume RBAC 403 burn the ticket; invalid cursor, invalid bearer, claim/scope mismatch, and pre-consume infrastructure failure do not.

### I3. manual JSON/multipart validation has more than one compliant envelope/classification

The installed 422 handler only covers FastAPI `RequestValidationError`; `request.json()`, `request.form()`, and `MessageSubmission.model_validate*()` are manual paths. The brief fixes the code but not message/fields. It also says JSON with `relative_paths` is invalid while separately saying count validation is authoritative in service/storage, so that request can plausibly be either `validation_error` (transport structure) or `attachment_validation_failed` (storage count). Media matching must also reject prefixes such as `application/json-patch+json`; `startswith` as used by the legacy task parser would not implement “exactly two content types”.

Replace the corresponding validation paragraph with:

> Compare the parsed base media type case-insensitively for exact equality with `application/json` or `multipart/form-data`; media-type parameters are allowed, but suffix/prefix lookalikes are not. Catch JSON decode errors, multipart parser/structure errors, and Pydantic `ValidationError` explicitly and return `ApiError(422, "validation_error", "Request validation failed", fields=None)` without serializing `exc.errors()` or any input value. For JSON, nonempty `relative_paths` is this same transport validation error and `send_message` is not called. For a structurally valid multipart request, pass the ordered uploads and relative paths to `send_message` exactly once; a count mismatch or safe-path/ZIP/size/storage failure raised as `AttachmentStorageError` maps to 422 `attachment_validation_failed`. Unsupported media uses 415 `unsupported_media_type`.

### I4. the minimum test matrix does not prove the new boundary-specific risks above

Items 1–10 are broad, but do not force the writer dependency to remain transaction-clean, do not distinguish FormData-owned from parser-owned close behavior, do not cover replay-store/backend failures, and do not specify bearer/burn/access-log behavior. A passing suite could therefore still violate the key boundary requirements.

Append these test requirements:

> 11. Instrument the request-scoped service factory on create/patch/archive/send to assert one writer Session is shared by `ConversationRepository`, `TaskRepository`, `ConversationEventService`, and `AuditService`, differs from authentication/read sessions, and has no transaction/new/dirty/deleted state at public write entry; also cover response validation and exception teardown.
>
> 12. For every multipart result path (success, replay, DTO/structure rejection after `FormData` return, each mapped service error, and unexpected error), assert `FormData.close()` awaits closure of accepted and rejected-field uploads. Separately induce a parser-before-return failure and assert the framework-owned temporary file is closed and the response is sanitized 422.
>
> 13. Cover absent, unreadable, and malformed signing configuration; JWT encode/decode failure; replay store `False`; replay store exception before apply and outcome-unknown exception; and assert the exact 401/503 codes without JWT/JTI/Redis detail leakage. Capture the configured access log and assert the issued JWT never appears.
>
> 14. Cover absent Authorization, Basic, empty/malformed/duplicate Bearer, invalid/valid Bearer, authentication configuration failure, and a ticket-reuse matrix proving the stated burn/no-burn result for invalid cursor, bearer failure, wrong claims/scope, inactive subject, missing conversation, and forbidden conversation.

## Confirmed compatible / non-blocking

- A generator dependency that only constructs the writer Session/service is safe: SQLAlchemy Session construction and the repository/service constructors do not start a transaction; `current_user` already uses a distinct session. The writer remains clean provided the new dependency performs no query and handlers return existing Pydantic DTOs rather than lazy ORM rows.
- Existing conversation service outcomes match the requested REST mappings: read/detail returns `None` for missing, write paths raise `KeyError`, `ForbiddenResource` remains distinct, archive is soft and readable, and exact message replay returns the original `MessageSendRead` with `replayed=True`.
- `ConversationRepository.events_after` already enforces authorization, ordered cursor reads, and limit up to 1,000; `canonical_json_dumps(event.payload)` provides the required compact UTF-8-safe payload without adding message content.
- `main.py` can build one replay-store object and pass the same identity to legacy and conversation ticket services without changing lifespan. Adding independent routers/state does not inherently disturb legacy routes/tests.
- The SSE cursor language is already determinate if implemented as an ASCII full match `[0-9]+` (rather than Python `int()` permissiveness), with query precedence and parse-before-consume as stated.

Implementation should not start until C1/C2 and I1–I3 are incorporated or explicitly waived; I4 should be folded into the required RED/GREEN evidence.
