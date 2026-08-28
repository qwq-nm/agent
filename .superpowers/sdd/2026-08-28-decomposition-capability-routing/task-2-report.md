# Task 2 report: deterministic model assignment policy

## RED

Added the assignment-policy matrix, provider preference, correction, tool
filtering, strict-output, and purity tests first. The required focused command
failed during collection with the expected
`ModuleNotFoundError: No module named 'secagent.services.assignment_policy'`.

## GREEN

Implemented immutable capability and route-reason mappings, strict
`AssignmentDecision`, safe `AssignmentPolicyError`, and deterministic pure
provider/tool assignment. Provider candidates are filtered by availability and
capability support, preferred/fixed matches are scored deterministically, valid
ties preserve the model proposal, and all route text comes from backend-owned
Chinese constants. Effective tools preserve model order while intersecting
registered and authorized sets.

## Files

- `backend/secagent/services/assignment_policy.py`
- `backend/tests/unit/test_assignment_policy.py`

## Verification

- `backend/tests/unit/test_assignment_policy.py`: **13 passed**
- Task 1 + Task 2 focused suites (`test_conversation_domain.py`,
  `test_conversation_decomposition.py`, `test_assignment_policy.py`):
  **44 passed**
- `git diff --check`: passed

## Concerns

- Pytest emits one pre-existing Starlette/httpx deprecation warning from the
  conversation-domain test environment; no new warnings were introduced.
- The SDD report is intentionally left outside the Task 2 code/test commit.

## Fix round: review findings I1/I2/M1

### RED

Added regressions for mutable module-level route-text aliases, both
contradictory `corrected`/`correction_codes` combinations, and an exploding
provider fake passed alongside valid logical providers through the actual
`available_providers` boundary. The pre-fix run produced the expected three
failures: the mutable alias changed controlled text, and both contradictory
DTOs were accepted.

### GREEN

Constructed `ROUTE_REASON_TEXT_ZH` directly from an immutable mapping wrapper,
added model-level consistency validation requiring
`corrected == bool(correction_codes)`, and exercised the fake at the policy
input boundary without adding provider calls.

### Fix-round verification

- Task 2 suite: **16 passed**
- Task 1 + Task 2 focused suites: **35 passed**
- `git diff --check`: passed
- Existing Starlette/httpx deprecation warning remains pre-existing.
