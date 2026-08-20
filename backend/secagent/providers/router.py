from secagent.domain import ModelRequest, ModelResponse, ModelStage
from secagent.providers.base import (
    ModelProvider,
    ProviderErrorCode,
    ProviderUnavailable,
)
from secagent.security.redaction import redact_text


FIXED_PROVIDER = {
    ModelStage.TASK_PARSE: "glm",
    ModelStage.PLAN: "deepseek",
    ModelStage.CRITIC: "deepseek",
    ModelStage.REPORT: "glm",
}


class ModelRouter:
    defaults = FIXED_PROVIDER

    def __init__(
        self,
        providers: dict[str, ModelProvider],
        mode: str = "auto",
        *,
        allow_missing: bool = False,
    ) -> None:
        if mode not in {"auto", "live", "mock"}:
            raise ValueError(f"unsupported model mode: {mode}")
        self.providers = providers
        self.mode = mode
        if mode == "live" and not allow_missing:
            for name in dict.fromkeys(FIXED_PROVIDER.values()):
                self._require_provider(name)
        elif mode == "mock":
            self._require_provider("mock")

    def provider_for(self, stage: ModelStage) -> ModelProvider:
        name = "mock" if self.mode == "mock" else FIXED_PROVIDER[stage]
        return self._require_provider(name)

    async def complete(
        self,
        stage: ModelStage,
        request: ModelRequest,
        preferred: str | None = None,
    ) -> ModelResponse:
        del preferred  # Stage ownership is fixed; manual input cannot override it.
        staged_request = request.model_copy(update={"stage": stage})
        return await self.provider_for(stage).complete(staged_request)

    def describe(self) -> list[dict[str, str | bool | None]]:
        result = []
        for name, provider in self.providers.items():
            api_style = getattr(provider, "api_style", None)
            base_url = getattr(provider, "base_url", None)
            display_name = name
            if name == "deepseek":
                display_name = (
                    "OpenCode Go (DeepSeek route)"
                    if api_style == "opencode-go"
                    else "DeepSeek Official"
                )
            elif name == "glm":
                display_name = "Zhipu GLM"
            elif name == "mock":
                display_name = "Mock Demo"
            result.append(
                {
                "name": name,
                "display_name": display_name,
                "configured": True,
                "mode": self.mode,
                "model": redact_text(
                    str(
                        getattr(
                            provider,
                            "model",
                            "deterministic-mock" if name == "mock" else "unknown",
                        )
                    )
                ),
                "status": "ready",
                "error_code": None,
                "api_style": api_style,
                "base_url": redact_text(str(base_url)) if base_url else None,
            }
            )
        return result

    async def aclose(self) -> None:
        closed: set[int] = set()
        for provider in self.providers.values():
            client = getattr(provider, "client", None)
            if client is None or id(client) in closed:
                continue
            closed.add(id(client))
            await client.aclose()

    def _require_provider(self, name: str) -> ModelProvider:
        provider = self.providers.get(name)
        if provider is None:
            raise ProviderUnavailable(
                name, ProviderErrorCode.AUTH, retryable=False
            )
        return provider
