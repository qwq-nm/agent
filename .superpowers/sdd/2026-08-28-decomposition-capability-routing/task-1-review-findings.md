# Task 1 review findings: strict decomposition contract

## Scope and evidence

Reviewed the required implementation plan, Task 1 brief, Task 1 report, and
`review-f3318f90..320cb922.diff`, then inspected the committed implementation
and focused tests at commit `320cb922`.

Verification run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests/unit/test_conversation_decomposition.py backend/tests/unit/test_conversation_domain.py -q
git diff --check
```

Result: **29 passed**, with one documented pre-existing Starlette/httpx
deprecation warning; `git diff --check` passed. A targeted boundary probe also
confirmed that plan versions `0` and `-1` are currently accepted.

## Findings

### Important (1)

1. **`plan_version` does not enforce the required lower bound.**
   - Location: `backend/secagent/conversation_decomposition.py:127`
   - The authoritative workflow spec, section 7.1, requires
     `DecompositionDocument.plan_version: integer >= 1`. The current strict
     `int` field accepts `0` and negative values, and
     `validate_decomposition()` only checks equality with the caller-provided
     expected value (lines 161-162). Thus a model response with
     `plan_version=0` can pass the complete Task 1 contract whenever the
     caller supplies `0`, violating the schema invariant before the later
     persistence boundary.
   - Recommendation: declare the field with a lower-bound constraint, e.g.
     `plan_version: Annotated[int, Field(ge=1, strict=True)]` (or equivalent),
     and add parametrized tests that reject `0` and a negative integer.

### Critical (0)

No critical findings.

### Minor (0)

No minor findings.

## Spec Compliance

**CHANGES REQUESTED.** The implementation satisfies the requested enum set,
extra-field rejection, documented string/list bounds, duplicate detection,
policy-code exclusion, dynamic subtask budget, dependency existence,
self-dependency, and deterministic cycle validation. It does not satisfy the
specification's `plan_version >= 1` invariant.

## Task / Code Quality

**CHANGES REQUESTED.** The implementation is concise and deterministic, and
the focused tests cover the intended happy path and primary graph/schema
failures. Add the version-bound implementation and regression coverage above;
after that, this Task 1 change is suitable for approval.
