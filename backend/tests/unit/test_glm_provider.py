import json

import httpx
import pytest

from secagent.domain import ModelStage
from secagent.providers.base import ProviderErrorCode, ProviderUnavailable
from tests.provider_fakes import glm_provider, parse_request, plan_request


@pytest.mark.asyncio
async def test_glm_empty_content_is_retryable_failure() -> None:
    provider = glm_provider(
        returning={
            "id": "glm-1",
            "choices": [
                {"finish_reason": "stop", "message": {"content": ""}}
            ],
        }
    )

    with pytest.raises(ProviderUnavailable, match="empty_content") as caught:
        await provider.complete(parse_request())

    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_glm_payload_has_no_thinking_and_uses_parse_token_limit() -> None:
    requests: list[httpx.Request] = []
    provider = glm_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"goal":"g"}'}}
            ]
        },
        capture=requests,
    )

    await provider.complete(parse_request())

    payload = json.loads(requests[0].content)
    assert "thinking" not in payload
    assert payload["stream"] is False
    assert payload["max_tokens"] == provider.max_tokens[ModelStage.TASK_PARSE]
    assert "JSON" in payload["messages"][0]["content"]


@pytest.mark.asyncio
async def test_glm_rejects_deepseek_stage_before_transport() -> None:
    requests: list[httpx.Request] = []
    provider = glm_provider(capture=requests)

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is ProviderErrorCode.INVALID_SCHEMA
    assert requests == []


@pytest.mark.asyncio
async def test_glm_repairs_invalid_json_on_glm_only() -> None:
    requests: list[httpx.Request] = []
    provider = glm_provider(
        responses=[
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{"}}
                ]
            },
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"goal":"fixed"}'}}
                ]
            },
        ],
        capture=requests,
    )

    response = await provider.complete(parse_request())

    assert response.provider == "glm"
    assert response.data == {"goal": "fixed"}
    assert response.retry_count == 1
    assert len(requests) == 2
    assert all(request.url.host == "glm.invalid" for request in requests)


@pytest.mark.asyncio
async def test_glm_repair_preserves_schema_field_names_and_bounds_bad_content() -> None:
    requests: list[httpx.Request] = []
    request = parse_request()
    request.response_schema["properties"]["authorization_scope"] = {
        "type": "string"
    }
    request.response_schema["required"].append("authorization_scope")
    provider = glm_provider(
        responses=[
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "api_key=sk-must-not-cross " + ("x" * 20_000)
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"goal":"fixed","authorization_scope":"scope"}'
                        },
                    }
                ]
            },
        ],
        capture=requests,
    )

    await provider.complete(request)

    repair_content = json.loads(requests[1].content)["messages"][0]["content"]
    repair_context = json.loads(repair_content)
    authorization_rule = repair_context["target_schema"]["properties"][
        "authorization_scope"
    ]
    assert authorization_rule == {"type": "string"}
    assert "sk-must-not-cross" not in repair_content
    assert len(repair_context["invalid_json"]) <= 8192


@pytest.mark.asyncio
async def test_glm_never_exposes_unknown_finish_reason() -> None:
    provider = glm_provider(
        returning={
            "choices": [
                {
                    "finish_reason": "Bearer top-secret-token",
                    "message": {"content": '{"goal":"g"}'},
                }
            ]
        }
    )

    response = await provider.complete(parse_request())

    assert response.finish_reason == "unknown"
    assert "top-secret-token" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_glm_repair_treats_missing_or_invalid_usage_as_zero() -> None:
    provider = glm_provider(
        responses=[
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{"}}
                ],
                "usage": {"prompt_tokens": -1, "completion_tokens": True},
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"goal":"fixed"}'},
                    }
                ]
            },
        ]
    )

    response = await provider.complete(parse_request())

    assert response.prompt_tokens == 0
    assert response.completion_tokens == 0
    assert response.retry_count == 1
