import httpx
from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.main import create_app
from secagent.security.url_guard import UrlGuard
from secagent.tools.web_tools import HttpFetch


HTML = """<!doctype html><html><body>
<form action="/search" method="get">
  <input name="q"><input name="page" value="1">
</form>
</body></html>"""


def test_approved_web_scene_records_passive_http_and_form_evidence(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html; charset=utf-8",
                "x-content-type-options": "nosniff",
            },
            text=HTML,
        )

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'web.db').as_posix()}",
        data_dir=tmp_path / "data",
        model_mode="mock",
        web_allowed_hosts="web-demo",
    )
    app = create_app(settings)
    app.state.tool_registry._tools["http_fetch"] = HttpFetch(
        UrlGuard({"web-demo"}, resolver=lambda host: ["172.20.0.10"]),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        task = client.post(
            "/api/tasks",
            json={
                "goal": "被动分析授权的 Web 演示站点",
                "authorization_scope": "仅允许 GET，不提交表单",
                "route_mode": "auto",
                "scene_hint": "web_analysis",
                "target_url": "http://web-demo/",
            },
        ).json()
        first = client.post(f"/api/tasks/{task['id']}/run")
        assert first.status_code == 202
        assert first.json()["status"] == "waiting_human"
        waiting = client.get(f"/api/tasks/{task['id']}").json()
        assert waiting["pending_approval"]["tool_name"] == "http_fetch"
        assert waiting["pending_approval"]["risk_level"] == "medium"
        approved = client.post(
            f"/api/tasks/{task['id']}/approve",
            json={"approved": True, "reason": "确认仅进行被动 GET"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "completed"
        report = client.get(f"/api/tasks/{task['id']}/report").text
        assert "http://web-demo/" in report
        assert "HTTP 200" in report
        assert "content-type" in report
        assert "action=/search" in report
        assert "q" in report and "page" in report
