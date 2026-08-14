# Task 4 Report: Task Ownership, RBAC, User Administration, and Audit

## Scope and baseline

- Baseline: `6e92e5a`
- Implemented only Task 4 concerns: task ownership/RBAC, administrator user management, append-only application audit records, common error envelopes, and approval expiry.
- Reused the existing `owner_id`, `audit_events`, and `approvals.expires_at` schema. No migration was required.
- Preserved refresh rotation and logout-lineage behavior while adding user-first locking for concurrent password/disable revocation.

## Design delivered

- Every task API requires a real Bearer-authenticated user.
- Analysts create, list, read, report on, and mutate only their own tasks. Administrators can access all tasks.
- `TaskRepository.get_authorized()` and `list_authorized()` are the authorization boundary. Known cross-owner access returns 403 and records `task.access_denied`; missing IDs return 404.
- `/api/admin/users` supports admin-only list/create/update. PATCH accepts only `is_active` and/or a replacement password. The tenth-user team cap is enforced transactionally.
- Disabling a user or replacing a password revokes all active refresh sessions in the same transaction. Active administrator rows are locked in a stable order, and the last active administrator cannot be disabled.
- Refresh rotation resolves and locks the user before claiming the refresh token, giving user changes and rotation a consistent user-to-session lock order while preserving logout lineage revocation.
- Audit writes are insert-only through `AuditService.record()`. Details are recursively field-redacted; secret-shaped strings, unsupported objects, resource IDs, and IP fields are sanitized before persistence.
- Task create/lifecycle/approval state and their audit event commit atomically. Long-running task execution records `task.run: started` before the runner executes.
- Approval decisions atomically consume one pending row and conditionally move a task out of `waiting_human`, preventing approve/reject and approve/cancel races.
- New approvals expire after 24 hours. Legacy rows with null `expires_at` use `created_at + 24h`. Expired decisions return 409, record `approval.expired`, and leave the task `waiting_human`.
- All error responses use exactly `{error:{code,message,fields,trace_id}}`; framework 404/405, validation errors, auth errors, conflicts, and generic 500 responses use the same contract. Raw exception/provider content is not returned.

## RED evidence

The following failures were observed before their corresponding production changes:

- RBAC: unauthenticated task list returned 200; cross-owner read returned 200; task responses lacked `owner_id`; analyst lists included other owners.
- Admin routes: all `/api/admin/*` requests returned 404; duplicate, last-admin, patch validation, and refresh-revocation cases failed.
- Audit: module initially absent; then denied access, login failure, task actions, provider checks, and expiry events were absent.
- Errors: responses used `detail` or plain text, unexpected failures were not JSON, and framework 404/405 bypassed the custom contract.
- Approval expiry: an expired approval was accepted and moved the task out of `waiting_human`.
- Hardening RED cases: malformed JSON produced 500; arbitrary audit objects/tuples leaked serialized content; missing pending approval returned 404; the eleventh user was accepted; refresh rotation did not lock the user; failed-login identifiers were stored raw; concurrent approval/task transitions were not protected; legacy null expiry returned a generic conflict.

## GREEN implementation and regression coverage

- Added integration coverage in `test_rbac.py`, `test_admin_users.py`, `test_audit_events.py`, and `test_error_contract.py`.
- Existing task, scene, system, and authentication tests continue to use real login flows; no authentication dependency overrides were added.
- Added real database concurrency coverage for single-consumption approval decisions and single-winner transitions out of `waiting_human`.
- Re-ran refresh rotation/replay, logout successor revocation, and in-flight rotation/logout tests after changing lock order.
- Updated legacy auth error assertions only to account for per-request trace IDs and the new common envelope.

## Verification

- Task 4 required set: 30 passed.
- Backend full suite before the final legacy-expiry parameterization: 84 passed; the parameterized legacy case adds one additional passing case.
- Coverage run: 84 passed, total backend coverage 93% (Task 4 production modules range from 81% to 97%).
- `python -m compileall -q backend/secagent`: passed.
- `git diff --check`: passed.
- No repository linter executable/configuration was available; no package was installed solely for linting.
- Normal test runs show one pre-existing Starlette/httpx deprecation warning. The coverage run additionally surfaced SQLite connection ResourceWarnings during garbage collection; these are test-engine lifecycle warnings, not failed assertions.

## Independent review and self-review

- Independent review initially found approval decision races, refresh/user lock inversion, split state/audit commits, malformed JSON handling, and attacker-controlled login audit IDs.
- The implementation was revised with conditional SQL updates, consistent lock order, atomic transaction boundaries, bounded/hash-based login identifiers, and regression tests.
- Follow-up review found the approve/cancel race and legacy null expiry; both were addressed with conditional task transitions and `created_at + 24h` fallback semantics.
- Secret scan by inspection confirms no password, JWT, cookie, API key, raw external response, or stack content is intentionally persisted or returned by the new audit/error paths.

## Residual risks and decisions

- The brief explicitly requires 403 for a known cross-owner task and 404 for a missing task. This distinction reveals existence to an authenticated analyst; it is retained to match the specified contract and is covered by tests.
- Concurrency regression tests run on SQLite. The SQL uses conditional updates/row locking compatible with PostgreSQL, but PostgreSQL-backed concurrency tests remain desirable in CI.
- Audit append-only behavior is enforced at the application/API boundary: there are no audit update/delete service methods or routes. Database administrators necessarily retain direct database authority.
- The warning-only Starlette/httpx compatibility issue is outside Task 4 and should be handled in dependency maintenance.

## Main-review security fixes

- Tightened audit persistence with an audit-only recursive policy for generic `key`, body/response-body, exception, client-IP/IP, and identifier aliases. The general ledger redactor remains less destructive so ordinary business-key fields retain their value.
- Extended recursive string scrubbing for labeled credentials, including quoted values and values nested in mappings or sequences.
- Approval reasons are scrubbed and limited to 1000 characters at the repository persistence boundary. Ledger snapshots scrub again so legacy rows cannot expose credential-shaped text, while ordinary explanatory text remains intact.

### Fix RED/GREEN evidence

- Audit RED: the new persistence regression failed because a generic `key` value was stored verbatim. GREEN: the strict-alias and existing recursive-redaction cases passed together (2 passed).
- Approval RED: the API/DB regression showed the full reason stored verbatim, and the repository-boundary regression stored 2016 characters. GREEN: both regressions passed (2 passed).
- Quoted-secret RED: a quoted password containing spaces left a suffix in the persisted reason. GREEN: the regression passed after quoted values were scrubbed as one unit.
- Focused audit file: 15 passed before the final quoted-secret refinement; the final verification below reruns the complete required sets.

### Fix self-review and risk notes

- The strict generic `key` policy is intentionally scoped to append-only audit details; normal `redact_mapping()` consumers are unchanged for ordinary business data.
- Sanitization occurs before the approval update statement, so raw user-controlled reasons never enter the database through the repository. Snapshot-time scrubbing is defense in depth for pre-existing rows.
- Pattern-based redaction cannot classify every possible opaque secret. Field-level audit redaction is deliberately conservative, and arbitrary unsupported objects remain non-serializable placeholders.

### Fix verification

- Task 4 required set: 35 passed.
- Backend full suite: 88 passed.
- Both runs emitted only the existing Starlette/httpx deprecation warning.
- `python -m compileall -q backend/secagent`: passed.
- `git diff --check`: passed.
