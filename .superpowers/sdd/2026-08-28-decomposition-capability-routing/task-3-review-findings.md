# Task 3 Independent Review Findings

## Verdict

**REQUEST CHANGES**

- Critical: 0
- Important: 2
- Minor: 0

## Spec Compliance

### Important I1 — The outbound request redacts a required budget limit

**Location:** `backend/secagent/agents/coordinator.py:200` (the enclosing `redact_mapping` call; the affected value is added at line 211)

`payload` is passed through `redact_mapping` after `budget_limits` has been populated. The shared redactor treats every key containing the substring `token` as sensitive, so both `payload["budget_limits"]["max_context_tokens"]` and `payload["context"]["budget"]["max_context_tokens"]` become the string `"***REDACTED***"`. A directed probe against the captured `ModelRequest.user` produced:

```text
{"max_context_tokens":"***REDACTED***","max_model_calls_per_subtask":2,...}
```

This violates the requirement that the canonical model request contain the turn's budget limits. It also means the model does not receive the actual context-token ceiling even though the coordinator correctly enforces that ceiling before the call. The existing request test only checks the top-level key set, so it does not detect the corrupted value.

**Fix:** redact only untrusted textual/context values before assembling the backend-owned envelope, or otherwise preserve/reinsert the strictly validated numeric budget mapping after redaction. Add an assertion that every serialized `budget_limits` value, especially `max_context_tokens`, equals the original `TurnBudgetSnapshot` value.

### Important I2 — Two context enum fields violate the strict/no-coercion DTO contract

**Locations:** `backend/secagent/agents/coordinator.py:94` and `backend/secagent/agents/coordinator.py:112`

`CoordinatorCompletedSubtask.provider` and `CoordinatorTool.risk_level` explicitly use `Field(strict=False)`. Consequently, Python `bytes` inputs are silently decoded and accepted:

```text
CoordinatorCompletedSubtask(..., provider=b"glm", ...) -> LogicalProvider.GLM
CoordinatorTool(..., risk_level=b"low", ...) -> RiskLevel.LOW
```

This contradicts the Task 3 requirement that the context DTOs be strict and reject coercion. Ordinary JSON string enum values still need to be accepted, but non-string scalar inputs must not be converted.

**Fix:** add `mode="before"` validators (as the existing `ConversationSettings` contract does) that require `isinstance(value, str)` before enum parsing, rather than relying on `strict=False`. Add regression cases for `bytes` and other non-string inputs on both fields.

## Task / Code Quality

The implementation otherwise follows the requested separation and control flow: `decompose`/`synthesize` remain fixed to DeepSeek, Flash model equality is checked before provider invocation, live/auto do not fall back to GLM or Mock, `subtask_execute` requires a logical preference, provider availability is router-derived, authorized tools are filtered in order, the pre-redaction context-size check occurs before the model call, the returned mapping is redacted before validation and reuse, and the real Mock provider supplies deterministic dual-provider/single-budget plans with the required emulation evidence. Legacy stages and token limits remain intact.

The two findings also expose focused test gaps: the canonical-request test should verify budget values, not only shape, and the strictness tests should cover enum-valued fields rather than only a text scalar.

## Verification

- Focused Task 1/2/3 suites: **151 passed, 1 warning**.
- `git diff --check`: passed.
- Directed probes reproduced both findings without modifying production or test code.

