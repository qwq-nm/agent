from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from secagent.db import Base
from secagent.db_models import UserRow
from scripts.migrate_sqlite_to_postgres import MigrationValidationError, migrate


def repository_count(database_url: str, table_name: str) -> int:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        return int(
            connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one()
        )


def _seed_legacy_sqlite(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE tasks (
                    id VARCHAR(36) PRIMARY KEY,
                    goal TEXT NOT NULL,
                    authorization_scope TEXT NOT NULL,
                    route_mode VARCHAR(16) NOT NULL,
                    preferred_model VARCHAR(120),
                    scene_hint VARCHAR(40),
                    target_url TEXT,
                    scene VARCHAR(40),
                    status VARCHAR(32) NOT NULL,
                    is_demo BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE task_steps (
                    id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
                    step_index INTEGER NOT NULL, name VARCHAR(255) NOT NULL,
                    purpose TEXT NOT NULL, tool_name VARCHAR(120) NOT NULL,
                    params_json TEXT NOT NULL, risk_level VARCHAR(16) NOT NULL,
                    need_human_confirm BOOLEAN NOT NULL, status VARCHAR(32) NOT NULL,
                    model_provider VARCHAR(80), route_reason TEXT,
                    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE model_calls (
                    id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
                    provider VARCHAR(80) NOT NULL, model VARCHAR(120) NOT NULL,
                    stage VARCHAR(40) NOT NULL, route_reason TEXT NOT NULL,
                    input_summary TEXT NOT NULL, latency_ms INTEGER NOT NULL,
                    is_demo BOOLEAN NOT NULL, created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE tool_calls (
                    id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
                    step_id VARCHAR(36), tool_name VARCHAR(120) NOT NULL,
                    params_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    status VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE approvals (
                    id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
                    step_id VARCHAR(36) NOT NULL, tool_name VARCHAR(120) NOT NULL,
                    risk_level VARCHAR(16) NOT NULL, params_summary TEXT NOT NULL,
                    status VARCHAR(32) NOT NULL, reason TEXT,
                    created_at DATETIME NOT NULL, decided_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE reports (
                    id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
                    content TEXT NOT NULL, is_demo BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE evidences (
                    id VARCHAR(36) PRIMARY KEY,
                    task_id VARCHAR(36) NOT NULL,
                    tool_call_id VARCHAR(36),
                    evidence_type VARCHAR(80) NOT NULL,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confidence FLOAT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        created_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, goal, authorization_scope, route_mode, preferred_model,
                    scene_hint, target_url, scene, status, is_demo, created_at
                ) VALUES (
                    'task-1', 'Inspect logs', 'authorized logs', 'auto', NULL,
                    NULL, NULL, 'source', 'created', 0, :created_at
                )
                """
            ),
            {"created_at": created_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO task_steps (
                    id, task_id, step_index, name, purpose, tool_name, params_json,
                    risk_level, need_human_confirm, status, model_provider,
                    route_reason, created_at, updated_at
                ) VALUES (
                    'step-1', 'task-1', 0, 'Collect', 'Collect evidence',
                    'http_get', '{}', 'low', 0, 'pending', NULL, NULL,
                    :created_at, :created_at
                )
                """
            ),
            {"created_at": created_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO model_calls (
                    id, task_id, provider, model, stage, route_reason,
                    input_summary, latency_ms, is_demo, created_at
                ) VALUES (
                    'model-1', 'task-1', 'mock', 'mock-model', 'plan',
                    'test', 'summary', 1, 1, :created_at
                )
                """
            ),
            {"created_at": created_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO tool_calls (
                    id, task_id, step_id, tool_name, params_json, result_json,
                    status, created_at
                ) VALUES (
                    'tool-1', 'task-1', 'step-1', 'http_get', '{}', '{}',
                    'completed', :created_at
                )
                """
            ),
            {"created_at": created_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO approvals (
                    id, task_id, step_id, tool_name, risk_level, params_summary,
                    status, reason, created_at, decided_at
                ) VALUES (
                    'approval-1', 'task-1', 'step-1', 'http_get', 'low',
                    'test', 'approved', NULL, :created_at, NULL
                )
                """
            ),
            {"created_at": created_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO reports (id, task_id, content, is_demo, created_at)
                VALUES ('report-1', 'task-1', 'report', 1, :created_at)
                """
            ),
            {"created_at": created_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO evidences (
                    id, task_id, tool_call_id, evidence_type, source, content,
                    confidence, metadata_json, created_at
                ) VALUES (
                    'evidence-1', 'task-1', 'tool-1', 'http_observation',
                    'https://example.test', 'observed', 0.9, '{}', :created_at
                )
                """
            ),
            {"created_at": created_at},
        )


def _target_database(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            UserRow.__table__.insert().values(
                id="owner-1",
                username="migration-owner",
                password_hash="not-used",
                role="analyst",
                is_active=True,
                created_at=datetime.now(timezone.utc),
            )
        )
    return database_url


import pytest


@pytest.fixture
def seeded_sqlite_url(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'source.db').as_posix()}"
    _seed_legacy_sqlite(database_url)
    return database_url


@pytest.fixture
def empty_postgres_url(tmp_path):
    return _target_database(tmp_path)


def test_import_is_dry_run_without_apply(seeded_sqlite_url, empty_postgres_url):
    result = migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=False)
    assert result.mode == "dry-run"
    assert result.tables["tasks"].inserted == 0
    assert repository_count(empty_postgres_url, "tasks") == 0


def test_apply_preserves_evidence_hashes(seeded_sqlite_url, empty_postgres_url):
    result = migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=True)
    assert result.evidence_hash_mismatches == 0
    assert repository_count(empty_postgres_url, "tasks") == repository_count(
        seeded_sqlite_url, "tasks"
    )


def test_second_apply_is_idempotent(seeded_sqlite_url, empty_postgres_url):
    first = migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=True)
    second = migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=True)

    assert sum(table.inserted for table in first.tables.values()) == 7
    assert sum(table.inserted for table in second.tables.values()) == 0
    assert all(table.skipped == table.source_count for table in second.tables.values())
    for table_name in first.tables:
        assert repository_count(empty_postgres_url, table_name) == first.tables[
            table_name
        ].target_count


def test_invalid_evidence_hash_does_not_write(seeded_sqlite_url, empty_postgres_url):
    engine = create_engine(seeded_sqlite_url)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE evidences ADD COLUMN sha256 VARCHAR(64)"))
        connection.execute(
            text("UPDATE evidences SET sha256 = 'bad-hash' WHERE id = 'evidence-1'")
        )

    with pytest.raises(MigrationValidationError) as error:
        migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=True)

    assert error.value.summary.evidence_hash_mismatches == 1
    assert repository_count(empty_postgres_url, "tasks") == 0


def test_broken_legacy_relationship_does_not_write(seeded_sqlite_url, empty_postgres_url):
    engine = create_engine(seeded_sqlite_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE task_steps SET task_id = 'missing-task' WHERE id = 'step-1'")
        )

    with pytest.raises(MigrationValidationError):
        migrate(seeded_sqlite_url, empty_postgres_url, "migration-owner", apply=True)

    assert repository_count(empty_postgres_url, "tasks") == 0
