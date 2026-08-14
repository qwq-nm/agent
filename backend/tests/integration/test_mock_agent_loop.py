from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.db import Base
from secagent.main import create_app


def test_mock_task_reaches_report_with_traceable_evidence(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'agent.db').as_posix()}",
        model_mode="mock",
        data_dir=tmp_path / "data",
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.session_factory.kw["bind"])
    with TestClient(app) as client:
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
