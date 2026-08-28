# Conversation service/storage fix round 1 report

## Status

`DONE_WITH_CONCERNS`

Fix commit: `2fc0fa0c` (`fix: harden conversation commit and storage boundaries`),
based on parent `5eebdccd`.

The three independent-review findings C1, I1, and M1 are addressed in the
five-file implementation/test diff. No other checkpoint behavior or production
surface was changed. The remaining concern is verification-environment-only:
this resumed agent host could not reach the Docker Desktop named pipe or
`127.0.0.1:55432`, so the live PostgreSQL barrier was not freshly rerun here.
The controller/root host will run it against this commit before scoped
re-review.

## Technical review of the recovered diff

### C1: uncertain COMMIT is the default

- `ConversationService._commit_explicitly_not_applied()` no longer consults
  `Session.in_transaction()`, which is only client-side state and cannot prove
  a server-side COMMIT outcome.
- The only compensatable COMMIT-attempt exception is the narrow typed
  `ConversationCommitNotApplied` signal, documented as controlled evidence
  that COMMIT was not attempted.
- Every other exception from either COMMIT path, including a non-invalidated
  SQLAlchemy driver/DBAPI error while `in_transaction()` is still true,
  invalidates the writer Session, raises
  `ConversationCommitOutcomeUnknown`, and preserves the published workspace
  for reconciliation.
- The existing explicit-no-commit case now raises the typed signal and retains
  its rollback/filesystem-compensation assertion. Separate tests cover a real
  successful commit followed by an exception, an invalidated driver error,
  and the new non-invalidated driver error.

### I1: Unicode control rejection is consistent

- The shared safe-relative-path validator now rejects characters whose Unicode
  category begins with `C`. This includes the required `Cc` controls and is
  deliberately the same policy already used for top-level filenames.
- ZIP paths are NFC-normalized and then validated through that shared path
  type, so every segment receives the same rule before collision tracking or
  extraction.
- U+0085 regression cases cover both top-level filenames and ZIP members; the
  tests assert fail-closed behavior and removal of staging residue.

### M1: completed batches are not strongly retained

- `StagedAttachmentBatch` is weak-referenceable and the issued-capability
  registry is now a `WeakKeyDictionary`, so completed/cleaned batches and their
  attachment tuples are collectible when callers release them.
- While a batch remains alive, repeated cleanup remains idempotent because its
  weak registry entry remains present with the consumed `None` state.
- Capability validation remains identity-based and unforgeable by ordinary
  construction: the batch must carry the service's private owner object, be an
  extant key in that service's registry, and have state equal to the registry
  value.

The diff is confined to:

- `backend/secagent/conversation_domain.py`
- `backend/secagent/services/conversation_service.py`
- `backend/secagent/services/conversation_storage.py`
- `backend/tests/unit/test_conversation_service.py`
- `backend/tests/unit/test_conversation_storage.py`

## Inherited TDD evidence

The interrupted implementation agent reported that each new regression was
observed failing against the pre-fix behavior and then passing after its
corresponding implementation change:

- C1 RED: a non-invalidated driver/DBAPI COMMIT error with live client-side
  transaction state was incorrectly treated as an explicit non-commit.
- I1 RED: U+0085 was accepted at the filename/ZIP boundary.
- M1 RED: completed batches remained strongly reachable from the registry.

That agent then reported `29 passed` for the combined C1/I1/M1 targeted run and
`105 passed, 3 warnings in 30.48s` for the focused suite with the real
PostgreSQL READ COMMITTED barrier. The raw RED console transcript disappeared
with the interrupted agent tree, so these are explicitly inherited results,
not represented as fresh output from this resumed agent.

## Fresh verification from the resumed agent

All successful pytest commands below used the worktree virtual environment,
`PYTHONDONTWRITEBYTECODE=1`, and `-p no:cacheprovider`.

### Environment discovery

The initial command used the ambient interpreter:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest <six targeted node ids> -p no:cacheprovider -q
```

It did not collect tests and exited 1 with
`ModuleNotFoundError: No module named 'fastapi'`. Repository reports identified
`.venv\Scripts\python.exe` as the intended test interpreter; every subsequent
pytest invocation used it.

### C1/I1/M1 targeted

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; .\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_conversation_service.py::test_explicit_commit_failure_compensates_but_uncertain_commit_keeps_files backend/tests/unit/test_conversation_service.py::test_connection_invalidated_during_commit_is_treated_as_uncertain backend/tests/unit/test_conversation_service.py::test_noninvalidated_driver_commit_error_defaults_to_uncertain backend/tests/unit/test_conversation_storage.py::test_unsafe_client_filenames_fail_closed backend/tests/unit/test_conversation_storage.py::test_zip_unsafe_aliases_and_normalized_collisions_reject_batch backend/tests/unit/test_conversation_storage.py::test_completed_batches_are_collectible_and_cleanup_remains_idempotent -p no:cacheprovider -q
```

Result: `29 passed, 1 warning in 2.24s` (exit 0).

### Focused checkpoint suite on this host

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; Remove-Item Env:SECAGENT_TEST_POSTGRES_URL -ErrorAction SilentlyContinue; .\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_conversation_service_contract.py backend/tests/unit/test_conversation_events_service.py backend/tests/unit/test_conversation_storage.py backend/tests/unit/test_conversation_service.py backend/tests/unit/test_conversation_repository.py backend/tests/unit/test_conversation_domain.py -p no:cacheprovider -q
```

Result: `104 passed, 1 skipped, 3 warnings in 15.87s` (exit 0). The single
skip is `test_concurrent_same_key_postgresql_read_committed_barrier` because the
PostgreSQL URL was intentionally unset after confirming this agent host could
not reach the service.

The required live URL was
`postgresql+psycopg://codex_test:codex_test_pw@127.0.0.1:55432/codex_agent_test`.
On this resumed agent host, Docker inspection failed because
`dockerDesktopLinuxEngine` was unavailable and a direct TCP check of port
55432 returned false. The root/controller host reported a working daemon and
will append its fresh live-PG evidence to the controller ledger rather than
having this report overstate local evidence.

### Full backend suite

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; Remove-Item Env:SECAGENT_TEST_POSTGRES_URL -ErrorAction SilentlyContinue; .\.venv\Scripts\python.exe -m pytest backend/tests -p no:cacheprovider -q
```

Result: `413 passed, 1 skipped, 3 warnings in 136.56s (0:02:16)` (exit 0).
The one skip is the same live PostgreSQL test. The warnings are one existing
Starlette TestClient deprecation and two existing Python 3.12 sqlite datetime
adapter deprecations.

## Final checks

`git diff --check` exited 0 before commit. Final status/diff inspection showed
only the five expected tracked files; the report itself is an allowed ignored
artifact under the repository's `.superpowers/` ignore rule. Commit
`2fc0fa0c` contains 95 insertions and 10 deletions across those five files.

## Controller live-PostgreSQL addendum

The controller restored the hard live-PostgreSQL gate after this agent's host
could not see Docker Desktop. Windows-to-WSL port forwarding was unavailable,
so it created a task-only WSL Python 3.12 environment and connected from WSL to
the task-only PostgreSQL 16 container at `127.0.0.1:55433`.

At committed HEAD `2fc0fa0c`, the isolated
`test_concurrent_same_key_postgresql_read_committed_barrier` result was:
`1 passed, 1 warning in 1.17s` (exit 0). The six-file focused checkpoint suite
with the same live PostgreSQL URL then returned:
`105 passed, 3 warnings in 12.70s` (exit 0). The warnings are the same existing
Starlette and Python 3.12 SQLite datetime-adapter deprecations recorded above.
