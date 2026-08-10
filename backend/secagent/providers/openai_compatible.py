import asyncio
import json
import time

import httpx

from secagent.domain import ModelRequest, ModelResponse
from secagent.providers.base import ProviderUnavailable


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        last_error = "request failed"
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return ModelResponse(
                    provider=self.name,
                    model=self.model,
                    data=json.loads(content),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except httpx.HTTPStatusError as exc:
                last_error = f"HTTP {exc.response.status_code}"
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                if not retryable:
                    break
            except (httpx.TimeoutException, httpx.NetworkError):
                last_error = "network unavailable"
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                last_error = "invalid JSON response"
                break
            if attempt < 2:
                await asyncio.sleep((0.2, 0.5)[attempt])
        raise ProviderUnavailable(f"{self.name}: {last_error}")
