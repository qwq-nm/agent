from collections.abc import Mapping

import httpx

from secagent.config import Settings
from secagent.providers.base import (
    ModelProvider,
    ProviderErrorCode,
    ProviderUnavailable,
)
from secagent.providers.deepseek import DeepSeekProvider
from secagent.providers.glm import GLMProvider
from secagent.providers.http_client import pooled_async_client
from secagent.providers.mock import MockProvider


def build_providers(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    provider_keys: Mapping[str, str | None] | None = None,
    allow_missing_live: bool = False,
) -> dict[str, ModelProvider]:
    providers: dict[str, ModelProvider] = {"mock": MockProvider()}
    keys = (
        provider_keys
        if provider_keys is not None
        else {
            "deepseek": settings.deepseek_key(),
            "glm": settings.glm_key(),
        }
    )
    deepseek_key = keys.get("deepseek")
    glm_key = keys.get("glm")
    if settings.model_mode == "live" and not allow_missing_live:
        for name, key in (("deepseek", deepseek_key), ("glm", glm_key)):
            if not key:
                raise ProviderUnavailable(
                    name, ProviderErrorCode.AUTH, retryable=False
                )
    if deepseek_key:
        _validate_model("deepseek", settings.deepseek_model)
    if glm_key:
        _validate_model("glm", settings.glm_model)
    shared_client = (
        client
        if client is not None
        else pooled_async_client(settings.model_timeout_seconds)
        if deepseek_key or glm_key
        else None
    )
    if deepseek_key and shared_client is not None:
        providers["deepseek"] = DeepSeekProvider(
            base_url=settings.deepseek_base_url,
            api_key=deepseek_key,
            model=settings.deepseek_model,
            client=shared_client,
            timeout_seconds=settings.model_timeout_seconds,
        )
    if glm_key and shared_client is not None:
        providers["glm"] = GLMProvider(
            base_url=settings.glm_base_url,
            api_key=glm_key,
            model=settings.glm_model,
            client=shared_client,
            timeout_seconds=settings.model_timeout_seconds,
        )
    return providers


def _validate_model(provider: str, model: str) -> None:
    normalized = model.strip().lower()
    if not normalized or "gpt" in normalized:
        raise ProviderUnavailable(
            provider, ProviderErrorCode.INVALID_SCHEMA, retryable=False
        )


__all__ = ["build_providers"]
