import pytest
from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.main import create_app
from secagent.repository import TaskRepository


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        model_mode="mock",
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as value:
        yield value


@pytest.fixture
def repository(app):
    with app.state.session_factory() as session:
        yield TaskRepository(session)
