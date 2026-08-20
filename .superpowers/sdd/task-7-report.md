# Task 7 Report: Provider-Specific DeepSeek and GLM Adapters

## Outcome

Replaced the generic OpenAI-compatible provider with fixed, provider-specific
DeepSeek and GLM adapters. DeepSeek owns only PLAN/CRITIC with thinking enabled;
GLM owns only TASK_PARSE/REPORT. Live and auto routing never cross providers or
fall back to Mock, while Mock remains available only in explicit mock mode.

## TDD evidence

- Initial RED: the two brief-specified provider tests failed during collection
  because `secagent.providers.deepseek` and `secagent.providers.glm` did not
  exist.
- The first GREEN implemented safe error objects, structured response metadata,
  pooled transport, basic schema validation, and the two stage-specific adapters.
- The full provider/router RED matrix had 25 cases: 17 passed and 8 failed. The
  failures isolated the missing one-shot repair flow, GLM incorrectly inheriting
  DeepSeek thinking, and the old router's cross-provider/Mock/preferred-model
  fallback paths. After the fixes, all 25 passed.
- Later RED/GREEN cycles covered adapter construction, secret-file keys, GPT
  rejection, ledger metadata, bounded/redacted repair context, malformed choices,
  live configuration failure before client allocation, and application/worker
  client lifecycle.

## Implementation notes

- `ProviderFailure`/`ProviderUnavailable` expose only the fixed provider name and
  `ProviderErrorCode` in their message. Request ids are scrubbed, restricted to a
  safe character set, and limited to 128 characters.
- The transport retries only 429, 5xx, timeout, and network failures, with no more
  than three transport attempts. It honors numeric or HTTP-date `Retry-After`
  values with a 30-second cap and otherwise uses injected jittered exponential
  delays. Tests inject both sleep and jitter, so no real waiting or network access
  occurs.
- 401/403 are non-retryable AUTH failures and never reach repair. Empty content
  and `finish_reason=length` are classified as retryable EMPTY_CONTENT and
  TRUNCATED failures without local transport retry. Invalid JSON and schema output
  receive exactly one same-provider repair call; a failed repair is final.
- Initial payloads are JSON objects with `stream=false`, JSON-object response
  format, stage-specific `max_tokens`, a literal JSON instruction, schema, and an
  example. DeepSeek adds `thinking={"type":"enabled"}`; GLM never does.
- Repair prompt content has exactly three fields: bounded/redacted invalid JSON,
  bounded validation errors, and a bounded sanitized target schema. It excludes
  the original system/user prompt. Provider reasoning content and raw response
  bodies are discarded and never added to `ModelResponse`, errors, logs, or the
  ledger.
- Structured output validation covers required/additional fields, nested objects
  and arrays, local `$ref`, `anyOf`, enum/const, JSON scalar types, string lengths,
  and numeric bounds used by the stage Pydantic schemas.
- `ModelResponse` now carries request id, finish reason, token usage, and retry
  count. `LedgerService` persists only these safe metadata fields and continues to
  store a generic input summary rather than prompts.
- `build_providers` reads direct or file-backed keys, rejects blank/GPT model
  names, uses the configured defaults (`deepseek-v4-pro`, `glm-5.2`), and shares
  one `httpx.AsyncClient` between both live adapters.
- The synchronous Celery entrypoint reuses a process-local `asyncio.Runner`,
  router, registry, and provider pool. Celery process shutdown closes the pool and
  runner; FastAPI lifespan shutdown closes its own shared pool. Live missing-key
  configuration fails before any client is allocated.

## Verification

- Focused provider/router/metadata/config suite: `59 passed`, `0 failed`.
- Full backend suite: `185 passed`, `0 failed`; one pre-existing Starlette
  TestClient deprecation warning.
- Global coverage: `91.90%` (required minimum: `85%`).
- `python -m compileall -q backend`: passed.
- `git diff --check`: passed; Git emitted only repository line-ending conversion
  notices.
- Tests use `httpx.MockTransport`; no real provider endpoint or API key was used.

## Scope and security checks

- `backend/secagent/providers/openai_compatible.py` is deleted and no runtime
  import remains.
- No GPT adapter, model, route, or fallback was introduced.
- Manual `preferred_model` input cannot override fixed stage ownership.
- No database schema or migration changed; existing model-call columns were wired
  to the new safe response metadata.
- Worker changes are limited to provider client/loop reuse and shutdown; the
  existing async task execution and queue semantics are unchanged.

## Review fixes

Self-review found and fixed three boundary issues, each with a failing regression
test first:

- schema key-based redaction preserved the `authorization_scope` field name but
  replaced its validation rule; repair now sanitizes schema values without
  treating field names as secrets;
- a malformed `choices` entry leaked `AttributeError`; it now maps to the safe
  EMPTY_CONTENT taxonomy;
- live startup with one missing key could allocate a client before router
  validation; key/model validation now occurs before pool construction.

## Main-review fixes

The main review identified four Important boundaries. Each was reproduced with a
failing test before the fix:

- A repair response replaced the initial successful response's token usage. Usage
  now sums every successful provider response in the completion flow; for example,
  `10/2 + 8/3` records `18/5`. Missing, boolean, string, or negative counts safely
  contribute zero. `retry_count` remains the number of extra calls: one repair is
  one retry, plus any transport retries.
- An untrusted provider `finish_reason` could reach `ModelResponse` and the ledger
  unchanged. `ModelResponse` now normalizes a bounded allowlist of protocol values;
  unknown or oversized values become the fixed string `unknown`. DeepSeek, GLM,
  serialized responses, and ledger persistence are covered with a Bearer-shaped
  secret regression. `length` is still intercepted as TRUNCATED before response
  construction.
- Docker Compose overrode the Settings defaults with `deepseek-chat` and
  `glm-4-flash`. Its production environment defaults now match Settings and
  `.env.example`: `deepseek-v4-pro` and `glm-5.2`.
- The process-global worker runtime could be initialized twice and concurrent
  threads could call one `asyncio.Runner.run`, causing `RuntimeError` and leaking a
  pool. A process-level reentrant lock now serializes initialization, runner use,
  and shutdown; a runtime lock makes direct run/close calls reentrant-safe. Two
  Barrier-based thread tests prove one pool, successful calls without RuntimeError,
  shutdown waiting for an in-flight call, and exactly one close. This serialization
  matches Celery's default prefork model: each process handles one task at a time,
  while the configured three worker processes remain concurrent.

## Follow-up review fixes

Two additional provider-boundary regressions were reproduced with failing tests
and fixed:

- Provider `finish_reason` is now stripped, lower-cased, and allowlist-normalized
  before content extraction or output validation. Whitespace- or mixed-case
  variants such as `" LENGTH "` and `"LeNgTh"` immediately raise retryable
  TRUNCATED failures for both DeepSeek and GLM, with one request and no repair.
- Token usage is constrained to the PostgreSQL Integer-safe range
  `0..2147483647`. Boolean, string, negative, and oversized individual values
  contribute zero; valid initial and repair counts use saturating addition so the
  final `ModelResponse` and ledger values cannot exceed `2147483647`. DeepSeek,
  GLM, repair accumulation, domain serialization, and ledger persistence are all
  covered by regression tests.
