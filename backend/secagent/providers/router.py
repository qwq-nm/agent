from secagent.domain import ModelRequest, ModelResponse, ModelStage
from secagent.providers.base import ModelProvider, ProviderUnavailable


class ModelRouter:
    defaults = {
        ModelStage.TASK_PARSE: "glm",
        ModelStage.REPORT: "glm",
        ModelStage.PLAN: "deepseek",
        ModelStage.CRITIC: "deepseek",
    }

    def __init__(self, providers: dict[str, ModelProvider], mode: str = "auto") -> None:
        if mode not in {"auto", "live", "mock"}:
            raise ValueError(f"unsupported model mode: {mode}")
        self.providers = providers
        self.mode = mode

    async def complete(
        self,
        stage: ModelStage,
        request: ModelRequest,
        preferred: str | None = None,
    ) -> ModelResponse:
        if self.mode == "mock":
            return await self.providers["mock"].complete(request)

        primary = preferred or self.defaults[stage]
        candidates = [primary] if preferred else [
            primary,
            "deepseek" if primary == "glm" else "glm",
        ]
        last_error: Exception | None = None
        for name in dict.fromkeys(candidates):
            try:
                return await self.providers[name].complete(request)
            except Exception as exc:
                last_error = exc

        if self.mode == "auto":
            return await self.providers["mock"].complete(request)
        raise ProviderUnavailable(str(last_error))

    def describe(self) -> list[dict[str, str | bool]]:
        return [
            {"name": name, "configured": True, "mode": self.mode}
            for name in self.providers
        ]
