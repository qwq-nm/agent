# Conversation HTTP / SSE fix round 1 — scoped re-review

## Scope and method

- Reviewed only the fix range `980270ad220c3028e86e392e183430c2b035e905..966fdec7` and the authority brief, original findings, and amended implementation report.
- This is a read-only review of the worktree, index, and commits. No production or test source was changed and no full suite was re-run.
- `git diff --check 980270ad 966fdec7` produced no whitespace errors.
- Focused verification ran only for the newly changed risk paths:

  ```powershell
  $env:PYTHONDONTWRITEBYTECODE='1'
  .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
    backend/tests/integration/test_conversation_events_api.py::test_real_signing_configuration_failure_maps_only_event_ticket_to_stream_503 `
    backend/tests/integration/test_conversation_events_api.py::test_inactive_ticket_subject_with_matching_bearer_burns_ticket `
    backend/tests/integration/test_conversation_events_api.py::test_decimal_cursor_beyond_db_bigint_is_safe_and_burns_normally `
    backend/tests/integration/test_conversation_events_api.py::test_cryptographically_invalid_bearer_does_not_burn_ticket `
    backend/tests/integration/test_conversation_events_api.py::test_distinct_replay_store_exceptions_are_503_without_automatic_retry
  ```

  Result: **9 passed, 1 pre-existing TestClient deprecation warning**.

## Original finding verdicts

1. **ADDRESSED — inactive subject with a matching Bearer burns the ticket.**

   `backend/secagent/api/conversation_events.py:116-129` now validates the optional Bearer cryptographically and returns only its subject; it deliberately does not load the user or evaluate `is_active`. Ticket validation and replay-store consumption occur next at `:185-196`; the subject lookup/activity check is post-consume at `:198-201`. Thus a matching Bearer cannot short-circuit the deterministic burn. The regression at `backend/tests/integration/test_conversation_events_api.py:609-637` deactivates the owner, asserts the initial 401, reactivates the owner, and proves the same ticket is rejected as replay.

2. **ADDRESSED — absent, unreadable, and malformed real signing configuration use the issuance-specific code.**

   The narrow event-ticket dependency at `backend/secagent/api/conversation_events.py:132-157` preserves normal auth failures but translates only `AuthenticationConfigurationError` to `EventStreamUnavailable`; the issuance route consumes that dependency at `:161-175`. The three real-app configuration variants and their exact `event_stream_unavailable` assertion are covered at `backend/tests/integration/test_conversation_events_api.py:28-54`, while the adjacent REST and legacy task routes retain `service_unavailable`.

3. **ADDRESSED — oversized or database-unrepresentable decimal cursors do not produce a 500/stream failure.**

   `backend/secagent/api/conversation_events.py:85-101` validates base-10 syntax without unbounded integer conversion, then clamps values above signed BIGINT to its maximum safe cursor. The normal consumption path remains after cursor parsing at `:179-196`, so valid oversized cursors retain normal single-use ticket semantics. `backend/tests/integration/test_conversation_events_api.py:312-347` covers both a 5,000-digit query value and an overflow `Last-Event-ID`, asserts the clamped value reaches the repository, returns HTTP 200, and proves the ticket is then consumed.

4. **ADDRESSED — the cited mandatory test-matrix gaps are now covered.**

   - Provider/model/queue zero-call coverage exercises every new route at `backend/tests/integration/test_conversation_api.py:914-976`.
   - Multi-attachment exact replay, durable graph cardinality, and stored-file identity are asserted at `backend/tests/integration/test_conversation_api.py:186-253`.
   - A syntactically Bearer-shaped but cryptographically invalid credential is shown not to burn a ticket at `backend/tests/integration/test_conversation_events_api.py:464-484`.
   - Replay-store before-apply and outcome-unknown exception paths are separately modeled, require one consume call, and assert sanitized 503/no automatic retry at `backend/tests/integration/test_conversation_events_api.py:678-717`.
   - The real signing-configuration issuance matrix is the three-case test at `backend/tests/integration/test_conversation_events_api.py:28-54`.

## New findings introduced by this fix diff

- Critical: **0**
- Important: **0**
- Minor: **0**

## Diff-external observations (non-blocking)

None. Existing surfaces outside the requested fix range were not re-reviewed and do not affect this scoped verdict.

## Total verdict

**APPROVE.** All four original Important findings are addressed, and this fix diff introduces no Critical, Important, or Minor regression found by scoped static review plus the targeted 9-case execution.
