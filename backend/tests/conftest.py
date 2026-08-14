import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from secagent.config import Settings
from secagent.db import Base
from secagent.main import create_app
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        model_mode="mock",
    )


@pytest.fixture
def app(settings):
    application = create_app(settings)
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
