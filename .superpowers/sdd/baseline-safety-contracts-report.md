# Baseline safety-contract repair report

## Scope and root causes

Implemented only the three requested baseline repairs.

1. Fixed-stage routing: `ModelRouter.provider_for()` caught a missing fixed provider and selected the other configured live provider. It now requires the fixed provider directly; `preferred` remains ignored and mock remains exclusive to mock mode.
2. Persistent deadline: `TaskRepository.budget_state()` treated every deadline at or before the current job start as stale and opened a new budget window. It now initializes only a null deadline. Job fencing and transaction-release behavior are unchanged.
3. Citation integrity: `Reporter._compose_report()` silently removed unknown IDs. It now rejects a non-empty citation list containing duplicates or IDs outside the current task snapshot, while an empty list still defaults to every current-task evidence ID. Repository validation remains in place.

The approval-continuation test then reached its existing report assertion and exposed that the fallback report translated away the machine-readable stop code. The deadline fallback error now retains `budget exhausted: <dimension>` alongside the human-readable text; no lifecycle behavior changed.

## RED evidence (production code unchanged)

Environment for every command: `$env:PYTHONDONTWRITEBYTECODE='1'`.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_model_router.py::test_auto_missing_fixed_provider_does_not_call_other_live_provider -q -p no:cacheprovider
```

Result: exit 1; 1 failed. Expected `ProviderUnavailable`, but no exception was raised because DeepSeek completed the GLM-owned stage.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_model_failure_state.py::test_budget_state_preserves_a_non_null_persisted_deadline -q -p no:cacheprovider
```

Result: exit 1; 1 failed. The returned deadline was reset to `now + timeout` instead of preserving the persisted past value.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_model_failure_state.py::test_report_rejects_foreign_evidence_id_and_repository_persists_exact_ids backend/tests/integration/test_model_failure_state.py::test_report_rejects_duplicate_explicit_evidence_citations backend/tests/integration/test_model_failure_state.py::test_report_empty_evidence_citations_default_to_all_current_evidence -q -p no:cacheprovider
```

Result: exit 1; 2 failed and 1 passed. Foreign and duplicate explicit citations were not rejected; empty citations already defaulted to all current evidence.

## GREEN evidence

Narrow routing regression: 1 passed, 1 existing third-party warning.

Narrow deadline regressions (durable deadline plus approval continuation): 2 passed, 1 existing third-party warning.

Narrow citation regressions: 3 passed, 1 existing third-party warning.

Required focused suite:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_model_router.py backend/tests/integration/test_model_failure_state.py -q -p no:cacheprovider
```

Result: exit 0; 31 passed, 1 warning in 10.45s. The warning is the pre-existing `StarletteDeprecationWarning` emitted by FastAPI's TestClient import and was not hidden.

Fresh pre-commit rerun of the same command: exit 0; 31 passed, 1 warning in 8.96s.

`git diff --check` completed with exit 0 (Git only reported the repository's LF-to-CRLF checkout notices).

An optional Ruff invocation could not run because Ruff is not installed in the worktree virtual environment; pytest imported and exercised all changed production modules.

## Changed tracked files

- `backend/secagent/providers/router.py`
- `backend/secagent/repository.py`
- `backend/secagent/agents/reporter.py`
- `backend/secagent/agents/runner.py`
- `backend/tests/unit/test_model_router.py`
- `backend/tests/integration/test_model_failure_state.py`

## Residual risk

- Full backend regression coverage is delegated to the parent baseline pass; this task ran the complete focused suite required by the brief.
- A future explicit retry/new-attempt lifecycle that intentionally resets the deadline still needs its own transition-level policy; no such behavior was added here.
