import asyncio

import httpx
from fastapi.testclient import TestClient

from secagent.config import Settings
from secagent.db import Base
from secagent.domain import UserRole
from secagent.main import create_app
from secagent.queue.fake import FakeJobQueue
from secagent.security.url_guard import UrlGuard
from secagent.tools.web_tools import HttpFetch
from secagent.services.auth_service import AuthService
from secagent.worker import execute_queued_task


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
        jwt_signing_key="test-signing-key-at-least-32-bytes",
    )
    queue = FakeJobQueue()
    app = create_app(settings, job_queue=queue)
    Base.metadata.create_all(app.state.session_factory.kw["bind"])
    with app.state.session_factory() as session:
        AuthService.from_session(session, app.state.settings).create_user(
            "alice", "Correct-Horse-9", UserRole.ANALYST
        )
    app.state.tool_registry._tools["http_fetch"] = HttpFetch(
        UrlGuard({"web-demo"}, resolver=lambda host: ["172.20.0.10"]),
        transport=httpx.MockTransport(handler),
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
                "goal": "被动分析授权的 Web 演示站点",
                "authorization_scope": "仅允许 GET，不提交表单",
                "route_mode": "auto",
                "scene_hint": "web_analysis",
                "target_url": "http://web-demo/",
            },
        ).json()
        first = client.post(
            f"/api/tasks/{task['id']}/run",
            headers={"Idempotency-Key": "web-run-001"},
        )
        assert first.status_code == 202
        assert first.json()["status"] == "queued"
        first_job = queue.enqueued[0]
        asyncio.run(
            execute_queued_task(
                first_job.task_id,
                first_job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
            )
        )
        waiting = client.get(f"/api/tasks/{task['id']}").json()
        assert waiting["pending_approval"]["tool_name"] == "http_fetch"
        assert waiting["pending_approval"]["risk_level"] == "medium"
        approved = client.post(
            f"/api/tasks/{task['id']}/approve",
            headers={"Idempotency-Key": "web-approve-001"},
            json={"approved": True, "reason": "确认仅进行被动 GET"},
        )
        assert approved.status_code == 202
        assert approved.json()["status"] == "queued"
        approved_job = queue.enqueued[1]
        asyncio.run(
            execute_queued_task(
                approved_job.task_id,
                approved_job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
            )
        )
        report = client.get(f"/api/tasks/{task['id']}/report").text
        assert "http://web-demo/" in report
        assert "HTTP 200" in report
        assert "content-type" in report
        assert "action=/search" in report
        assert "q" in report and "page" in report
