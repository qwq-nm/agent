import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from secagent.config import Settings
from secagent.db import Base
from secagent.domain import UserRole
from secagent.main import create_app
from secagent.queue.fake import FakeJobQueue
from secagent.repository import TaskRepository
from secagent.services.auth_service import AuthService
from secagent.services.ledger import LedgerService
from secagent.worker import execute_queued_task


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        model_mode="mock",
        jwt_signing_key="test-signing-key-at-least-32-bytes",
    )


@pytest.fixture
def fake_queue():
    return FakeJobQueue()


@pytest.fixture
def run_queued_job(app, fake_queue):
    def run(index: int = -1) -> None:
        job = fake_queue.enqueued[index]
        asyncio.run(
            execute_queued_task(
                job.task_id,
                job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
            )
        )

    return run


@pytest.fixture
def app(settings, fake_queue):
    application = create_app(settings, job_queue=fake_queue)
    Base.metadata.create_all(application.state.session_factory.kw["bind"])
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as value:
        yield value


@pytest.fixture
def repository(app):
    with app.state.session_factory() as session:
        yield TaskRepository(session)


@pytest.fixture
def ledger(repository):
    return LedgerService(repository)


@pytest.fixture
def seeded_admin(app):
    with app.state.session_factory() as session:
        return AuthService.from_session(session, app.state.settings).create_user(
            "admin", "Admin-Pass-9", UserRole.ADMIN
        )


@pytest.fixture
def seeded_analyst(app):
    with app.state.session_factory() as session:
        return AuthService.from_session(session, app.state.settings).create_user(
            "alice", "Correct-Horse-9", UserRole.ANALYST
        )


@pytest.fixture
def seeded_bob(app):
    with app.state.session_factory() as session:
        return AuthService.from_session(session, app.state.settings).create_user(
            "bob", "Bob-Secure-Pass-9", UserRole.ANALYST
        )


def _authenticated_client(app, username: str, password: str):
    with TestClient(app) as value:
        response = value.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200
        value.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield value


@pytest.fixture
def admin_client(app, seeded_admin):
    yield from _authenticated_client(app, "admin", "Admin-Pass-9")


@pytest.fixture
def analyst_client(app, seeded_analyst):
    yield from _authenticated_client(app, "alice", "Correct-Horse-9")


@pytest.fixture
def alice_client(analyst_client):
    return analyst_client


@pytest.fixture
def bob_client(app, seeded_bob):
    yield from _authenticated_client(app, "bob", "Bob-Secure-Pass-9")


@pytest.fixture
def bob_task(bob_client):
    response = bob_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect Bob's authorized logs",
            "authorization_scope": "Bob's uploaded logs only",
            "route_mode": "auto",
        },
    )
    assert response.status_code == 201
    return response.json()
