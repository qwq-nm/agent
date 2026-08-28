# Provider route timestamp migration report

## Scope

- Fix only the Alembic nullable drift for `provider_routes.created_at` and
  `provider_routes.updated_at`.
- Preserve the ORM and the published `20260820_06` migration unchanged.
- Add a corrective migration immediately after `20260821_07`.

## Root cause

`ProviderRouteRow` declares both timestamp fields as non-nullable, while
`20260820_06_provider_routes.py` created both fields with `nullable=True`.
No later migration reconciled the deployed schema with the ORM metadata, so
Alembic autogenerate/check reported two nullable changes.

## TDD evidence

### RED

Command:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; .\.venv\Scripts\python.exe -m pytest backend\tests\unit\test_schema.py -k provider_route_timestamp_migration_backfills_and_round_trips -vv
```

Result before the corrective migration: `1 failed, 8 deselected, 1 warning`.
The intended assertion failed because `created_at["nullable"]` was `True`.

### GREEN

The same focused command passed after adding the migration:
`1 passed, 8 deselected, 1 warning`.

## Implementation

- Added `20260826_08_provider_route_timestamps.py` with
  `down_revision = "20260821_07"`.
- Upgrade symmetrically backfills missing timestamps with `COALESCE`, then
  makes both fields non-null using `batch_alter_table`.
- Downgrade restores both fields to nullable using `batch_alter_table`.
- The regression test upgrades to `20260821_07`, inserts a legacy row with
  null timestamps, upgrades to head, verifies the backfill and NOT NULL
  contract, downgrades, and verifies nullability plus the existing foreign key
  and index.

## Verification

Command:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; .\.venv\Scripts\python.exe -m pytest backend\tests\unit\test_schema.py -vv
```

Result: `9 passed, 1 warning in 13.89s`.

This test file includes SQLite migration upgrade/downgrade/re-upgrade paths,
`alembic check`, and PostgreSQL offline SQL generation. The warning is the
pre-existing third-party `StarletteDeprecationWarning` from
`fastapi.testclient`; it was not filtered or hidden.

## Risks and unverified items

- No live PostgreSQL server migration was executed; PostgreSQL was verified
  through Alembic offline SQL generation only.
- The corrective migration now owns revision `20260826_08`; any later planned
  migration must chain from it and use a new revision identifier.
