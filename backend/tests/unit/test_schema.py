import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import inspect

from secagent import db_models  # noqa: F401 -- registers ORM tables for this contract
from secagent.db import Base, make_engine
from secagent.domain import TaskStatus, UserRole


REQUIRED_COLUMNS = {
    "tasks": {
        "owner_id",
        "status_version",
        "current_step_index",
        "max_model_calls",
        "max_input_tokens",
        "max_output_tokens",
        "updated_at",
    },
    "job_runs": {
        "task_id",
        "broker_id",
        "command_id",
        "worker_id",
        "attempt",
        "status",
        "lease_expires_at",
        "heartbeat_at",
        "started_at",
        "finished_at",
    },
    "task_steps": {"idempotency_key", "attempt", "result_json"},
    "model_calls": {
        "request_id",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "retry_count",
        "status",
        "error_code",
    },
    "tool_calls": {"duration_ms", "attempt", "error_code"},
    "evidences": {"sha256", "file_ref"},
    "approvals": {"decided_by", "expires_at"},
    "reports": {"version", "evidence_ids_json"},
    "audit_events": {
        "actor_id",
        "action",
        "resource_type",
        "resource_id",
        "outcome",
        "ip_address",
        "details_json",
        "created_at",
    },
}


def _schema_inspector(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    Base.metadata.create_all(engine)
    return inspect(engine)


def test_team_schema_has_required_tables(tmp_path):
    tables = set(_schema_inspector(tmp_path).get_table_names())

    assert {
        "users",
        "refresh_sessions",
        "tasks",
        "job_runs",
        "task_events",
        "task_steps",
        "model_calls",
        "tool_calls",
        "evidences",
        "approvals",
        "reports",
        "audit_events",
    } <= tables


def test_team_schema_has_required_production_columns(tmp_path):
    inspector = _schema_inspector(tmp_path)

    for table, expected in REQUIRED_COLUMNS.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        assert expected <= actual, table


def test_team_schema_enforces_ownership_and_task_lifecycle_foreign_keys(tmp_path):
    inspector = _schema_inspector(tmp_path)

    task_owner = inspector.get_foreign_keys("tasks")
    assert any(
        fk["constrained_columns"] == ["owner_id"]
        and fk["referred_table"] == "users"
        and fk["options"].get("ondelete") == "RESTRICT"
        for fk in task_owner
    )

    for table in {
        "job_runs",
        "task_events",
        "task_steps",
        "model_calls",
        "tool_calls",
        "evidences",
        "approvals",
        "reports",
    }:
        assert any(
            fk["constrained_columns"] == ["task_id"]
            and fk["referred_table"] == "tasks"
            and fk["options"].get("ondelete") == "CASCADE"
            for fk in inspector.get_foreign_keys(table)
        ), table

    audit_actor = inspector.get_foreign_keys("audit_events")
    assert any(
        fk["constrained_columns"] == ["actor_id"]
        and fk["referred_table"] == "users"
        and fk["options"].get("ondelete") == "RESTRICT"
        for fk in audit_actor
    )


def test_team_schema_has_required_unique_contracts(tmp_path):
    inspector = _schema_inspector(tmp_path)

    def unique_column_sets(table):
        constraints = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(table)
        }
        indexes = {
            tuple(item["column_names"])
            for item in inspector.get_indexes(table)
            if item["unique"]
        }
        return constraints | indexes

    assert ("username",) in unique_column_sets("users")
    assert ("command_id",) in unique_column_sets("job_runs")
    assert ("task_id", "step_index") in unique_column_sets("task_steps")
    assert ("task_id", "idempotency_key") in unique_column_sets("task_steps")
    assert ("task_id", "sha256", "source") in unique_column_sets("evidences")
    assert ("task_id", "version") in unique_column_sets("reports")


def test_team_schema_domain_enums_include_queue_and_user_roles():
    assert TaskStatus.QUEUED.value == "queued"
    assert TaskStatus.CANCELLED.value == "cancelled"
    assert {role.value for role in UserRole} == {"admin", "analyst"}


def test_alembic_accepts_percent_encoded_database_url():
    repository_root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = (
        "postgresql+psycopg://secagent:p%40ss@postgres/secagent"
    )

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
