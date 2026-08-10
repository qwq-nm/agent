from secagent.config import Settings
from secagent.providers.base import ModelProvider
from secagent.providers.mock import MockProvider
from secagent.providers.openai_compatible import OpenAICompatibleProvider


def build_providers(settings: Settings) -> dict[str, ModelProvider]:
    providers: dict[str, ModelProvider] = {"mock": MockProvider()}
    if settings.deepseek_api_key:
        providers["deepseek"] = OpenAICompatibleProvider(
            name="deepseek",
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            timeout_seconds=settings.model_timeout_seconds,
        )
    if settings.glm_api_key:
        providers["glm"] = OpenAICompatibleProvider(
            name="glm",
            base_url=settings.glm_base_url,
            api_key=settings.glm_api_key,
            model=settings.glm_model,
            timeout_seconds=settings.model_timeout_seconds,
        )
    return providers


__all__ = ["build_providers"]
