import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import inspect, text

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
        "max_steps",
        "budget_deadline_at",
        "orchestration_json",
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
        "attempt",
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
    "tool_call_evidences": {"tool_call_id", "evidence_id", "step_attempt"},
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
    "provider_credentials": {
        "provider",
        "encrypted_api_key",
        "key_hint",
        "state",
        "updated_by",
        "created_at",
        "updated_at",
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
        "tool_call_evidences",
        "approvals",
        "reports",
        "audit_events",
        "provider_credentials",
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

    credential_actor = inspector.get_foreign_keys("provider_credentials")
    assert any(
        fk["constrained_columns"] == ["updated_by"]
        and fk["referred_table"] == "users"
        and fk["options"].get("ondelete") == "RESTRICT"
        for fk in credential_actor
    )

    bindings = inspector.get_foreign_keys("tool_call_evidences")
    assert any(
        fk["constrained_columns"] == ["tool_call_id"]
        and fk["referred_table"] == "tool_calls"
        and fk["options"].get("ondelete") == "CASCADE"
        for fk in bindings
    )
    assert any(
        fk["constrained_columns"] == ["evidence_id"]
        and fk["referred_table"] == "evidences"
        and fk["options"].get("ondelete") == "CASCADE"
        for fk in bindings
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


def test_task8_migration_round_trips_from_task6_and_has_no_drift(tmp_path):
    repository_root = Path(__file__).resolve().parents[3]
    database_path = tmp_path / "migration.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"

    def alembic(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert alembic("upgrade", "20260815_02").returncode == 0
    engine = make_engine(environment["DATABASE_URL"])
    task6_columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
    assert "orchestration_json" not in task6_columns

    upgraded = alembic("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    task8_columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
    assert {"max_steps", "budget_deadline_at", "orchestration_json"} <= task8_columns

    downgraded = alembic("downgrade", "20260815_02")
    assert downgraded.returncode == 0, downgraded.stderr
    downgraded_columns = {
        column["name"] for column in inspect(engine).get_columns("tasks")
    }
    assert {"max_steps", "budget_deadline_at", "orchestration_json"}.isdisjoint(
        downgraded_columns
    )

    reupgraded = alembic("upgrade", "head")
    assert reupgraded.returncode == 0, reupgraded.stderr
    drift = alembic("check")
    assert drift.returncode == 0, drift.stdout + drift.stderr


def test_model_call_attempt_migration_round_trips_from_task8_v3(tmp_path):
    repository_root = Path(__file__).resolve().parents[3]
    database_path = tmp_path / "model-attempt-migration.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"

    def alembic(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert alembic("upgrade", "20260815_03").returncode == 0
    engine = make_engine(environment["DATABASE_URL"])
    assert "attempt" not in {
        column["name"] for column in inspect(engine).get_columns("model_calls")
    }

    upgraded = alembic("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "attempt" in {
        column["name"] for column in inspect(engine).get_columns("model_calls")
    }

    downgraded = alembic("downgrade", "20260815_03")
    assert downgraded.returncode == 0, downgraded.stderr
    assert "attempt" not in {
        column["name"] for column in inspect(engine).get_columns("model_calls")
    }

    assert alembic("upgrade", "head").returncode == 0
    drift = alembic("check")
    assert drift.returncode == 0, drift.stdout + drift.stderr


def test_provider_route_timestamp_migration_backfills_and_round_trips(tmp_path):
    repository_root = Path(__file__).resolve().parents[3]
    database_path = tmp_path / "provider-route-timestamps.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"

    def alembic(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert alembic("upgrade", "20260821_07").returncode == 0
    engine = make_engine(environment["DATABASE_URL"])
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO provider_routes (
                    provider,
                    base_url,
                    model,
                    api_style,
                    reasoning_effort,
                    updated_by,
                    created_at,
                    updated_at
                ) VALUES (
                    'legacy',
                    'https://legacy.example/v1',
                    'legacy-model',
                    'openai',
                    NULL,
                    NULL,
                    NULL,
                    NULL
                )
                """
            )
        )

    upgraded = alembic("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    upgraded_columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("provider_routes")
    }
    assert upgraded_columns["created_at"]["nullable"] is False
    assert upgraded_columns["updated_at"]["nullable"] is False
    with engine.connect() as connection:
        timestamps = connection.execute(
            text(
                """
                SELECT created_at, updated_at
                FROM provider_routes
                WHERE provider = 'legacy'
                """
            )
        ).one()
    assert timestamps.created_at is not None
    assert timestamps.updated_at is not None

    downgraded = alembic("downgrade", "20260821_07")
    assert downgraded.returncode == 0, downgraded.stderr
    downgraded_inspector = inspect(engine)
    downgraded_columns = {
        column["name"]: column
        for column in downgraded_inspector.get_columns("provider_routes")
    }
    assert downgraded_columns["created_at"]["nullable"] is True
    assert downgraded_columns["updated_at"]["nullable"] is True
    assert any(
        foreign_key["constrained_columns"] == ["updated_by"]
        and foreign_key["referred_table"] == "users"
        and foreign_key["options"].get("ondelete") == "RESTRICT"
        for foreign_key in downgraded_inspector.get_foreign_keys("provider_routes")
    )
    assert any(
        index["name"] == "ix_provider_routes_updated_by"
        and index["column_names"] == ["updated_by"]
        for index in downgraded_inspector.get_indexes("provider_routes")
    )
