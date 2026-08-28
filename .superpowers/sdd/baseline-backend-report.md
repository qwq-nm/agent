# Baseline backend report-contract stabilization

## Scope and change

- Updated `backend/secagent/providers/mock.py` only in the `ReportSections` branch.
- Added focused coverage in `backend/tests/unit/test_mock_provider.py`.
- `ReportSections` itself, workers, database configuration, and integration expectations were not changed.

The mock now converts each structured finding to the deterministic narrative
`证据 <id>：<content>`, while continuing to return the original evidence IDs
separately.  This works from the JSON request payload and does not mutate it.

## TDD evidence

### RED

Command:

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/unit/test_mock_provider.py -q
```

Result before the production change: `1 failed, 1 passed, 1 warning in 0.34s`.
The focused structured-evidence assertion failed exactly because
`response.data["findings"]` was `list[dict]`, rather than the expected
traceable narrative `list[str]`.

### GREEN

The same command after the smallest mock-provider change passed:
`2 passed, 1 warning in 0.03s`.  A fresh final run also passed:
`2 passed, 1 warning in 0.04s` (`FOCUSED_TEST_EXIT=0`).

The tests prove that structured evidence becomes deterministic strings that
include both ID and content, evidence IDs are preserved, output validates via
`ReportSections`, and empty findings remain valid.

## Required verification

### Six affected integration files

Command:

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/integration/test_fixed_model_stages.py backend/tests/integration/test_mock_agent_loop.py backend/tests/integration/test_web_scene.py backend/tests/integration/test_log_scene.py backend/tests/integration/test_source_scene.py backend/tests/integration/test_web_agent_replanning.py -q
```

Result: `14 passed, 3 failed, 1 warning in 13.60s`.

The three failures are an independently pre-existing Mock Critic baseline:

- Web scene: after `url_guard` and `http_fetch`, the mock Critic reports
  `should_report=True`, so the runner does not execute `form_extract`; the
  report lacks `action=/search`.
- Log scene: the report includes only uploaded `raw_line` evidence and lacks
  `WEB-SCAN-002`.
- Source scene: the report includes `PY-CMD-001` but lacks `CFG-DEBUG-001`.

Root-cause tracing showed `MockProvider` sets `should_report` from
`bool(evidence_count)`, and the runner exits when `critic.should_report or
critic.is_complete`.  For the log case, `Critic.normalize_decision` identifies
a required gap but preserves that mock `should_report=True`; for web and
source, the missing observations are only recommended by the Critic.  This
behavior predates this slice (history includes it before the current change),
and modifying it was explicitly declined to keep the brief scope.

### Complete backend suite

Command (run twice because the desktop runner detached):

```powershell
.venv\Scripts\python.exe -m pytest -q --tb=short
```

The desktop execution detached from its command wrapper.  The same second
pytest PID (`54720`) was polled until it exited, but the wrapper returned only
progress dots and did not provide the pytest summary or exit code.  Therefore
the final complete-suite status was not captured and is intentionally not
claimed as passing.  The main agent will independently re-run it after this
commit.

### Diff check and self-review

`git diff --check` completed with `DIFF_CHECK_EXIT=0` (only Git's existing
LF-to-CRLF warning was emitted).  Manual self-review confirmed the production
change is limited to narrative conversion; no caller data is changed and the
separate `evidence_ids` contract is retained.

## Commit

Focused code-and-test commit:
`4f29d0cd50eeefb862bed83f9f11c88e559d42a3`

## Status

`DONE_WITH_CONCERNS`: the ReportSections type-contract mismatch is fixed and
covered by RED/GREEN evidence.  The three unrelated Mock Critic integration
baseline failures and unavailable complete-suite final status remain concerns.

## Follow-up review remediation

### Review findings addressed

The review confirmed that the deterministic mock Critic treated any evidence
as sufficient, and that the mock Planner re-emitted already-successful tools
on replan.  The resulting integration failures did not originate in
`ReportSections`; they prevented the runner from reaching form, attack, and
configuration evidence.

`MockProvider` now applies a deterministic minimum-sufficiency policy only:

- required assessment gaps always block;
- low-risk core recommendations (`response header observation`, `public form
  observation`, `attack pattern evidence`, `secret scanning evidence`, and
  `configuration risk evidence`) block as factual gaps;
- all remaining recommendations are retained as non-blocking `next_focus`;
- explicit demo evidence (`demo_evidence` or the deterministic `demo:` source)
  is sufficient for the built-in demo path; and
- a replan removes `execution_memory.successful_tool_calls`, retaining the
  original allowlist order and the configured step limit.

The Web integration's fixed four-stage assertion was updated with explicit
authorization because the corrected control flow must make a second
`plan`/`critic` pair before it can produce the required form evidence.  The
replacement remains strict: task parsing is first, reporting is last, and all
intermediate calls are ordered `plan`/`critic` pairs.

### Follow-up RED/GREEN evidence

Focused MockProvider tests were extended before the follow-up production
change.  RED command:

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/unit/test_mock_provider.py -q
```

Result: `2 failed, 3 passed, 1 warning in 0.44s`.  The failures showed that
`robots.txt observation` was incorrectly blocking and demo evidence was not
accepted.  After the minimal mock-only implementation, the same test command
was GREEN: `5 passed, 1 warning in 0.07s`.

### Follow-up verification

Focused test command above: `5 passed, 1 warning in 0.07s`.

Required integration command:

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/integration/test_fixed_model_stages.py backend/tests/integration/test_mock_agent_loop.py backend/tests/integration/test_web_scene.py backend/tests/integration/test_log_scene.py backend/tests/integration/test_source_scene.py backend/tests/integration/test_web_agent_replanning.py -q
```

Result: `17 passed, 1 warning in 13.67s`.

The complete suite was started exactly once with
`.venv\Scripts\python.exe -m pytest -q --tb=short`, then polled using the
same PID (`49008`) until it exited.  Final captured result: `5 failed, 294
passed, 1 warning in 113.99s (0:01:53)`; pytest exit code was `1`.  The five
unrelated baseline failures are three tests in
`backend/tests/integration/test_model_failure_state.py` and two migration-drift
tests in `backend/tests/unit/test_schema.py`.

`git diff --check` passed before commit (only existing LF-to-CRLF warnings).
Self-review confirmed the follow-up production behavior remains confined to
`MockProvider`; the only integration-test update narrows stage-order validation
without weakening the fixed provider mapping or report-content checks.

### Follow-up commit

`2fe26c725d2a06e597982afd3f5a5cd4040bff7f`

### Full-suite failure isolation

The complete suite result is `294/299` tests passed.  The five failed nodeids
were reproduced separately without code changes:

- `backend/tests/integration/test_model_failure_state.py::test_missing_fixed_provider_persists_safe_error_and_counts_call`
  expected `ProviderUnavailable`, but no exception was raised.  Its configured
  router omits the fixed `glm` provider, so the expected failure is before any
  MockProvider call; this slice did not alter router stage mapping.
- `backend/tests/integration/test_model_failure_state.py::test_persisted_deadline_fails_pending_approval_step_atomically`
  observed task status `waiting_human` rather than `failed`.  This is in the
  `http_fetch` approval/deadline path before Critic review or re-planning.
- `backend/tests/integration/test_model_failure_state.py::test_report_rejects_foreign_evidence_id_and_repository_persists_exact_ids`
  expected a foreign-evidence citation `ValueError`, but none was raised.  It
  uses `ForgedCitationRouter`, `Reporter`, and repository code directly; it
  does not instantiate MockProvider.
- `backend/tests/unit/test_schema.py::test_task8_migration_round_trips_from_task6_and_has_no_drift`
- `backend/tests/unit/test_schema.py::test_model_call_attempt_migration_round_trips_from_task8_v3`

Both schema tests fail Alembic `check` with return code `4294967295`.  Its
stderr reports `Detected NOT NULL on column 'provider_routes.created_at'` and
`Detected NOT NULL on column 'provider_routes.updated_at'`, followed by two
`modify_nullable` upgrade operations from nullable `True` to `False`.  This
slice has no database model or migration changes.

Final status: `DONE_WITH_CONCERNS`.  The MockProvider/report-contract work and
all 17 requested focused integration cases are green; the independently
reproduced full-suite baseline failures above remain.

## Final baseline remediation and acceptance

The five isolated failures were subsequently repaired without changing their
expected contracts:

- `28da8fea` adds a forward-only corrective migration for the two
  `provider_routes` timestamp nullability drifts, including legacy NULL
  backfill and SQLite upgrade/downgrade coverage.
- `168c7656` restores fixed-stage provider routing, preserves non-null persisted
  deadlines across approval jobs, strictly rejects foreign/unknown/duplicate
  model evidence citations, and preserves the budget stop code in partial
  reports. The focused safety suite passed `31 passed, 1 warning`.

Fresh complete-suite command from `168c7656`:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest backend\tests -q -p no:cacheprovider
```

Final captured result: `304 passed, 1 warning in 144.74s`; exit code `0`.
The only warning is the pre-existing third-party
`StarletteDeprecationWarning` emitted by FastAPI's TestClient import and was
not filtered. Final status: `DONE` pending independent code review.
