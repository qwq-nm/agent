import asyncio

from secagent.domain import ModelResponse
from secagent.providers.mock import MockProvider
from secagent.providers.router import ModelRouter
from secagent.worker import execute_queued_task


class NamedStubProvider:
    def __init__(self, name: str, calls: list[tuple[str, str]]) -> None:
        self.name = name
        self.calls = calls
        self.mock = MockProvider()

    async def complete(self, request):
        self.calls.append((request.stage.value, self.name))
        response = await self.mock.complete(request)
        return ModelResponse(
            **response.model_dump(exclude={"provider", "model", "prompt_tokens", "completion_tokens"}),
            provider=self.name,
            model=f"{self.name}-fixed-test",
            prompt_tokens=11,
            completion_tokens=7,
        )


def test_live_pipeline_uses_fixed_providers_and_persists_usage(
    analyst_client, app, fake_queue
) -> None:
    calls: list[tuple[str, str]] = []
    app.state.model_router = ModelRouter(
        {
            "glm": NamedStubProvider("glm", calls),
            "deepseek": NamedStubProvider("deepseek", calls),
        },
        mode="live",
    )
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect deterministic evidence",
            "authorization_scope": "Built-in evidence only",
            "route_mode": "manual",
            "preferred_model": "gpt-unsafe-fallback",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "fixed-stage-001"},
    )
    job = fake_queue.enqueued[0]

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

    assert calls == [
        ("task_parse", "glm"),
        ("plan", "deepseek"),
        ("critic", "deepseek"),
        ("report", "glm"),
    ]
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert [(call["stage"], call["provider"]) for call in detail["model_calls"]] == calls
    assert all(call["prompt_tokens"] == 11 for call in detail["model_calls"])
    assert all(call["completion_tokens"] == 7 for call in detail["model_calls"])
