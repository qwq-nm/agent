from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.db import Base
from secagent.domain import UserRole
from secagent.main import create_app
from secagent.services.auth_service import AuthService


def test_create_list_and_get_task(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            jwt_signing_key="test-signing-key-at-least-32-bytes",
        )
    )
    Base.metadata.create_all(app.state.session_factory.kw["bind"])
    with app.state.session_factory() as session:
        AuthService.from_session(session, app.state.settings).create_user(
            "alice", "Correct-Horse-9", UserRole.ANALYST
        )
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "Correct-Horse-9"},
        )
        assert login.status_code == 200
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        created = client.post(
            "/api/tasks",
            json={
                "goal": "分析 access.log",
                "authorization_scope": "仅分析上传文件",
                "route_mode": "auto",
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["status"] == "created"
        assert client.get("/api/tasks").json()[0]["id"] == task_id
        assert client.get(f"/api/tasks/{task_id}").json()["goal"] == "分析 access.log"
