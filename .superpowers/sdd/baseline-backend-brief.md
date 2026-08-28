# Baseline backend report-contract stabilization

## Scope

Repair the pre-existing deterministic mock report contract mismatch. Keep the production `ReportSections.findings: list[str]` contract and make the mock provider honor it. Do not change workers, database configuration, or integration-test expectations.

## Existing RED evidence

At least ten integration failures reach `Reporter.render()` and fail because `MockProvider` returns `payload["findings"]` as `list[dict]`, while `ReportSections` requires `list[str]`. The trace is `worker.py -> task_service.py -> runner.py -> reporter.py: ReportSections.model_validate(response.data)`.

## Required changes

1. Add a focused unit test for `MockProvider.complete()` with a `ReportSections` response schema and structured evidence input. It must prove:
   - returned `data["findings"]` is a deterministic list of human-readable strings, not dictionaries;
   - each finding remains traceable to its evidence ID and content;
   - `data["evidence_ids"]` preserves the input evidence IDs;
   - empty findings remain valid.
2. Verify the new test fails for the established contract mismatch before changing production code.
3. Make the smallest change in `backend/secagent/providers/mock.py` to convert structured evidence findings to safe deterministic narrative strings while keeping evidence IDs separate. Do not mutate the caller payload and do not special-case tests.
4. Keep `ReportSections` unchanged: live providers must still return narrative report sections, while structured evidence remains input material.

## Verification

- Run the new focused unit test and record RED then GREEN evidence.
- Run the six affected integration files together:
  - `backend/tests/integration/test_fixed_model_stages.py`
  - `backend/tests/integration/test_mock_agent_loop.py`
  - `backend/tests/integration/test_web_scene.py`
  - `backend/tests/integration/test_log_scene.py`
  - `backend/tests/integration/test_source_scene.py`
  - `backend/tests/integration/test_web_agent_replanning.py`
- Run the complete backend suite with `-q --tb=short`; if the desktop process detaches, poll the same process until it exits and report the true final result.
- Run `git diff --check`.

## Delivery

Commit the focused change. Write the full report to `.superpowers/sdd/baseline-backend-report.md` with RED/GREEN proof, files changed, commands, exact outcomes, commit hash, and self-review. Return only status, commit, one-line verification summary, and concerns.
