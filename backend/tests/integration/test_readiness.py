from secagent.domain import ModelResponse
from secagent.providers.router import ModelRouter


class CheckProvider:
    name = "deepseek"
    model = "deepseek-v4-pro"

    def __init__(self) -> None:
        self.client = TrackingClient()

    async def complete(self, request):
        del request
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data={"steps": []},
            latency_ms=120,
            request_id="req-1",
            prompt_tokens=12,
            completion_tokens=8,
        )


class TrackingClient:
    def __init__(self) -> None:
        self.is_closed = False

    async def aclose(self) -> None:
        self.is_closed = True


def test_live_is_process_only(client):
    assert client.get("/api/health/live").json() == {
        "status": "ok",
        "service": "secagent-x",
    }

def test_live_mode_without_both_keys_is_not_ready(app, client):
    app.state.settings.model_mode = "live"
    response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["model_configuration"] == "failed"


def test_admin_worker_summary_is_safe_and_analysts_are_denied(
    admin_client, analyst_client
):
    response = admin_client.get("/api/admin/workers")

    assert response.status_code == 200
    assert response.json() == {
        "online": 0,
        "active": 0,
        "capacity": 3,
        "queued": 0,
    }
    assert analyst_client.get("/api/admin/workers").status_code == 403


def test_admin_provider_check_returns_safe_runtime_metadata(
    admin_client, app, monkeypatch
) -> None:
    provider = CheckProvider()
    monkeypatch.setattr(
        app.state.provider_runtime_factory,
        "build",
        lambda _session: ModelRouter({"deepseek": provider}, mode="auto"),
    )

    response = admin_client.post("/api/models/check", json={"provider": "deepseek"})

    assert response.status_code == 200
    assert response.json() == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "status": "ok",
        "request_id": "req-1",
        "input_tokens": 12,
        "output_tokens": 8,
        "latency_ms": 120,
        "error_code": None,
    }
    assert provider.client.is_closed is True
