# Task 2 review findings: deterministic capability matrix and assignment policy

**Review range:** `d4cc3462..4e2536fc`  
**Verdict:** REQUEST CHANGES  
**Findings:** 0 Critical, 2 Important, 1 Minor

## Important

### I1. The supposedly controlled route-reason registry remains mutable, breaking policy purity

- **Location:** `backend/secagent/services/assignment_policy.py:61-79`
- **Evidence:** `ROUTE_REASON_TEXT_ZH` is a `MappingProxyType`, but it wraps the separately retained mutable `_REASON_TEXT` dictionary. Code able to import the module can mutate `assignment_policy._REASON_TEXT[...]`; the public mapping immediately reflects the replacement. A focused reproduction changed `BALANCED_MODEL_SUGGESTION` to `"mutated"`, after which `ROUTE_REASON_TEXT_ZH[...]` returned that value.
- **Impact:** The same assignment inputs can yield different `route_reason` output after global state is changed. This violates the specified backend-owned/controlled reason text and the pure deterministic boundary; it also permits unreviewed text to enter displayed or audited assignment data.
- **Fix:** Do not retain a mutable alias to the backing dictionary. Construct `ROUTE_REASON_TEXT_ZH = MappingProxyType({...})` directly (or keep the backing data private and immutable from the outset), and add a test that mutation is impossible through every module-level reference.

### I2. `AssignmentDecision` accepts contradictory correction state

- **Location:** `backend/secagent/services/assignment_policy.py:119-139`
- **Evidence:** `require_policy_codes` checks only code membership and uniqueness. `require_backend_reason` checks only text equality. Consequently, both of these invalid audit records validate: `corrected=False` with `[POLICY_TOOL_FILTERED]`, and `corrected=True` with `[]`.
- **Impact:** `corrected` and `correction_codes` are the downstream audit/display contract consumed by Task 3. Contradictory values can misrepresent whether backend policy overrode a model proposal, despite all codes individually being controlled.
- **Fix:** In the model-level validator require `corrected == bool(correction_codes)`. Add failing cases for both contradictory combinations alongside the existing strict DTO tests.

## Minor

### M1. The purity test never exercises its exploding fake

- **Location:** `backend/tests/unit/test_assignment_policy.py:223-236`
- **Evidence:** The test creates `_provider = ExplodingProvider()` at line 229 but never passes or attaches it to `AssignmentPolicy.assign`; the variable is unused. The test only demonstrates the normal assignment result.
- **Impact:** This does not supply the requested proof that the policy never touches provider/model methods, so a future dependency on one would not be caught by this test.
- **Fix:** Either remove the dead fake and rely on the policy's intentionally provider-free API, or introduce the fake through the actual dependency seam that a future implementation could invoke, then assert the assignment still completes without attribute access.

## Spec-compliance checks that passed

- Capability levels and the GLM/DeepSeek matrix exactly match the Task 2 brief.
- Candidate filtering excludes unavailable/unsupported providers and logical `mock`; scoring and `deepseek`, then `glm` fallback ordering are deterministic.
- Provider-unavailable/capability-mismatch failures expose only the validated subtask key and a controlled code.
- Effective tools are the model-requested ordered intersection of registered and authorized tools, with filtered tools recorded through `POLICY_TOOL_FILTERED`.
- `assign` does not mutate the input document; it performs no provider invocation.
- Assignment output reasons and correction codes are otherwise restricted to backend enums/templates.

## Verification

- `PYTHONDONTWRITEBYTECODE=1 .\\.venv\\Scripts\\python.exe -m pytest -p no:cacheprovider backend/tests/unit/test_conversation_decomposition.py backend/tests/unit/test_assignment_policy.py -q`
  - **32 passed**, with one pre-existing Starlette/httpx deprecation warning.
- `git diff --check d4cc3462 4e2536fc` passed.
- Focused manual DTO/registry probes reproduced I1 and I2 without modifying repository files.
