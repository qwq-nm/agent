import json

import httpx
import pytest

from secagent.domain import ModelRequest, ModelStage
from secagent.providers.base import ProviderErrorCode, ProviderUnavailable
from tests.provider_fakes import deepseek_provider, parse_request, plan_request


@pytest.mark.asyncio
async def test_deepseek_records_usage_and_never_exposes_reasoning() -> None:
    provider = deepseek_provider(
        returning={
            "id": "ds-req-1",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": '{"steps": []}',
                        "reasoning_content": "private",
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        }
    )

    response = await provider.complete(plan_request())

    assert response.request_id == "ds-req-1"
    assert response.data == {"steps": []}
    assert response.prompt_tokens == 12
    assert response.completion_tokens == 5
    assert "private" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_deepseek_payload_is_non_streaming_json_with_thinking() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"steps": []}'}}
            ]
        },
        capture=requests,
    )

    await provider.complete(plan_request())

    payload = json.loads(requests[0].content)
    assert payload["stream"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == provider.max_tokens[ModelStage.PLAN]
    assert payload["thinking"] == {"type": "enabled"}
    assert "JSON" in payload["messages"][0]["content"]
    assert "Example" in payload["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_max_tokens"),
    [
        (ModelStage.DECOMPOSE, 4096),
        (ModelStage.SUBTASK_EXECUTE, 4096),
        (ModelStage.SYNTHESIZE, 8192),
    ],
)
async def test_deepseek_supports_new_stages_with_bounded_output_tokens(
    stage: ModelStage, expected_max_tokens: int
) -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
            ]
        },
        capture=requests,
        model="deepseek-v4-flash",
    )

    response = await provider.complete(
        ModelRequest(
            stage=stage,
            system="s",
            user="{}",
            response_schema={"type": "object"},
        )
    )

    assert response.data == {"ok": True}
    assert json.loads(requests[0].content)["max_tokens"] == expected_max_tokens


@pytest.mark.asyncio
async def test_opencode_go_payload_uses_reasoning_effort() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"steps": []}'}}
            ]
        },
        capture=requests,
        base_url="https://opencode.ai/zen/go/v1",
        api_style="opencode-go",
    )

    await provider.complete(plan_request())

    payload = json.loads(requests[0].content)
    assert payload["reasoning_effort"] == "high"
    assert "thinking" not in payload


@pytest.mark.asyncio
async def test_opencode_go_decompose_uses_low_reasoning_effort_by_default() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
            ]
        },
        capture=requests,
        base_url="https://opencode.ai/zen/go/v1",
        api_style="opencode-go",
        model="deepseek-v4-flash",
    )

    await provider.complete(
        ModelRequest(
            stage=ModelStage.DECOMPOSE,
            system="s",
            user="{}",
            response_schema={"type": "object"},
        )
    )

    payload = json.loads(requests[0].content)
    assert payload["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_opencode_go_synthesis_keeps_configured_reasoning_effort() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
            ]
        },
        capture=requests,
        base_url="https://opencode.ai/zen/go/v1",
        api_style="opencode-go",
        model="deepseek-v4-flash",
    )

    await provider.complete(
        ModelRequest(
            stage=ModelStage.SYNTHESIZE,
            system="s",
            user="{}",
            response_schema={"type": "object"},
        )
    )

    payload = json.loads(requests[0].content)
    assert payload["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_opencode_go_stage_effort_override_is_explicitly_configurable() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
            ]
        },
        capture=requests,
        base_url="https://opencode.ai/zen/go/v1",
        api_style="opencode-go",
        model="deepseek-v4-flash",
        stage_effort_overrides={ModelStage.DECOMPOSE: "medium"},
    )

    await provider.complete(
        ModelRequest(
            stage=ModelStage.DECOMPOSE,
            system="s",
            user="{}",
            response_schema={"type": "object"},
        )
    )

    payload = json.loads(requests[0].content)
    assert payload["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_deepseek_decompose_disables_thinking_by_default() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
            ]
        },
        capture=requests,
        model="deepseek-v4-flash",
    )

    await provider.complete(
        ModelRequest(
            stage=ModelStage.DECOMPOSE,
            system="s",
            user="{}",
            response_schema={"type": "object"},
        )
    )

    payload = json.loads(requests[0].content)
    assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_deepseek_synthesis_keeps_thinking_enabled_by_default() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
            ]
        },
        capture=requests,
        model="deepseek-v4-flash",
    )

    await provider.complete(
        ModelRequest(
            stage=ModelStage.SYNTHESIZE,
            system="s",
            user="{}",
            response_schema={"type": "object"},
        )
    )

    payload = json.loads(requests[0].content)
    assert payload["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_deepseek_stage_thinking_override_is_explicitly_configurable() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning={
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
            ]
        },
        capture=requests,
        model="deepseek-v4-flash",
        stage_thinking_overrides={ModelStage.DECOMPOSE: True},
    )

    await provider.complete(
        ModelRequest(
            stage=ModelStage.DECOMPOSE,
            system="s",
            user="{}",
            response_schema={"type": "object"},
        )
    )

    payload = json.loads(requests[0].content)
    assert payload["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_deepseek_rejects_glm_stage_before_transport() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(capture=requests)

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(parse_request())

    assert caught.value.code is ProviderErrorCode.INVALID_SCHEMA
    assert requests == []


@pytest.mark.asyncio
async def test_deepseek_repairs_invalid_json_once_with_minimal_redacted_input() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        responses=[
            {
                "id": "initial",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"api_key":"sk-secret-value",'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
            {
                "id": "repair",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"steps": []}'},
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3},
            },
        ],
        capture=requests,
    )

    response = await provider.complete(plan_request())

    assert response.data == {"steps": []}
    assert response.request_id == "repair"
    assert response.retry_count == 1
    assert response.prompt_tokens == 18
    assert response.completion_tokens == 5
    assert len(requests) == 2
    repair_payload = json.loads(requests[1].content)
    repair_input = json.loads(repair_payload["messages"][0]["content"])
    assert set(repair_input) == {
        "invalid_json",
        "validation_errors",
        "target_schema",
    }
    assert "sk-secret-value" not in requests[1].content.decode()
    assert "Create a minimal authorized plan" not in requests[1].content.decode()
    assert repair_payload["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_deepseek_repairs_schema_failure_only_once() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        responses=[
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{}"}}
                ]
            },
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{}"}}
                ]
            },
        ],
        capture=requests,
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is ProviderErrorCode.INVALID_SCHEMA
    assert caught.value.retryable is False
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_deepseek_auth_failure_is_not_retried_or_repaired(
    status_code: int,
) -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        returning=httpx.Response(
            status_code,
            json={"api_key": "sk-body-secret"},
            headers={"x-request-id": "request-safe"},
        ),
        capture=requests,
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is ProviderErrorCode.AUTH
    assert caught.value.retryable is False
    assert caught.value.request_id == "request-safe"
    assert str(caught.value) == "deepseek: auth"
    assert "secret" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_deepseek_rate_limit_retries_three_attempts_and_honors_retry_after() -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    provider = deepseek_provider(
        responses=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(429, headers={"retry-after": "1000"}),
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"steps": []}'}}
                ]
            },
        ],
        capture=requests,
        sleep=record_delay,
    )

    response = await provider.complete(plan_request())

    assert response.retry_count == 2
    assert len(requests) == 3
    assert delays == [2.0, 30.0]


@pytest.mark.asyncio
async def test_deepseek_server_failure_stops_after_three_attempts() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        responses=[httpx.Response(503), httpx.Response(500), httpx.Response(502)],
        capture=requests,
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is ProviderErrorCode.SERVER
    assert caught.value.retryable is True
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (httpx.ReadTimeout("slow"), ProviderErrorCode.TIMEOUT),
        (httpx.ConnectError("offline"), ProviderErrorCode.NETWORK),
    ],
)
async def test_deepseek_transport_failures_retry_three_times(
    failure: Exception, code: ProviderErrorCode
) -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        responses=[failure, failure, failure],
        capture=requests,
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is code
    assert caught.value.retryable is True
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_deepseek_fallback_delay_uses_injected_jitter() -> None:
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    provider = deepseek_provider(
        responses=[
            httpx.Response(500),
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"steps": []}'}}
                ]
            },
        ],
        sleep=record_delay,
        jitter=lambda: 0.5,
    )

    await provider.complete(plan_request())

    assert delays == [0.375]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body", "code", "retryable"),
    [
        ({"choices": []}, ProviderErrorCode.EMPTY_CONTENT, True),
    ],
)
async def test_deepseek_classifies_unusable_completions(
    response_body: dict, code: ProviderErrorCode, retryable: bool
) -> None:
    provider = deepseek_provider(returning=response_body)

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is code
    assert caught.value.retryable is retryable


@pytest.mark.asyncio
async def test_deepseek_retries_truncated_completion_with_larger_token_limit() -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        responses=[
            {
                "choices": [
                    {"finish_reason": "length", "message": {"content": '{"steps": ['}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
            },
            {
                "id": "retry-success",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"steps": []}'}}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        ],
        capture=requests,
    )

    response = await provider.complete(plan_request())

    first_payload = json.loads(requests[0].content)
    retry_payload = json.loads(requests[1].content)
    assert response.data == {"steps": []}
    assert response.request_id == "retry-success"
    assert response.retry_count == 1
    assert response.prompt_tokens == 21
    assert response.completion_tokens == 4099
    assert retry_payload["max_tokens"] > first_payload["max_tokens"]


@pytest.mark.asyncio
async def test_deepseek_rejects_non_object_provider_body_without_exposing_it() -> None:
    provider = deepseek_provider(
        returning=httpx.Response(200, content=b"not-json sk-provider-secret")
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is ProviderErrorCode.INVALID_JSON
    assert str(caught.value) == "deepseek: invalid_json"


@pytest.mark.asyncio
async def test_deepseek_sanitizes_and_bounds_provider_request_id() -> None:
    provider = deepseek_provider(
        returning={
            "id": "Bearer secret-token\r\n" + ("x" * 300),
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"steps": []}'}}
            ],
        }
    )

    response = await provider.complete(plan_request())

    assert response.request_id is not None
    assert len(response.request_id) <= 128
    assert "secret-token" not in response.request_id
    assert "\r" not in response.request_id
    assert "\n" not in response.request_id


@pytest.mark.asyncio
async def test_deepseek_classifies_malformed_choice_without_raw_exception() -> None:
    provider = deepseek_provider(returning={"choices": [None]})

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is ProviderErrorCode.EMPTY_CONTENT
    assert str(caught.value) == "deepseek: empty_content"


@pytest.mark.asyncio
async def test_deepseek_never_exposes_unknown_finish_reason() -> None:
    provider = deepseek_provider(
        returning={
            "choices": [
                {
                    "finish_reason": "Bearer top-secret-token",
                    "message": {"content": '{"steps": []}'},
                }
            ]
        }
    )

    response = await provider.complete(plan_request())

    assert response.finish_reason == "unknown"
    assert "top-secret-token" not in response.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [" LENGTH ", "LeNgTh"])
async def test_deepseek_normalizes_length_before_validation_or_repair(
    finish_reason: str,
) -> None:
    requests: list[httpx.Request] = []
    provider = deepseek_provider(
        responses=[
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": "not JSON"},
                    }
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": "not JSON"},
                    }
                ]
            },
        ],
        capture=requests,
    )

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(plan_request())

    assert caught.value.code is ProviderErrorCode.TRUNCATED
    assert caught.value.retryable is True
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_deepseek_repair_token_usage_saturates_at_db_integer_limit() -> None:
    provider = deepseek_provider(
        responses=[
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{"}}
                ],
                "usage": {
                    "prompt_tokens": 2_147_483_647,
                    "completion_tokens": 2_147_483_646,
                },
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"steps": []}'},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 10},
            },
        ]
    )

    response = await provider.complete(plan_request())

    assert response.prompt_tokens == 2_147_483_647
    assert response.completion_tokens == 2_147_483_647
