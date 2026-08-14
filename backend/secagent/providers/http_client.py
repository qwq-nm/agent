import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import re
from typing import Any

import httpx

from secagent.providers.base import ProviderErrorCode, ProviderUnavailable
from secagent.security.redaction import redact_text


MAX_TRANSPORT_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 30.0
_REQUEST_ID_PATTERN = re.compile(r"[^A-Za-z0-9._:-]+")


@dataclass(frozen=True)
class JSONResponse:
    payload: dict[str, Any]
    retry_count: int
    request_id: str | None


def safe_request_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _REQUEST_ID_PATTERN.sub("_", redact_text(value))[:128].strip("_")
    return cleaned or None


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return min(MAX_RETRY_DELAY_SECONDS, max(0.0, float(value.strip())))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = (parsed - datetime.now(timezone.utc)).total_seconds()
            return min(MAX_RETRY_DELAY_SECONDS, max(0.0, delay))
        except (TypeError, ValueError, OverflowError):
            return None


class ProviderHTTPClient:
    """Retrying JSON transport around a caller-owned pooled AsyncClient."""

    def __init__(
        self,
        *,
        provider: str,
        client: httpx.AsyncClient,
        timeout_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.provider = provider
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        self.jitter = jitter

    async def post_json(
        self, *, url: str, api_key: str, payload: dict[str, Any]
    ) -> JSONResponse:
        for attempt in range(MAX_TRANSPORT_ATTEMPTS):
            try:
                response = await self.client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                failure = ProviderUnavailable(
                    self.provider, ProviderErrorCode.TIMEOUT, True
                )
                if attempt == MAX_TRANSPORT_ATTEMPTS - 1:
                    raise failure from exc
                await self.sleep(self._fallback_delay(attempt))
                continue
            except httpx.RequestError as exc:
                failure = ProviderUnavailable(
                    self.provider, ProviderErrorCode.NETWORK, True
                )
                if attempt == MAX_TRANSPORT_ATTEMPTS - 1:
                    raise failure from exc
                await self.sleep(self._fallback_delay(attempt))
                continue

            request_id = safe_request_id(response.headers.get("x-request-id"))
            failure = self._http_failure(response.status_code, request_id)
            if failure is not None:
                if not failure.retryable or attempt == MAX_TRANSPORT_ATTEMPTS - 1:
                    raise failure
                delay = _retry_after_seconds(response.headers.get("retry-after"))
                await self.sleep(
                    delay if delay is not None else self._fallback_delay(attempt)
                )
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderUnavailable(
                    self.provider,
                    ProviderErrorCode.INVALID_JSON,
                    False,
                    request_id,
                ) from exc
            if not isinstance(body, dict):
                raise ProviderUnavailable(
                    self.provider,
                    ProviderErrorCode.INVALID_JSON,
                    False,
                    request_id,
                )
            body_request_id = safe_request_id(body.get("id"))
            return JSONResponse(body, attempt, body_request_id or request_id)
        raise AssertionError("unreachable")

    def _fallback_delay(self, attempt: int) -> float:
        jitter = min(1.0, max(0.0, float(self.jitter())))
        return min(MAX_RETRY_DELAY_SECONDS, 0.25 * (2**attempt) * (1.0 + jitter))

    def _http_failure(
        self, status_code: int, request_id: str | None
    ) -> ProviderUnavailable | None:
        if 200 <= status_code < 300:
            return None
        if status_code in {401, 403}:
            return ProviderUnavailable(
                self.provider, ProviderErrorCode.AUTH, False, request_id
            )
        if status_code == 429:
            return ProviderUnavailable(
                self.provider, ProviderErrorCode.RATE_LIMIT, True, request_id
            )
        return ProviderUnavailable(
            self.provider,
            ProviderErrorCode.SERVER,
            status_code >= 500,
            request_id,
        )


def pooled_async_client(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout_seconds,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
