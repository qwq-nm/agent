# Task 3 Report: Fixed DeepSeek V4 Flash Coordinator Boundary

## Outcome

Implemented the fixed DeepSeek V4 Flash decomposition boundary, bounded and
redacted coordinator context, deterministic assignment integration, explicit
mock decomposition evidence, new execution stages, and aligned defaults. No
persistence, scheduling, event emission, tool execution, failure catching,
HTTP routes, or frontend behavior was added.

## TDD Evidence

### Provider routing and adapter stages

- RED command:
  `python -m pytest -p no:cacheprovider backend/tests/unit/test_model_router.py backend/tests/unit/test_deepseek_provider.py backend/tests/unit/test_glm_provider.py -q`
- RED result: collection failed with 2 errors because
  `ModelStage.DECOMPOSE` did not exist (the same missing contract also blocked
  the DeepSeek new-stage parameterization).
- GREEN result: `67 passed, 1 warning in 0.81s`.

### Coordinator boundary

- RED command:
  `python -m pytest -p no:cacheprovider backend/tests/unit/test_coordinator.py -q`
- RED result: collection failed with
  `ModuleNotFoundError: No module named 'secagent.agents.coordinator'`.
- During GREEN verification, pytest produced two setup errors because a
  64,001-character parameter became a Windows environment-sized test ID. The
  test data and production expectation were unchanged; explicit short test IDs
  fixed the harness issue.
- GREEN result: `23 passed, 1 warning in 0.11s`.

### Explicit deterministic mock decomposition

- RED command:
  `python -m pytest -p no:cacheprovider backend/tests/unit/test_mock_provider.py backend/tests/unit/test_model_router.py -q`
- RED result: `2 failed, 37 passed, 1 warning`; both failures were the expected
  `unsupported mock schema: DecompositionDocument` behavior.
- GREEN command also included coordinator coverage.
- GREEN result: `62 passed, 1 warning in 0.75s`.

### Flash defaults

- RED command:
  `python -m pytest -p no:cacheprovider backend/tests/unit/test_config_secrets.py -q`
- RED result: `3 failed, 12 passed, 1 warning`; application, `.env.example`,
  and both Docker service defaults still used `deepseek-v4-pro`.
- GREEN result: `15 passed, 1 warning in 0.08s`.

### Boundary hardening follow-up

- RED command covered explicit-mock subtask preference and pre-redaction
  context-budget measurement.
- First RED result: `1 failed, 1 passed, 1 warning`; mock subtask execution did
  not yet require a logical preferred provider. After choosing a discriminating
  budget value, the pre-redaction budget test independently failed with
  `DID NOT RAISE ValueError`.
- GREEN result: `2 passed, 1 warning in 0.14s`.

## Verification Evidence

- Focused Task 1/2/3 command:
  `python -m pytest -p no:cacheprovider backend/tests/unit/test_conversation_decomposition.py backend/tests/unit/test_assignment_policy.py backend/tests/unit/test_coordinator.py backend/tests/unit/test_model_router.py backend/tests/unit/test_deepseek_provider.py backend/tests/unit/test_glm_provider.py backend/tests/unit/test_mock_provider.py backend/tests/unit/test_config_secrets.py -q`
- Focused result: `151 passed, 1 warning in 1.45s`.
- Full backend command:
  `python -m pytest -p no:cacheprovider backend/tests`
- Initial full backend result: `603 passed, 1 skipped, 3 warnings in 246.54s`.
- Fresh post-refactor completion verification result:
  `603 passed, 1 skipped, 3 warnings in 245.04s`.
- `git diff --check`: passed. Git emitted only Windows LF-to-CRLF working-copy
  notices; no whitespace errors were reported.

## Files

- Added `backend/secagent/agents/coordinator.py`.
- Added `backend/tests/unit/test_coordinator.py`.
- Updated `backend/secagent/domain.py`.
- Updated `backend/secagent/providers/router.py`.
- Updated `backend/secagent/providers/deepseek.py`.
- Updated `backend/secagent/providers/glm.py`.
- Updated `backend/secagent/providers/mock.py`.
- Updated `backend/secagent/config.py`.
- Updated provider/router/mock/config unit tests and provider test fakes.
- Updated `.env.example` and `docker-compose.yml`.

## Concerns

- No functional blockers or known correctness failures.
- The full suite reports pre-existing Starlette/httpx and Python 3.12 sqlite
  deprecation warnings; these are outside Task 3.
- The repository's Windows checkout reports LF-to-CRLF conversion notices when
  running Git checks; `git diff --check` still passes.
