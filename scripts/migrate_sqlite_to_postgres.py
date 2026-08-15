"""Import the legacy SQLite ledger into the current SQLAlchemy schema.

The importer deliberately reads the legacy schema by reflection.  The source
database predates the team schema, while the target must already have the
current migrations applied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, func, select
from sqlalchemy.engine import Connection

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from secagent.db import Base, make_engine  # noqa: E402
import secagent.db_models  # noqa: E402,F401


LEGACY_TABLES = (
    "tasks",
    "task_steps",
    "model_calls",
    "tool_calls",
    "evidences",
    "approvals",
    "reports",
)
PRIMARY_KEYS = {table_name: "id" for table_name in LEGACY_TABLES}


@dataclass(frozen=True)
class TableSummary:
    source_count: int
    target_count: int
    inserted: int
    skipped: int
    evidence_hash_mismatches: int = 0


@dataclass(frozen=True)
class ImportSummary:
    mode: str
    tables: dict[str, TableSummary]
    evidence_hash_mismatches: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MigrationValidationError(RuntimeError):
    """A source or target invariant prevents a safe import."""

    def __init__(
        self, message: str, summary: ImportSummary | None = None
    ) -> None:
        super().__init__(message)
        self.summary = summary


@dataclass(frozen=True)
class LegacyRecord:
    table_name: str
    values: dict[str, Any]

    @property
    def primary_key(self) -> Any:
        return self.values[PRIMARY_KEYS[self.table_name]]


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    return row[key] if key in row else default


def _parse_datetime(value: Any, *, required: bool = True) -> datetime | None:
    if value is None:
        if required:
            raise MigrationValidationError("legacy timestamp is missing")
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationValidationError("legacy timestamp is invalid") from exc
    return parsed


def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _json_text(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _bool(value: Any, default: bool = False) -> bool:
    return default if value is None else bool(value)


def _int(value: Any, default: int) -> int:
    return default if value is None else int(value)


def _evidence_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _legacy_records(
    source_url: str, mode: str
) -> tuple[dict[str, list[LegacyRecord]], int]:
    engine = make_engine(source_url)
    source_metadata = MetaData()
    records: dict[str, list[LegacyRecord]] = {name: [] for name in LEGACY_TABLES}
    hash_mismatches = 0
    try:
        source_metadata.reflect(bind=engine)
        available = set(source_metadata.tables)
        with engine.connect() as connection:
            for table_name in LEGACY_TABLES:
                if table_name not in available:
                    continue
                table = source_metadata.tables[table_name]
                for row in connection.execute(table.select()).mappings():
                    values = _map_legacy_row(table_name, row)
                    if table_name == "evidences":
                        supplied = _row_value(row, "sha256")
                        if supplied:
                            if str(supplied).lower() != values["sha256"]:
                                hash_mismatches += 1
                            values["sha256"] = str(supplied).lower()
                    records[table_name].append(LegacyRecord(table_name, values))
    finally:
        engine.dispose()
    if hash_mismatches:
        raise MigrationValidationError(
            "evidence hash validation failed",
            _source_failure_summary(records, hash_mismatches, mode),
        )
    _validate_source_relationships(records)
    return records, hash_mismatches


def _map_legacy_row(table_name: str, row: Any) -> dict[str, Any]:
    created_at = _parse_datetime(_row_value(row, "created_at"))
    if table_name == "tasks":
        return {
            "id": _text(_row_value(row, "id")),
            "owner_id": None,
            "goal": _text(_row_value(row, "goal")),
            "authorization_scope": _text(_row_value(row, "authorization_scope")),
            "route_mode": _text(_row_value(row, "route_mode")),
            "preferred_model": _row_value(row, "preferred_model"),
            "scene_hint": _row_value(row, "scene_hint"),
            "target_url": _row_value(row, "target_url"),
            "scene": _row_value(row, "scene"),
            "status": _text(_row_value(row, "status"), "created"),
            "status_version": _int(_row_value(row, "status_version"), 0),
            "current_step_index": _int(_row_value(row, "current_step_index"), 0),
            "max_model_calls": _int(_row_value(row, "max_model_calls"), 8),
            "max_input_tokens": _int(_row_value(row, "max_input_tokens"), 120_000),
            "max_output_tokens": _int(_row_value(row, "max_output_tokens"), 24_000),
            "max_steps": _int(_row_value(row, "max_steps"), 20),
            "budget_deadline_at": _parse_datetime(
                _row_value(row, "budget_deadline_at"), required=False
            ),
            "orchestration_json": _json_text(
                _row_value(row, "orchestration_json"), "{}"
            ),
            "is_demo": _bool(_row_value(row, "is_demo")),
            "created_at": created_at,
            "updated_at": _parse_datetime(
                _row_value(row, "updated_at"), required=False
            )
            or created_at,
        }
    if table_name == "task_steps":
        return {
            "id": _text(_row_value(row, "id")),
            "task_id": _text(_row_value(row, "task_id")),
            "step_index": _int(_row_value(row, "step_index"), 0),
            "idempotency_key": _row_value(row, "idempotency_key"),
            "attempt": _int(_row_value(row, "attempt"), 1),
            "name": _text(_row_value(row, "name")),
            "purpose": _text(_row_value(row, "purpose")),
            "tool_name": _text(_row_value(row, "tool_name")),
            "params_json": _json_text(_row_value(row, "params_json"), "{}"),
            "result_json": _row_value(row, "result_json"),
            "risk_level": _text(_row_value(row, "risk_level")),
            "need_human_confirm": _bool(_row_value(row, "need_human_confirm")),
            "status": _text(_row_value(row, "status"), "pending"),
            "model_provider": _row_value(row, "model_provider"),
            "route_reason": _row_value(row, "route_reason"),
            "created_at": created_at,
            "updated_at": _parse_datetime(
                _row_value(row, "updated_at"), required=False
            )
            or created_at,
        }
    if table_name == "model_calls":
        return {
            "id": _text(_row_value(row, "id")),
            "task_id": _text(_row_value(row, "task_id")),
            "attempt": _int(_row_value(row, "attempt"), 1),
            "provider": _text(_row_value(row, "provider")),
            "model": _text(_row_value(row, "model"), "unknown"),
            "stage": _text(_row_value(row, "stage")),
            "route_reason": _text(_row_value(row, "route_reason")),
            "request_id": _row_value(row, "request_id"),
            "input_summary": _text(_row_value(row, "input_summary")),
            "finish_reason": _row_value(row, "finish_reason"),
            "prompt_tokens": _int(_row_value(row, "prompt_tokens"), 0),
            "completion_tokens": _int(_row_value(row, "completion_tokens"), 0),
            "latency_ms": _int(_row_value(row, "latency_ms"), 0),
            "retry_count": _int(_row_value(row, "retry_count"), 0),
            "status": _text(_row_value(row, "status"), "completed"),
            "error_code": _row_value(row, "error_code"),
            "is_demo": _bool(_row_value(row, "is_demo")),
            "created_at": created_at,
        }
    if table_name == "tool_calls":
        return {
            "id": _text(_row_value(row, "id")),
            "task_id": _text(_row_value(row, "task_id")),
            "step_id": _row_value(row, "step_id"),
            "tool_name": _text(_row_value(row, "tool_name")),
            "params_json": _json_text(_row_value(row, "params_json"), "{}"),
            "result_json": _json_text(_row_value(row, "result_json"), "{}"),
            "status": _text(_row_value(row, "status"), "completed"),
            "duration_ms": _int(_row_value(row, "duration_ms"), 0),
            "attempt": _int(_row_value(row, "attempt"), 1),
            "error_code": _row_value(row, "error_code"),
            "created_at": created_at,
        }
    if table_name == "evidences":
        content = _text(_row_value(row, "content"))
        return {
            "id": _text(_row_value(row, "id")),
            "task_id": _text(_row_value(row, "task_id")),
            "tool_call_id": _row_value(row, "tool_call_id"),
            "evidence_type": _text(_row_value(row, "evidence_type")),
            "source": _text(_row_value(row, "source")),
            "content": content,
            "sha256": _evidence_digest(content),
            "confidence": float(_row_value(row, "confidence")),
            "file_ref": _row_value(row, "file_ref"),
            "metadata_json": _json_text(_row_value(row, "metadata_json"), "{}"),
            "created_at": created_at,
        }
    if table_name == "approvals":
        return {
            "id": _text(_row_value(row, "id")),
            "task_id": _text(_row_value(row, "task_id")),
            "step_id": _text(_row_value(row, "step_id")),
            "tool_name": _text(_row_value(row, "tool_name")),
            "risk_level": _text(_row_value(row, "risk_level")),
            "params_summary": _text(_row_value(row, "params_summary")),
            "status": _text(_row_value(row, "status"), "pending"),
            "reason": _row_value(row, "reason"),
            "decided_by": _row_value(row, "decided_by"),
            "created_at": created_at,
            "decided_at": _parse_datetime(
                _row_value(row, "decided_at"), required=False
            ),
            "expires_at": _parse_datetime(
                _row_value(row, "expires_at"), required=False
            ),
        }
    if table_name == "reports":
        return {
            "id": _text(_row_value(row, "id")),
            "task_id": _text(_row_value(row, "task_id")),
            "version": _int(_row_value(row, "version"), 1),
            "content": _text(_row_value(row, "content")),
            "evidence_ids_json": _json_text(
                _row_value(row, "evidence_ids_json"), "[]"
            ),
            "is_demo": _bool(_row_value(row, "is_demo")),
            "created_at": created_at,
        }
    raise MigrationValidationError("unsupported legacy table")


def _validate_source_relationships(records: dict[str, list[LegacyRecord]]) -> None:
    task_ids = {row.primary_key for row in records["tasks"]}
    step_by_id = {row.primary_key: row for row in records["task_steps"]}
    tool_by_id = {row.primary_key: row for row in records["tool_calls"]}
    for table_name in ("task_steps", "model_calls", "tool_calls", "evidences", "approvals", "reports"):
        for row in records[table_name]:
            if row.values["task_id"] not in task_ids:
                raise MigrationValidationError("legacy relationship validation failed")
    for row in records["tool_calls"]:
        step_id = row.values["step_id"]
        if step_id is not None:
            step = step_by_id.get(step_id)
            if step is None or step.values["task_id"] != row.values["task_id"]:
                raise MigrationValidationError("legacy relationship validation failed")
    for row in records["evidences"]:
        tool_call_id = row.values["tool_call_id"]
        if tool_call_id is not None:
            tool_call = tool_by_id.get(tool_call_id)
            if tool_call is None or tool_call.values["task_id"] != row.values["task_id"]:
                raise MigrationValidationError("legacy relationship validation failed")
    for row in records["approvals"]:
        step = step_by_id.get(row.values["step_id"])
        if step is None or step.values["task_id"] != row.values["task_id"]:
            raise MigrationValidationError("legacy relationship validation failed")


def _source_failure_summary(
    records: dict[str, list[LegacyRecord]], hash_mismatches: int, mode: str
) -> ImportSummary:
    return ImportSummary(
        mode=mode,
        tables={
            name: TableSummary(len(rows), 0, 0, 0, hash_mismatches if name == "evidences" else 0)
            for name, rows in records.items()
        },
        evidence_hash_mismatches=hash_mismatches,
    )


def _target_row(
    connection: Connection, table: Table, primary_key: Any
) -> Any | None:
    return connection.execute(
        select(table).where(table.c[PRIMARY_KEYS[table.name]] == primary_key)
    ).mappings().first()


def _normalized(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value


def _rows_match(table: Table, existing: Any, expected: dict[str, Any]) -> bool:
    return all(
        _normalized(existing[column.name]) == _normalized(value)
        for column in table.columns
        if column.name in expected
        for value in [expected[column.name]]
    )


def _validate_target_relationships(
    connection: Connection,
    tables: dict[str, Table],
    records: dict[str, list[LegacyRecord]],
    allow_pending: bool,
) -> None:
    task_ids = {row.primary_key for row in records["tasks"]}
    step_rows = {
        row.primary_key: row
        for row in records["task_steps"]
    }
    tool_rows = {
        row.primary_key: row
        for row in records["tool_calls"]
    }
    for table_name in ("task_steps", "model_calls", "tool_calls", "evidences", "approvals", "reports"):
        for row in records[table_name]:
            if (
                not allow_pending
                and _target_row(connection, tables["tasks"], row.values["task_id"]) is None
            ):
                raise MigrationValidationError("target relationship validation failed")
    for row in records["tool_calls"]:
        step_id = row.values["step_id"]
        if step_id is not None:
            step = step_rows.get(step_id)
            target_step = _target_row(connection, tables["task_steps"], step_id)
            if (
                step is None
                or step.values["task_id"] != row.values["task_id"]
                or (
                    not allow_pending
                    and (
                        target_step is None
                        or target_step["task_id"] != row.values["task_id"]
                    )
                )
            ):
                raise MigrationValidationError("target relationship validation failed")
    for row in records["evidences"]:
        tool_call_id = row.values["tool_call_id"]
        if tool_call_id is not None:
            tool = tool_rows.get(tool_call_id)
            target_tool = _target_row(connection, tables["tool_calls"], tool_call_id)
            if (
                tool is None
                or tool.values["task_id"] != row.values["task_id"]
                or (
                    not allow_pending
                    and (
                        target_tool is None
                        or target_tool["task_id"] != row.values["task_id"]
                    )
                )
            ):
                raise MigrationValidationError("target relationship validation failed")
    for row in records["approvals"]:
        step = step_rows.get(row.values["step_id"])
        target_step = _target_row(connection, tables["task_steps"], row.values["step_id"])
        if (
            step is None
            or step.values["task_id"] != row.values["task_id"]
            or (
                not allow_pending
                and (
                    target_step is None
                    or target_step["task_id"] != row.values["task_id"]
                )
            )
        ):
            raise MigrationValidationError("target relationship validation failed")
    if len(task_ids) != len(records["tasks"]):
        raise MigrationValidationError("legacy primary key validation failed")


def migrate(
    source_url: str, target_url: str, owner_username: str, apply: bool
) -> ImportSummary:
    """Import legacy rows, or validate and report what an apply would do."""
    mode = "apply" if apply else "dry-run"
    records, source_hash_mismatches = _legacy_records(source_url, mode)
    target_engine = make_engine(target_url)
    summaries: dict[str, TableSummary] = {}
    inserted_counts = {name: 0 for name in LEGACY_TABLES}
    skipped_counts = {name: 0 for name in LEGACY_TABLES}
    try:
        target_tables = {
            name: Base.metadata.tables[name]
            for name in LEGACY_TABLES
        }
        target_users = Base.metadata.tables["users"]
        connection = target_engine.connect()
        transaction = connection.begin()
        try:
            owner = connection.execute(
                select(target_users.c.id).where(
                    target_users.c.username == owner_username
                )
            ).scalar_one_or_none()
            if owner is None:
                raise MigrationValidationError("migration owner was not found")
            for row in records["tasks"]:
                row.values["owner_id"] = owner

            for table_name in LEGACY_TABLES:
                table = target_tables[table_name]
                for record in records[table_name]:
                    existing = _target_row(connection, table, record.primary_key)
                    if existing is not None:
                        if not _rows_match(table, existing, record.values):
                            hash_mismatches = (
                                1
                                if table_name == "evidences"
                                and existing.get("sha256") != record.values["sha256"]
                                else 0
                            )
                            if hash_mismatches:
                                source_hash_mismatches += hash_mismatches
                            raise MigrationValidationError(
                                "target row validation failed",
                                ImportSummary(
                                    mode=mode,
                                    tables={},
                                    evidence_hash_mismatches=source_hash_mismatches,
                                ),
                            )
                        skipped_counts[table_name] += 1
                    elif apply:
                        connection.execute(table.insert().values(**record.values))
                        inserted_counts[table_name] += 1

            _validate_target_relationships(
                connection, target_tables, records, allow_pending=not apply
            )
            for table_name in LEGACY_TABLES:
                table = target_tables[table_name]
                target_count = int(
                    connection.execute(select(func.count()).select_from(table)).scalar_one()
                )
                migrated_count = int(
                    connection.execute(
                        select(func.count())
                        .select_from(table)
                        .where(
                            table.c[PRIMARY_KEYS[table_name]].in_(
                                [row.primary_key for row in records[table_name]]
                            )
                        )
                    ).scalar_one()
                ) if records[table_name] else 0
                projected_count = migrated_count
                if not apply:
                    projected_count += len(records[table_name]) - skipped_counts[table_name]
                if projected_count != len(records[table_name]):
                    raise MigrationValidationError("target count validation failed")
                summaries[table_name] = TableSummary(
                    source_count=len(records[table_name]),
                    target_count=target_count,
                    inserted=inserted_counts[table_name],
                    skipped=skipped_counts[table_name],
                    evidence_hash_mismatches=0,
                )
            if apply:
                transaction.commit()
            else:
                transaction.rollback()
        except Exception:
            if transaction.is_active:
                transaction.rollback()
            raise
        finally:
            connection.close()
    finally:
        target_engine.dispose()
    return ImportSummary(
        mode=mode,
        tables=summaries,
        evidence_hash_mismatches=source_hash_mismatches,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import legacy SQLite evidence into PostgreSQL")
    parser.add_argument("--source", required=True, help="legacy SQLite SQLAlchemy URL")
    parser.add_argument("--target", required=True, help="target SQLAlchemy URL")
    parser.add_argument("--owner-username", required=True)
    parser.add_argument("--apply", action="store_true", help="commit the import")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = migrate(
            args.source,
            args.target,
            args.owner_username,
            apply=args.apply,
        )
    except MigrationValidationError as exc:
        payload = exc.summary.to_dict() if exc.summary else {
            "mode": "apply" if args.apply else "dry-run",
            "error": "migration validation failed",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "error": "migration failed",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
