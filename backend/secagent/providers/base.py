from typing import Protocol

from secagent.domain import ModelRequest, ModelResponse


class ProviderUnavailable(RuntimeError):
    """Raised when a configured model provider cannot serve a request."""


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
