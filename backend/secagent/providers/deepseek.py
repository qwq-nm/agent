import asyncio
from collections.abc import Awaitable, Callable
import json
import random
import time
from typing import Any

import httpx

from secagent.domain import (
    MAX_DB_INTEGER,
    ModelRequest,
    ModelResponse,
    ModelStage,
    normalize_finish_reason,
    normalize_token_count,
)
from secagent.providers.base import ProviderErrorCode, ProviderUnavailable
from secagent.providers.http_client import JSONResponse, ProviderHTTPClient
from secagent.providers.validation import (
    OutputValidationError,
    repair_context,
    validate_output,
)

MAX_OUTPUT_TOKEN_RETRY_LIMIT = 8192
DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"


class _StructuredProvider:
    """Shared structured-output flow; concrete providers own their payload policy."""

    name: str
    allowed_stages: frozenset[ModelStage]
    max_tokens: dict[ModelStage, int]

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        api_style: str = "deepseek",
        reasoning_effort: str = "high",
        stage_effort_overrides: dict[ModelStage, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.api_style = api_style
        self.reasoning_effort = reasoning_effort
        self.stage_effort_overrides = stage_effort_overrides or {}
        self.client = client
        self.transport = ProviderHTTPClient(
            provider=self.name,
            client=client,
            timeout_seconds=timeout_seconds,
            sleep=sleep,
            jitter=jitter,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.stage not in self.allowed_stages:
            raise ProviderUnavailable(
                self.name, ProviderErrorCode.INVALID_SCHEMA, False
            )
        started = time.perf_counter()
        (
            content,
            finish_reason,
            final,
            prompt_tokens,
            completion_tokens,
            retry_count,
        ) = await self._send_completion_with_truncation_retry(
            request,
            self._initial_payload(request),
        )
        try:
            data = validate_output(content, request.response_schema)
        except OutputValidationError as first_error:
            (
                repaired_content,
                finish_reason,
                final,
                repair_prompt_tokens,
                repair_completion_tokens,
                repair_retry_count,
            ) = await self._send_completion_with_truncation_retry(
                request,
                self._repair_payload(
                    request,
                    repair_context(
                        content,
                        first_error.errors,
                        request.response_schema,
                    ),
                ),
            )
            try:
                data = validate_output(repaired_content, request.response_schema)
            except OutputValidationError as repair_error:
                raise ProviderUnavailable(
                    self.name,
                    repair_error.code,
                    False,
                    final.request_id,
                ) from repair_error
            retry_count += 1 + repair_retry_count
            prompt_tokens = _saturating_add(
                prompt_tokens, repair_prompt_tokens
            )
            completion_tokens = _saturating_add(
                completion_tokens, repair_completion_tokens
            )
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data=data,
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=final.request_id,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            retry_count=retry_count,
        )

    async def _send_completion_with_truncation_retry(
        self, request: ModelRequest, payload: dict[str, Any]
    ) -> tuple[str, str | None, JSONResponse, int, int, int]:
        response = await self._send(payload)
        prompt_tokens, completion_tokens = _usage_counts(response.payload)
        retry_count = response.retry_count
        try:
            content, finish_reason = self._completion(response)
        except ProviderUnavailable as exc:
            if exc.code is not ProviderErrorCode.TRUNCATED:
                raise
            retry_payload = self._truncation_retry_payload(request, payload)
            response = await self._send(retry_payload)
            retry_prompt_tokens, retry_completion_tokens = _usage_counts(
                response.payload
            )
            prompt_tokens = _saturating_add(prompt_tokens, retry_prompt_tokens)
            completion_tokens = _saturating_add(
                completion_tokens, retry_completion_tokens
            )
            retry_count += 1 + response.retry_count
            content, finish_reason = self._completion(response)
        return (
            content,
            finish_reason,
            response,
            prompt_tokens,
            completion_tokens,
            retry_count,
        )

    async def _send(self, payload: dict[str, Any]) -> JSONResponse:
        return await self.transport.post_json(
            url=f"{self.base_url}/chat/completions",
            api_key=self.api_key,
            payload=payload,
        )

    def _initial_payload(self, request: ModelRequest) -> dict[str, Any]:
        assert request.stage is not None
        schema = json.dumps(
            request.response_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": self.max_tokens[request.stage],
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{request.system}\nReturn only a JSON object matching this "
                        f"schema: {schema}\nExample JSON: {{\"result\":{{}}}}"
                    ),
                },
                {"role": "user", "content": request.user},
            ],
        }
        payload.update(self._provider_options(request))
        return payload

    def _repair_payload(
        self, request: ModelRequest, context: dict[str, Any]
    ) -> dict[str, Any]:
        assert request.stage is not None
        payload: dict[str, Any] = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": self.max_tokens[request.stage],
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        context,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            ],
        }
        payload.update(self._provider_options(request))
        return payload

    def _truncation_retry_payload(
        self, request: ModelRequest, payload: dict[str, Any]
    ) -> dict[str, Any]:
        assert request.stage is not None
        retry = dict(payload)
        current_max = int(retry.get("max_tokens") or self.max_tokens[request.stage])
        retry["max_tokens"] = min(
            MAX_OUTPUT_TOKEN_RETRY_LIMIT,
            max(current_max + 1024, current_max * 2),
        )
        retry["messages"] = [
            *retry.get("messages", []),
            {
                "role": "user",
                "content": (
                    "The previous JSON response was truncated. Return the same "
                    "schema again as a complete, concise JSON object only."
                ),
            },
        ]
        return retry

    def _provider_options(self, request: ModelRequest | None = None) -> dict[str, Any]:
        return {}

    def _completion(self, response: JSONResponse) -> tuple[str, str | None]:
        try:
            choice = response.payload["choices"][0]
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.EMPTY_CONTENT,
                True,
                response.request_id,
            ) from exc
        if not isinstance(choice, dict):
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.EMPTY_CONTENT,
                True,
                response.request_id,
            )
        finish_reason = normalize_finish_reason(choice.get("finish_reason"))
        if finish_reason == "length":
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.TRUNCATED,
                True,
                response.request_id,
            )
        try:
            content = choice["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.EMPTY_CONTENT,
                True,
                response.request_id,
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.EMPTY_CONTENT,
                True,
                response.request_id,
            )
        return content, finish_reason


class DeepSeekProvider(_StructuredProvider):
    name = "deepseek"
    allowed_stages = frozenset(
        {
            ModelStage.PLAN,
            ModelStage.CRITIC,
            ModelStage.DECOMPOSE,
            ModelStage.SUBTASK_EXECUTE,
            ModelStage.SYNTHESIZE,
        }
    )
    max_tokens = {
        ModelStage.PLAN: 4096,
        ModelStage.CRITIC: 4096,
        ModelStage.DECOMPOSE: 4096,
        ModelStage.SUBTASK_EXECUTE: 4096,
        ModelStage.SYNTHESIZE: 8192,
    }
    #: Default stage-level reasoning-effort overrides (opencode-go style only).
    #: Decomposition is the measured latency hotspot (100-140 s at "high");
    #: these frequent short stages default to "low" so each message does not
    #: pay minutes of reasoning cost. Final synthesis keeps the configured
    #: effort because answer quality matters most there.
    default_stage_effort_overrides: dict[ModelStage, str] = {
        ModelStage.DECOMPOSE: "low",
        ModelStage.SUBTASK_EXECUTE: "low",
    }

    def __init__(self, **kwargs: Any) -> None:
        explicit = dict(kwargs.pop("stage_effort_overrides", None) or {})
        merged = dict(self.default_stage_effort_overrides)
        merged.update(explicit)
        super().__init__(stage_effort_overrides=merged, **kwargs)

    def _provider_options(self, request: ModelRequest | None = None) -> dict[str, Any]:
        if self.api_style == "opencode-go":
            return {
                "reasoning_effort": self._effective_reasoning_effort(
                    request.stage if request is not None else None
                )
            }
        return {"thinking": {"type": "enabled"}}

    def _effective_reasoning_effort(self, stage: ModelStage | None) -> str:
        """Stage-level effort override, falling back to the configured default.

        Frequent short stages (decompose, subtask execute) default to ``low``
        because a single decomposition call was measured at 100-140 seconds
        with ``high`` reasoning; the final synthesis keeps the configured
        effort since answer quality matters most there.
        """
        if stage is not None and stage in self.stage_effort_overrides:
            return self.stage_effort_overrides[stage]
        return self.reasoning_effort


def _safe_token_count(value: object) -> int:
    return normalize_token_count(value)


def _saturating_add(first: int, second: int) -> int:
    return min(MAX_DB_INTEGER, first + second)


def _usage_counts(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    return (
        _safe_token_count(usage.get("prompt_tokens")),
        _safe_token_count(usage.get("completion_tokens")),
    )
