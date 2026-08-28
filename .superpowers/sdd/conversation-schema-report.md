# Conversation schema and DTO checkpoint report

## Scope

Implemented only the first conversational persistence checkpoint:

- strict conversation/message/turn/attachment/event DTOs and canonical JSON helpers;
- five SQLAlchemy tables with named integrity contracts;
- Alembic revision `20260826_09`, chained from `20260826_08`;
- focused validation, metadata, migration, deletion, and cursor tests.

No repository, API, upload I/O, SSE, job, DAG, or frontend behavior was added.

## TDD evidence

The tests were added before production code. Initial focused command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest backend\tests\unit\test_conversation_domain.py backend\tests\unit\test_conversation_schema.py -q -p no:cacheprovider
```

RED result: collection stopped with `ModuleNotFoundError: No module named
'secagent.conversation_domain'`. This proved the new public DTO contract did not
exist before implementation.

After adding DTOs, ORM rows, and the migration, the same focused suite passed:
`13 passed, 1 warning in 3.89s`. The one warning is the existing third-party
`StarletteDeprecationWarning`; it was not hidden.

## Contract coverage

- exact enum values and conservative defaults;
- request/write `extra="forbid"`, non-null patch semantics, trim/length/count
  boundaries, strict integer bounds, canonical hyphenated UUID normalization;
- safe relative POSIX storage paths, lowercase SHA-256, strict JSON object
  keys/values, non-finite-number rejection, deterministic UTF-8 JSON;
- exact columns, nullability, named FKs/checks, indexes, and unique constraints;
- deferred trigger-message protection, successful parent-conversation cascade;
- globally increasing SQLite event cursor after deleting the highest row;
- SQLite `20260826_08 -> head -> 20260826_08 -> head` round trip and
  `alembic check`;
- PostgreSQL offline DDL for all five tables, both named cycle constraints, and
  the deferred FK.

## Full verification

Fresh command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest backend\tests -q -p no:cacheprovider
```

Result: `317 passed, 1 warning in 115.26s`; exit code `0`. The warning is the
same documented third-party TestClient warning.

## Independent-review remediation

DTO and schema reviewers found no database defects, but the DTO audit exposed
three input-hardening gaps plus one coverage gap. Tests reproduced all issues
before the fixes (`5 failed, 7 passed`): empty non-`dict` mappings bypassed
patch non-emptiness, Windows device/ADS/normalization aliases passed path
validation, isolated Unicode surrogates broke the UTF-8-safe JSON promise, and
some request strings/target containers were coercible. The implementation now
rejects those inputs and tests exact maximum acceptance plus every Read DTO.

Fresh post-review verification:

- focused conversation suites: `17 passed, 1 warning in 5.46s`;
- complete backend suite: `321 passed, 1 warning in 140.48s`; exit code `0`.

## Self-review and residual risk

- The ORM and migration use matching types, defaults, names, nullability,
  deferrability, and delete actions; `alembic check` is clean.
- SQLite executes the real migration and cascade scenarios. PostgreSQL was
  validated through offline SQL generation, not against a live server.
- `subtask_id` intentionally remains an opaque indexed string without an FK in
  this checkpoint; the durable DAG revision will own that relationship.
