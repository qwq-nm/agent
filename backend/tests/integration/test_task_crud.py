from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.main import create_app


def test_create_list_and_get_task(tmp_path) -> None:
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}"))
    with TestClient(app) as client:
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
