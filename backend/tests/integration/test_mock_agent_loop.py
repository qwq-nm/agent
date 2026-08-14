from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.db import Base
from secagent.domain import UserRole
from secagent.main import create_app
from secagent.services.auth_service import AuthService


def test_mock_task_reaches_report_with_traceable_evidence(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'agent.db').as_posix()}",
        model_mode="mock",
        data_dir=tmp_path / "data",
        jwt_signing_key="test-signing-key-at-least-32-bytes",
    )
    app = create_app(settings)
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
        task = client.post(
            "/api/tasks",
            json={
                "goal": "分析示例日志并生成报告",
                "authorization_scope": "仅使用内置示例数据",
                "route_mode": "auto",
            },
        ).json()
        run = client.post(f"/api/tasks/{task['id']}/run")
        assert run.status_code == 202
        detail = client.get(f"/api/tasks/{task['id']}").json()
        assert detail["status"] == "completed"
        assert detail["is_demo"] is True
        assert detail["steps"]
        assert detail["evidences"][0]["source"]
        report = client.get(f"/api/tasks/{task['id']}/report").text
        assert "演示结果" in report
        assert "证据链" in report
