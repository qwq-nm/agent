import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from secagent.db import Base, make_engine, make_session_factory
from secagent.db_models import TaskRow, TaskStepRow, UserRow


def test_schema_creation_is_explicit(tmp_path) -> None:
    database_path = tmp_path / "secagent.db"
    database_url = f"sqlite:///{database_path.as_posix()}"

    factory = make_session_factory(database_url)

    assert inspect(factory.kw["bind"]).get_table_names() == []

    Base.metadata.create_all(make_engine(database_url))

    assert "tasks" in inspect(factory.kw["bind"]).get_table_names()


def test_sqlite_enforces_task_owned_row_cascade(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'cascade.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        task = TaskRow(
            goal="analyze logs",
            authorization_scope="uploaded logs only",
            route_mode="auto",
        )
        session.add(task)
        session.flush()
        step = TaskStepRow(
            task_id=task.id,
            step_index=1,
            name="parse",
            purpose="parse logs",
            tool_name="log_analyzer",
            risk_level="low",
        )
        session.add(step)
        session.commit()
        step_id = step.id

        session.delete(task)
        session.commit()

        assert session.get(TaskStepRow, step_id) is None


def test_sqlite_enforces_task_owner_restrict(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'restrict.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        owner = UserRow(
            username="owner",
            password_hash="not-a-real-hash",
            role="analyst",
        )
        session.add(owner)
        session.flush()
        session.add(
            TaskRow(
                owner_id=owner.id,
                goal="analyze logs",
                authorization_scope="uploaded logs only",
                route_mode="auto",
            )
        )
        session.commit()

        session.delete(owner)
        with pytest.raises(IntegrityError):
            session.commit()
