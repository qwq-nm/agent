import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from secagent import db_models  # noqa: F401 -- registers ORM tables
from secagent.db import Base, make_engine


CONVERSATION_TABLES = {
    "conversations",
    "conversation_messages",
    "message_attachments",
    "conversation_turns",
    "conversation_events",
}

EXPECTED_COLUMNS = {
    "conversations": {
        "id",
        "owner_id",
        "title",
        "status",
        "settings_json",
        "active_turn_id",
        "next_message_sequence",
        "next_turn_plan_version",
        "created_at",
        "updated_at",
    },
    "conversation_messages": {
        "id",
        "conversation_id",
        "sequence",
        "role",
        "kind",
        "content",
        "status",
        "turn_id",
        "idempotency_key",
        "created_at",
        "updated_at",
    },
    "message_attachments": {
        "id",
        "message_id",
        "original_name",
        "storage_ref",
        "relative_path",
        "content_type",
        "size_bytes",
        "sha256",
        "status",
        "created_at",
    },
    "conversation_turns": {
        "id",
        "conversation_id",
        "trigger_message_id",
        "task_id",
        "plan_version",
        "status",
        "budget_json",
        "replan_from_turn_id",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    },
    "conversation_events": {
        "id",
        "conversation_id",
        "turn_id",
        "subtask_id",
        "event_type",
        "payload_json",
        "created_at",
    },
}


def _engine(tmp_path, name: str = "conversation-schema.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return engine


def _foreign_key(inspector, table: str, constrained_column: str):
    return next(
        item
        for item in inspector.get_foreign_keys(table)
        if item["constrained_columns"] == [constrained_column]
    )


def _unique_column_sets(inspector, table: str) -> set[tuple[str, ...]]:
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


def test_conversation_metadata_has_exact_tables_columns_and_contracts(tmp_path) -> None:
    inspector = inspect(_engine(tmp_path))
    assert CONVERSATION_TABLES <= set(inspector.get_table_names())

    for table, expected in EXPECTED_COLUMNS.items():
        columns = {item["name"]: item for item in inspector.get_columns(table)}
        assert set(columns) == expected, table
        for name, column in columns.items():
            if (table, name) not in {
                ("conversations", "active_turn_id"),
                ("conversation_messages", "turn_id"),
                ("conversation_messages", "idempotency_key"),
                ("message_attachments", "relative_path"),
                ("conversation_turns", "task_id"),
                ("conversation_turns", "replan_from_turn_id"),
                ("conversation_turns", "started_at"),
                ("conversation_turns", "finished_at"),
                ("conversation_events", "turn_id"),
                ("conversation_events", "subtask_id"),
            }:
                assert column["nullable"] is False, (table, name)

    expected_foreign_keys = {
        ("conversations", "owner_id"): (
            "users",
            "RESTRICT",
            "fk_conversations_owner_id_users",
        ),
        ("conversations", "active_turn_id"): (
            "conversation_turns",
            "SET NULL",
            "fk_conversations_active_turn_id_conversation_turns",
        ),
        ("conversation_messages", "conversation_id"): (
            "conversations",
            "CASCADE",
            "fk_conversation_messages_conversation_id_conversations",
        ),
        ("conversation_messages", "turn_id"): (
            "conversation_turns",
            "SET NULL",
            "fk_conversation_messages_turn_id_conversation_turns",
        ),
        ("message_attachments", "message_id"): (
            "conversation_messages",
            "CASCADE",
            "fk_message_attachments_message_id_conversation_messages",
        ),
        ("conversation_turns", "conversation_id"): (
            "conversations",
            "CASCADE",
            "fk_conversation_turns_conversation_id_conversations",
        ),
        ("conversation_turns", "trigger_message_id"): (
            "conversation_messages",
            "NO ACTION",
            "fk_conversation_turns_trigger_message_id_conversation_messages",
        ),
        ("conversation_turns", "task_id"): (
            "tasks",
            "SET NULL",
            "fk_conversation_turns_task_id_tasks",
        ),
        ("conversation_turns", "replan_from_turn_id"): (
            "conversation_turns",
            "SET NULL",
            "fk_conversation_turns_replan_from_turn_id_conversation_turns",
        ),
        ("conversation_events", "conversation_id"): (
            "conversations",
            "CASCADE",
            "fk_conversation_events_conversation_id_conversations",
        ),
        ("conversation_events", "turn_id"): (
            "conversation_turns",
            "SET NULL",
            "fk_conversation_events_turn_id_conversation_turns",
        ),
    }
    for (table, column), (target, ondelete, name) in expected_foreign_keys.items():
        foreign_key = _foreign_key(inspector, table, column)
        assert foreign_key["referred_table"] == target
        reflected_ondelete = foreign_key["options"].get("ondelete")
        if ondelete == "NO ACTION":
            # SQLite reflects its explicit default action as None.
            assert reflected_ondelete in {None, "NO ACTION"}
        else:
            assert reflected_ondelete == ondelete
        assert foreign_key["name"] == name

    trigger = _foreign_key(inspector, "conversation_turns", "trigger_message_id")
    assert trigger["options"].get("deferrable") is True
    assert trigger["options"].get("initially") == "DEFERRED"
    trigger_metadata = next(
        item
        for item in Base.metadata.tables["conversation_turns"].foreign_key_constraints
        if item.name
        == "fk_conversation_turns_trigger_message_id_conversation_messages"
    )
    assert trigger_metadata.ondelete == "NO ACTION"

    assert {
        ("conversation_id", "sequence"),
        ("conversation_id", "idempotency_key"),
    } <= _unique_column_sets(inspector, "conversation_messages")
    assert ("conversation_id", "plan_version") in _unique_column_sets(
        inspector, "conversation_turns"
    )

    expected_indexes = {
        "conversations": {"owner_id", "active_turn_id"},
        "conversation_messages": {"conversation_id", "turn_id"},
        "message_attachments": {"message_id"},
        "conversation_turns": {
            "conversation_id",
            "trigger_message_id",
            "task_id",
            "replan_from_turn_id",
        },
        "conversation_events": {"conversation_id", "turn_id", "subtask_id"},
    }
    for table, columns in expected_indexes.items():
        indexed = {
            column
            for index in inspector.get_indexes(table)
            for column in index["column_names"]
        }
        assert columns <= indexed, table

    check_names = {
        item["name"]
        for table in CONVERSATION_TABLES
        for item in inspector.get_check_constraints(table)
    }
    assert {
        "ck_conversations_next_message_sequence_ge_1",
        "ck_conversations_next_turn_plan_version_ge_1",
        "ck_conversation_messages_sequence_ge_1",
        "ck_message_attachments_size_bytes_ge_0",
        "ck_conversation_turns_plan_version_ge_1",
    } <= check_names


def test_conversation_event_cursor_never_reuses_deleted_highest_id(tmp_path) -> None:
    engine = _engine(tmp_path, "event-cursor.db")
    owner_id = str(uuid4())
    conversation_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, 'cursor-owner', 'hash', 'analyst', 1, CURRENT_TIMESTAMP)"
            ),
            {"id": owner_id},
        )
        connection.execute(
            text(
                "INSERT INTO conversations (id, owner_id, title) "
                "VALUES (:id, :owner_id, 'Cursor test')"
            ),
            {"id": conversation_id, "owner_id": owner_id},
        )
        first = connection.execute(
            text(
                "INSERT INTO conversation_events (conversation_id, event_type) "
                "VALUES (:conversation_id, 'first') RETURNING id"
            ),
            {"conversation_id": conversation_id},
        ).scalar_one()
        second = connection.execute(
            text(
                "INSERT INTO conversation_events (conversation_id, event_type) "
                "VALUES (:conversation_id, 'second') RETURNING id"
            ),
            {"conversation_id": conversation_id},
        ).scalar_one()
        connection.execute(
            text("DELETE FROM conversation_events WHERE id = :id"), {"id": second}
        )
        third = connection.execute(
            text(
                "INSERT INTO conversation_events (conversation_id, event_type) "
                "VALUES (:conversation_id, 'third') RETURNING id"
            ),
            {"conversation_id": conversation_id},
        ).scalar_one()

    assert (first, second, third) == (1, 2, 3)


def test_deferred_trigger_blocks_direct_message_delete_but_parent_cascades(
    tmp_path,
) -> None:
    engine = _engine(tmp_path, "conversation-delete.db")
    owner_id = str(uuid4())
    conversation_id = str(uuid4())
    message_id = str(uuid4())
    turn_id = str(uuid4())
    attachment_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, 'delete-owner', 'hash', 'analyst', 1, CURRENT_TIMESTAMP)"
            ),
            {"id": owner_id},
        )
        connection.execute(
            text(
                "INSERT INTO conversations (id, owner_id, title) "
                "VALUES (:id, :owner_id, 'Delete test')"
            ),
            {"id": conversation_id, "owner_id": owner_id},
        )
        connection.execute(
            text(
                "INSERT INTO conversation_messages "
                "(id, conversation_id, sequence, role, kind, content) "
                "VALUES (:id, :conversation_id, 1, 'user', 'user_text', 'hello')"
            ),
            {"id": message_id, "conversation_id": conversation_id},
        )
        connection.execute(
            text(
                "INSERT INTO conversation_turns "
                "(id, conversation_id, trigger_message_id, plan_version) "
                "VALUES (:id, :conversation_id, :message_id, 1)"
            ),
            {
                "id": turn_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
            },
        )
        connection.execute(
            text(
                "UPDATE conversations SET active_turn_id = :turn_id WHERE id = :id"
            ),
            {"turn_id": turn_id, "id": conversation_id},
        )
        connection.execute(
            text("UPDATE conversation_messages SET turn_id = :turn_id WHERE id = :id"),
            {"turn_id": turn_id, "id": message_id},
        )
        connection.execute(
            text(
                "INSERT INTO message_attachments "
                "(id, message_id, original_name, storage_ref, content_type, size_bytes, sha256) "
                "VALUES (:id, :message_id, 'a.txt', 'uploads/a.txt', 'text/plain', 1, :sha)"
            ),
            {"id": attachment_id, "message_id": message_id, "sha": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO conversation_events (conversation_id, turn_id, event_type) "
                "VALUES (:conversation_id, :turn_id, 'turn.created')"
            ),
            {"conversation_id": conversation_id, "turn_id": turn_id},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM conversation_messages WHERE id = :id"),
                {"id": message_id},
            )

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )

    with engine.connect() as connection:
        for table in (
            "conversations",
            "conversation_messages",
            "message_attachments",
            "conversation_turns",
            "conversation_events",
        ):
            assert connection.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar_one() == 0


def test_conversation_migration_round_trips_and_has_no_drift(tmp_path) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    database_path = tmp_path / "conversation-migration.db"
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

    assert alembic("upgrade", "20260826_08").returncode == 0
    engine = make_engine(environment["DATABASE_URL"])
    assert CONVERSATION_TABLES.isdisjoint(inspect(engine).get_table_names())

    upgraded = alembic("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    inspector = inspect(engine)
    assert CONVERSATION_TABLES <= set(inspector.get_table_names())
    assert _foreign_key(
        inspector, "conversations", "active_turn_id"
    )["name"] == "fk_conversations_active_turn_id_conversation_turns"
    assert _foreign_key(
        inspector, "conversation_messages", "turn_id"
    )["name"] == "fk_conversation_messages_turn_id_conversation_turns"

    downgraded = alembic("downgrade", "20260826_08")
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    assert CONVERSATION_TABLES.isdisjoint(inspect(engine).get_table_names())

    reupgraded = alembic("upgrade", "head")
    assert reupgraded.returncode == 0, reupgraded.stdout + reupgraded.stderr
    drift = alembic("check")
    assert drift.returncode == 0, drift.stdout + drift.stderr


def test_postgres_offline_sql_contains_conversation_tables_and_cycle_constraints() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "postgresql+psycopg://user:pass@db/secagent"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    sql = result.stdout.lower()
    for table in CONVERSATION_TABLES:
        assert f"create table {table}" in sql
    assert "fk_conversations_active_turn_id_conversation_turns" in sql
    assert "fk_conversation_messages_turn_id_conversation_turns" in sql
    assert "deferrable initially deferred" in sql
