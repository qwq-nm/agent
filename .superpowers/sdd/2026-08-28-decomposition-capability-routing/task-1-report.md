# Task 1 report: strict decomposition contract

## RED

Added the happy-path, strict-schema, graph, dynamic-limit, and boundary tests
first. The required focused command failed during collection with the expected
`ModuleNotFoundError: No module named 'secagent.conversation_decomposition'`.

## GREEN

Implemented the strict `Capability`, `LogicalProvider`, and
`RouteReasonCode` enums, the `SubtaskSpec` and `DecompositionDocument` DTOs,
and deterministic dependency validation with dynamic plan-version and
subtask-budget checks. The focused suite then passed: **17 passed**.

## Files

- `backend/secagent/conversation_decomposition.py`
- `backend/tests/unit/test_conversation_decomposition.py`

## Verification

- `backend/tests/unit/test_conversation_decomposition.py`: **17 passed**
- `backend/tests/unit/test_conversation_domain.py` plus Task 1 suite:
  **29 passed**
- `git diff --check`: passed

## Concerns

- Pytest emits one pre-existing Starlette/httpx deprecation warning from the
  conversation-domain test environment; no new warnings were introduced.

## Fix round: review finding 1

Added a parameterized regression test for `plan_version` values `0` and `-1`.
The pre-fix focused run produced two expected failures (`DID NOT RAISE`).
Added the minimal `Field(ge=1, strict=True)` constraint, after which the
focused Task 1 plus conversation-domain suite passed: **31 passed**.

