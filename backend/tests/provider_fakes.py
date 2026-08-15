from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from secagent.domain import ModelRequest, ModelStage
from secagent.providers.deepseek import DeepSeekProvider
from secagent.providers.glm import GLMProvider


ResponseSpec = dict[str, Any] | httpx.Response | Exception


def _client(
    responses: list[ResponseSpec],
    capture: list[httpx.Request] | None = None,
) -> httpx.AsyncClient:
    remaining = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if not remaining:
            raise AssertionError("provider made more requests than expected")
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_: float) -> None:
    return None


def deepseek_provider(
    returning: ResponseSpec | None = None,
    *,
    responses: list[ResponseSpec] | None = None,
    capture: list[httpx.Request] | None = None,
    base_url: str = "https://deepseek.invalid/v1",
    api_style: str = "deepseek",
    reasoning_effort: str = "high",
    sleep: Callable[[float], Awaitable[None]] = _no_sleep,
    jitter: Callable[[], float] = lambda: 0.0,
) -> DeepSeekProvider:
    sequence = responses or [returning or {}]
    return DeepSeekProvider(
        base_url=base_url,
        api_key="deepseek-test-key",
        model="deepseek-v4-pro",
        client=_client(sequence, capture),
        api_style=api_style,
        reasoning_effort=reasoning_effort,
        sleep=sleep,
        jitter=jitter,
    )


def glm_provider(
    returning: ResponseSpec | None = None,
    *,
    responses: list[ResponseSpec] | None = None,
    capture: list[httpx.Request] | None = None,
    sleep: Callable[[float], Awaitable[None]] = _no_sleep,
    jitter: Callable[[], float] = lambda: 0.0,
) -> GLMProvider:
    sequence = responses or [returning or {}]
    return GLMProvider(
        base_url="https://glm.invalid/v4",
        api_key="glm-test-key",
        model="glm-5.2",
        client=_client(sequence, capture),
        sleep=sleep,
        jitter=jitter,
    )


def plan_request() -> ModelRequest:
    return ModelRequest(
        stage=ModelStage.PLAN,
        system="Create a minimal authorized plan.",
        user='{"goal":"inspect logs"}',
        response_schema={
            "title": "PlanDocument",
            "type": "object",
            "properties": {"steps": {"type": "array", "items": {}}},
            "required": ["steps"],
            "additionalProperties": False,
        },
    )


def parse_request() -> ModelRequest:
    return ModelRequest(
        stage=ModelStage.TASK_PARSE,
        system="Parse the authorized task.",
        user='{"goal":"inspect logs"}',
        response_schema={
            "title": "ParsedTask",
            "type": "object",
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
            "additionalProperties": False,
        },
    )
