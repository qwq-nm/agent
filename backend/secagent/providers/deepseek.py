import asyncio
from collections.abc import Awaitable, Callable
import json
import random
import time
from typing import Any

import httpx

from secagent.domain import ModelRequest, ModelResponse, ModelStage
from secagent.providers.base import ProviderErrorCode, ProviderUnavailable
from secagent.providers.http_client import JSONResponse, ProviderHTTPClient
from secagent.providers.validation import (
    OutputValidationError,
    repair_context,
    validate_output,
)


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
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
        initial = await self._send(self._initial_payload(request))
        content, finish_reason = self._completion(initial)
        try:
            data = validate_output(content, request.response_schema)
            final = initial
            retry_count = initial.retry_count
        except OutputValidationError as first_error:
            repaired = await self._send(
                self._repair_payload(
                    request,
                    repair_context(
                        content,
                        first_error.errors,
                        request.response_schema,
                    ),
                )
            )
            repaired_content, finish_reason = self._completion(repaired)
            try:
                data = validate_output(repaired_content, request.response_schema)
            except OutputValidationError as repair_error:
                raise ProviderUnavailable(
                    self.name,
                    repair_error.code,
                    False,
                    repaired.request_id,
                ) from repair_error
            final = repaired
            retry_count = initial.retry_count + 1 + repaired.retry_count

        usage = (
            final.payload.get("usage")
            if isinstance(final.payload.get("usage"), dict)
            else {}
        )
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data=data,
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=final.request_id,
            finish_reason=finish_reason,
            prompt_tokens=_safe_token_count(usage.get("prompt_tokens")),
            completion_tokens=_safe_token_count(usage.get("completion_tokens")),
            retry_count=retry_count,
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
        payload.update(self._provider_options())
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
        payload.update(self._provider_options())
        return payload

    def _provider_options(self) -> dict[str, Any]:
        return {}

    def _completion(self, response: JSONResponse) -> tuple[str, str | None]:
        try:
            choice = response.payload["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.EMPTY_CONTENT,
                True,
                response.request_id,
            ) from exc
        if finish_reason == "length":
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.TRUNCATED,
                True,
                response.request_id,
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderUnavailable(
                self.name,
                ProviderErrorCode.EMPTY_CONTENT,
                True,
                response.request_id,
            )
        return content, finish_reason if isinstance(finish_reason, str) else None


class DeepSeekProvider(_StructuredProvider):
    name = "deepseek"
    allowed_stages = frozenset({ModelStage.PLAN, ModelStage.CRITIC})
    max_tokens = {ModelStage.PLAN: 4096, ModelStage.CRITIC: 2048}

    def _provider_options(self) -> dict[str, Any]:
        return {"thinking": {"type": "enabled"}}


def _safe_token_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
