# Baseline safety-contract repair task

Work in `F:\codex\agent\.worktrees\conversational-multi-agent`. This is a focused baseline repair; do not begin conversation-feature work.

## Scope

Repair three independently reproduced production regressions without weakening their existing tests or changing public semantics.

1. Fixed-stage provider routing
   - `mock` mode still uses only `mock`.
   - `auto` and `live` must require `FIXED_PROVIDER[stage]`; a missing fixed provider raises `ProviderUnavailable` with that provider and `AUTH` code.
   - Never fall back to the other live provider or to mock.
   - Keep `preferred` unable to override stage ownership.

2. Persistent task deadline
   - `TaskRepository.budget_state()` initializes `budget_deadline_at` only when it is `None`.
   - A non-null deadline is durable even when it precedes the current job's `started_at`; approval/re-entry must not create a new budget window.
   - Do not remove job fencing or transaction release behavior.
   - If a future explicit retry/new-attempt policy needs a new deadline, it must reset it at that explicit lifecycle transition, not inside every worker start; that policy is outside this task.

3. Evidence citation integrity
   - If model `ReportSections.evidence_ids` is empty, preserve the existing default of citing all evidence belonging to the current task.
   - If it is non-empty, reject duplicate IDs and any ID not in the current task snapshot using `ValueError("invalid evidence citation for current task")` (or a message still matching `evidence citation`).
   - Do not silently filter invalid IDs.
   - Preserve repository-level validation as defense in depth.

## TDD requirements

Before production edits, add/strengthen the smallest tests and run them to demonstrate RED:

- router unit: `auto` with only DeepSeek configured, `TASK_PARSE` raises missing `glm` with `AUTH` and no completion by DeepSeek;
- repository/integration: a non-null past persisted deadline is returned unchanged; the existing approval continuation test must end atomically with task and active pending step failed and one budget event;
- reporter: explicit foreign/unknown citation and duplicate citation are rejected; empty citation list still defaults to all current-task evidence.

Then make minimal production changes and run GREEN:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_model_router.py backend/tests/integration/test_model_failure_state.py -q -p no:cacheprovider
```

Also run any narrower RED/GREEN selections first. Do not hide warnings, edit unrelated code, or rewrite tests to accept fallback/deadline extension/filtering.

## Delivery

- Use `apply_patch` for edits.
- Write the implementation and verification account to ignored `.superpowers/sdd/baseline-safety-contracts-report.md`.
- Commit only relevant source/tests with a focused commit message.
- Return the commit hash, exact RED/GREEN commands and outcomes, changed files, and any residual risk.
