import asyncio
import json
import time

import pytest

from secagent.db_models import TaskRow
from secagent.domain import ModelResponse, RiskLevel, ToolResult
from secagent.providers.mock import MockProvider
from secagent.providers.router import ModelRouter
from secagent.tools.base import BaseTool, ToolContext
from secagent.worker import execute_queued_task


class _GlmStub:
    name = "glm"
    model = "glm-replan-test"

    def __init__(self) -> None:
        self.mock = MockProvider()

    async def complete(self, request) -> ModelResponse:
        response = await self.mock.complete(request)
        return response.model_copy(
            update={"provider": self.name, "model": self.model}
        )


class _SlowGlmStub(_GlmStub):
    async def complete(self, request) -> ModelResponse:
        await asyncio.sleep(3)
        return await super().complete(request)


class _ReplanningDeepSeekStub:
    name = "deepseek"
    model = "deepseek-replan-test"

    def __init__(self) -> None:
        self.plan_inputs: list[dict] = []
        self.critic_calls = 0

    async def complete(self, request) -> ModelResponse:
        title = request.response_schema.get("title")
        if title == "PlanDocument":
            payload = json.loads(request.user)
            self.plan_inputs.append(payload)
            round_number = len(self.plan_inputs) - 1
            data = {
                "steps": [
                    {
                        "name": f"Probe round {round_number}",
                        "purpose": "Collect the missing authorized observation",
                        "tool_name": "url_guard",
                        "params": {"probe": f"round-{round_number + 1}"},
                        "risk_level": "low",
                        "need_human_confirm": False,
                    }
                ]
            }
        elif title == "CriticDecision":
            self.critic_calls += 1
            complete = self.critic_calls == 2
            data = {
                "is_complete": complete,
                "confidence": 0.9 if complete else 0.2,
                "reason": "sufficient" if complete else "need another probe",
                "missing_evidence": [] if complete else ["second observation"],
            }
        else:
            raise AssertionError(f"unexpected DeepSeek schema: {title}")
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data=data,
            latency_ms=0,
            prompt_tokens=5,
            completion_tokens=3,
        )


class _NeverCompleteDeepSeekStub(_ReplanningDeepSeekStub):
    async def complete(self, request) -> ModelResponse:
        if request.response_schema.get("title") == "CriticDecision":
            self.critic_calls += 1
            return ModelResponse(
                provider=self.name,
                model=self.model,
                data={
                    "is_complete": False,
                    "confidence": 0.1,
                    "reason": "still incomplete",
                    "missing_evidence": ["more evidence"],
                },
                latency_ms=0,
                prompt_tokens=5,
                completion_tokens=3,
            )
        return await super().complete(request)


class _ObservationTool(BaseTool):
    name = "url_guard"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        probe = params["probe"]
        self.calls.append(probe)
        return ToolResult(
            success=True,
            summary=f"observed {probe}",
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": f"https://target.test/{probe}",
                    "content": f"{probe} token=private-observation-secret",
                    "confidence": 1.0,
                }
            ],
        )


def test_web_agent_replans_from_redacted_observations_and_completes(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _ReplanningDeepSeekStub()
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    observation_tool = _ObservationTool()
    app.state.tool_registry._tools[observation_tool.name] = observation_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect the authorized web target and gather enough evidence",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-replan-001"},
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
            max_replans=1,
        )
    )

    assert observation_tool.calls == ["round-1", "round-2"]
    assert len(deepseek.plan_inputs) == 2
    second_plan = deepseek.plan_inputs[1]
    assert second_plan["replan_round"] == 1
    assert "round-1" in json.dumps(second_plan["observations"])
    assert "private-observation-secret" not in json.dumps(second_plan)
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "completed"
    assert [call["stage"] for call in detail["model_calls"]] == [
        "task_parse",
        "plan",
        "critic",
        "plan",
        "critic",
        "report",
    ]
    with app.state.session_factory() as session:
        checkpoint = json.loads(session.get(TaskRow, task["id"]).orchestration_json)
    assert checkpoint["stages"]["plan"]["data"]["replan_round"] == 1


def test_web_agent_stops_replanning_at_configured_bound(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _NeverCompleteDeepSeekStub()
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    observation_tool = _ObservationTool()
    app.state.tool_registry._tools[observation_tool.name] = observation_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Stop after the authorized web evidence budget",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-replan-bound-001"},
    )
    job = fake_queue.enqueued[0]

    with pytest.raises(RuntimeError, match="maximum replans"):
        asyncio.run(
            execute_queued_task(
                job.task_id,
                job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
                max_replans=1,
            )
        )

    assert len(deepseek.plan_inputs) == 2
    assert deepseek.critic_calls == 2
    assert analyst_client.get(f"/api/tasks/{task['id']}").json()["status"] == (
        "failed_retryable"
    )


def test_web_agent_uses_registered_tool_risk_over_model_risk(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _ReplanningDeepSeekStub()
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    observation_tool = _ObservationTool()
    observation_tool.risk_level = RiskLevel.MEDIUM
    app.state.tool_registry._tools[observation_tool.name] = observation_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Require approval for a state-changing web probe",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-risk-source-001"},
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
            max_replans=1,
        )
    )

    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "waiting_human"
    assert detail["pending_approval"]["risk_level"] == "medium"
    assert deepseek.critic_calls == 0


def test_model_call_cannot_outlive_task_deadline(
    analyst_client, app, fake_queue
) -> None:
    app.state.model_router = ModelRouter(
        {
            "glm": _SlowGlmStub(),
            "deepseek": _ReplanningDeepSeekStub(),
        },
        mode="live",
    )
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Bound a slow authorized web task",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-model-deadline-001"},
    )
    job = fake_queue.enqueued[0]

    started = time.perf_counter()
    asyncio.run(
        execute_queued_task(
            job.task_id,
            job.command_id,
            app.state.session_factory,
            app.state.model_router,
            app.state.tool_registry,
            app.state.settings.data_dir,
            task_timeout_seconds=1,
        )
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 2
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "failed"
    assert detail["model_calls"] == []
