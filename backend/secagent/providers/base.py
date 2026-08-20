from enum import StrEnum
from typing import Protocol

from secagent.domain import ModelRequest, ModelResponse


class ProviderErrorCode(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    NETWORK = "network"
    EMPTY_CONTENT = "empty_content"
    TRUNCATED = "truncated"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"


class ProviderFailure(RuntimeError):
    """A provider failure whose string representation is safe to persist."""

    def __init__(
        self,
        provider: str,
        code: ProviderErrorCode,
        retryable: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{provider}: {code.value}")
        self.provider = provider
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


class ProviderUnavailable(ProviderFailure):
    """Raised when a configured model provider cannot serve a request."""


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
