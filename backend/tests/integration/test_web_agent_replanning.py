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

    def __init__(self, tool_name: str = "url_guard") -> None:
        self.tool_name = tool_name
        self.plan_inputs: list[dict] = []
        self.critic_calls = 0
        self.critic_system_prompts: list[str] = []

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
                        "tool_name": self.tool_name,
                        "params": {"probe": f"round-{round_number + 1}"},
                        "risk_level": "low",
                        "need_human_confirm": False,
                    }
                ]
            }
        elif title == "CriticDecision":
            self.critic_calls += 1
            self.critic_system_prompts.append(request.system)
            complete = self.critic_calls == 2
            data = {
                "is_complete": complete,
                "confidence": 0.9 if complete else 0.2,
                "reason": "sufficient" if complete else "need another probe",
                "missing_evidence": (
                    []
                    if complete
                    else [
                        {
                            "kind": "factual",
                            "description": "second observation",
                        }
                    ]
                ),
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
                    "missing_evidence": [
                        {"kind": "factual", "description": "more evidence"}
                    ],
                },
                latency_ms=0,
                prompt_tokens=5,
                completion_tokens=3,
            )
        return await super().complete(request)


class _ReportOnlyCriticDeepSeekStub(_ReplanningDeepSeekStub):
    async def complete(self, request) -> ModelResponse:
        if request.response_schema.get("title") == "CriticDecision":
            self.critic_calls += 1
            return ModelResponse(
                provider=self.name,
                model=self.model,
                data={
                    "is_complete": False,
                    "confidence": 0.8,
                    "reason": "The final report still needs to be generated",
                    "missing_evidence": [
                        {
                            "kind": "report_generation",
                            "description": "Need final report",
                        },
                        {
                            "kind": "report_generation",
                            "description": "尚需最终报告",
                        },
                    ],
                },
                latency_ms=0,
                prompt_tokens=5,
                completion_tokens=3,
            )
        return await super().complete(request)


class _MixedCriticDeepSeekStub(_ReplanningDeepSeekStub):
    async def complete(self, request) -> ModelResponse:
        if request.response_schema.get("title") == "CriticDecision":
            self.critic_calls += 1
            incomplete = self.critic_calls == 1
            return ModelResponse(
                provider=self.name,
                model=self.model,
                data={
                    "is_complete": True,
                    "confidence": 0.3 if incomplete else 0.9,
                    "reason": "Need another observation" if incomplete else "sufficient",
                    "missing_evidence": (
                        [
                            {
                                "kind": "report_generation",
                                "description": "Need final report",
                            },
                            {
                                "kind": "factual",
                                "description": "second observation",
                            },
                        ]
                        if incomplete
                        else []
                    ),
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


class _SlowObservationTool(_ObservationTool):
    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        await asyncio.sleep(3)
        return await super().run(params, context)


class _FailThenObserveHttpTool(_ObservationTool):
    name = "http_fetch"

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        probe = params["probe"]
        self.calls.append(probe)
        if len(self.calls) == 1:
            return ToolResult(
                success=False,
                summary="HTTP request did not receive a response",
                error="http_transport_error",
                evidence=[
                    {
                        "evidence_type": "http_observation",
                        "source": "https://target.test/",
                        "content": "HTTP request failed: http_transport_error",
                        "confidence": 1.0,
                    }
                ],
            )
        return ToolResult(
            success=True,
            summary=f"observed {probe}",
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": f"https://target.test/{probe}",
                    "content": f"observed {probe}",
                    "confidence": 1.0,
                }
            ],
        )


class _NonTransportFailureTool(_ObservationTool):
    name = "http_fetch"

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        probe = params["probe"]
        self.calls.append(probe)
        return ToolResult(
            success=False,
            summary="Tool input was rejected",
            error="invalid_tool_input",
        )


class _ChangedParamsDeepSeekStub:
    name = "deepseek"
    model = "deepseek-approval-scope-test"

    def __init__(self) -> None:
        self.plan_calls = 0

    async def complete(self, request) -> ModelResponse:
        title = request.response_schema.get("title")
        if title == "PlanDocument":
            self.plan_calls += 1
            path = "first" if self.plan_calls == 1 else "second"
            data = {
                "steps": [
                    {
                        "name": f"Fetch {path}",
                        "purpose": "Collect an authorized HTTP observation",
                        "tool_name": "http_fetch",
                        "params": {"url": f"https://target.test/{path}"},
                        "risk_level": "low",
                        "need_human_confirm": False,
                    }
                ]
            }
        elif title == "CriticDecision":
            data = {
                "is_complete": False,
                "confidence": 0.1,
                "reason": "another path is required",
                "missing_evidence": [
                    {
                        "kind": "factual",
                        "description": "second path observation",
                    }
                ],
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


class _AlwaysFailHttpTool(BaseTool):
    name = "http_fetch"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        url = params["url"]
        self.calls.append(url)
        return ToolResult(
            success=False,
            summary="HTTP request did not receive a response",
            error="http_transport_error",
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": url,
                    "content": "HTTP request failed: http_transport_error",
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
    assert all(
        "报告本身不得列为缺失证据" in prompt
        for prompt in deepseek.critic_system_prompts
    )
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


def test_report_generation_only_missing_evidence_is_complete(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _ReportOnlyCriticDeepSeekStub()
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    observation_tool = _ObservationTool()
    app.state.tool_registry._tools[observation_tool.name] = observation_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect the authorized target and produce a report",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-report-only-critic-001"},
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
            max_replans=0,
        )
    )

    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "completed"
    assert deepseek.critic_calls == 1
    assert len(deepseek.plan_inputs) == 1


def test_report_generation_items_are_removed_but_factual_gaps_replan(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _MixedCriticDeepSeekStub()
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    observation_tool = _ObservationTool()
    app.state.tool_registry._tools[observation_tool.name] = observation_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Collect two authorized target observations",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-mixed-critic-001"},
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
    assert detail["status"] == "completed"
    assert observation_tool.calls == ["round-1", "round-2"]
    replan_input = json.dumps(deepseek.plan_inputs[1], ensure_ascii=False)
    assert "second observation" in replan_input
    assert "Need final report" not in replan_input


def test_web_agent_replans_after_controlled_tool_failure(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _ReplanningDeepSeekStub(tool_name="http_fetch")
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    observation_tool = _FailThenObserveHttpTool()
    app.state.tool_registry._tools[observation_tool.name] = observation_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Recover from a temporary authorized target failure",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-tool-failure-replan-001"},
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
    assert "http_transport_error" in json.dumps(deepseek.plan_inputs[1])
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "completed"
    assert [step["status"] for step in detail["steps"]] == ["failed", "success"]


def test_web_agent_does_not_replan_non_transport_tool_failure(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _ReplanningDeepSeekStub(tool_name="http_fetch")
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    observation_tool = _NonTransportFailureTool()
    app.state.tool_registry._tools[observation_tool.name] = observation_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Reject an invalid authorized web tool request",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-non-transport-failure-001"},
    )
    job = fake_queue.enqueued[0]

    with pytest.raises(RuntimeError, match="invalid_tool_input"):
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

    assert observation_tool.calls == ["round-1"]
    assert len(deepseek.plan_inputs) == 1
    assert deepseek.critic_calls == 0
    assert analyst_client.get(f"/api/tasks/{task['id']}").json()["status"] == (
        "failed_retryable"
    )


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


def test_replanned_http_params_require_a_new_approval(
    analyst_client, app, fake_queue
) -> None:
    deepseek = _ChangedParamsDeepSeekStub()
    app.state.model_router = ModelRouter(
        {"glm": _GlmStub(), "deepseek": deepseek}, mode="live"
    )
    http_tool = _AlwaysFailHttpTool()
    app.state.tool_registry._tools[http_tool.name] = http_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect two explicitly approved target paths",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/first",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-approval-scope-run-001"},
    )

    first_job = fake_queue.enqueued[0]
    asyncio.run(
        execute_queued_task(
            first_job.task_id,
            first_job.command_id,
            app.state.session_factory,
            app.state.model_router,
            app.state.tool_registry,
            app.state.settings.data_dir,
            max_replans=1,
        )
    )
    first_detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert first_detail["status"] == "waiting_human"
    assert "/first" in first_detail["pending_approval"]["params_summary"]

    approved = analyst_client.post(
        f"/api/tasks/{task['id']}/approve",
        headers={"Idempotency-Key": "web-approval-scope-approve-001"},
        json={"approved": True, "reason": "Approve only the first path"},
    )
    assert approved.status_code == 202
    second_job = fake_queue.enqueued[1]
    asyncio.run(
        execute_queued_task(
            second_job.task_id,
            second_job.command_id,
            app.state.session_factory,
            app.state.model_router,
            app.state.tool_registry,
            app.state.settings.data_dir,
            max_replans=1,
        )
    )

    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "waiting_human"
    assert "/second" in detail["pending_approval"]["params_summary"]
    assert http_tool.calls == ["https://target.test/first"]


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


def test_tool_call_cannot_outlive_task_deadline(
    analyst_client, app, fake_queue
) -> None:
    app.state.model_router = ModelRouter(
        {
            "glm": _GlmStub(),
            "deepseek": _ReplanningDeepSeekStub(),
        },
        mode="live",
    )
    slow_tool = _SlowObservationTool()
    app.state.tool_registry._tools[slow_tool.name] = slow_tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Bound a slow authorized web tool",
            "authorization_scope": "Only the configured target host",
            "scene_hint": "web_analysis",
            "target_url": "https://target.test/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "web-tool-deadline-001"},
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
    assert slow_tool.calls == []
