from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from secagent.providers.base import ProviderErrorCode, ProviderFailure


@dataclass
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    fields: list[str] | None = None


class ForbiddenResource(ApiError):
    def __init__(self, resource_type: str) -> None:
        super().__init__(403, "forbidden", f"Access to {resource_type} is forbidden")


class ApprovalExpired(ApiError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(409, "approval_expired", "Approval has expired")
        self.approval_id = approval_id


class QueueUnavailable(ApiError):
    def __init__(self) -> None:
        super().__init__(503, "service_unavailable", "Task queue is unavailable")


class CredentialStorageUnavailable(ApiError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "credential_storage_unavailable",
            "Provider credential storage is unavailable",
        )


class ConversationNotFound(ApiError):
    def __init__(self) -> None:
        super().__init__(404, "conversation_not_found", "Conversation not found")


class InvalidIdempotencyKey(ApiError):
    def __init__(self) -> None:
        super().__init__(
            400,
            "invalid_idempotency_key",
            "A valid Idempotency-Key header is required",
        )


class ConversationRequestValidationError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            422, "validation_error", "Request validation failed", fields=None
        )


class UnsupportedConversationMediaType(ApiError):
    def __init__(self) -> None:
        super().__init__(
            415,
            "unsupported_media_type",
            "Use application/json or multipart/form-data",
        )


class AttachmentValidationFailed(ApiError):
    def __init__(self) -> None:
        super().__init__(
            422,
            "attachment_validation_failed",
            "One or more attachments failed validation",
        )


class InvalidStreamTicket(ApiError):
    def __init__(self) -> None:
        super().__init__(401, "invalid_stream_ticket", "Invalid stream ticket")


class EventStreamUnavailable(ApiError):
    def __init__(self) -> None:
        super().__init__(
            503, "event_stream_unavailable", "Event streaming is unavailable"
        )


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    415: "unsupported_media_type",
    422: "validation_error",
    503: "service_unavailable",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        request.state.trace_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Trace-ID"] = request.state.trace_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return _response(
            request, exc.status_code, exc.code, exc.message, exc.fields
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"] if part != "body")
                for error in exc.errors()
            }
        )
        return _response(
            request,
            422,
            "validation_error",
            "Request validation failed",
            fields,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        fields = None
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return _response(
            request,
            exc.status_code,
            code,
            message,
            fields,
            headers=exc.headers,
        )

    @app.exception_handler(ProviderFailure)
    async def provider_error_handler(
        request: Request, exc: ProviderFailure
    ) -> JSONResponse:
        return _response(
            request,
            503,
            f"model_{exc.code.value}",
            _provider_message(exc),
            None,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, _exc: Exception) -> JSONResponse:
        return _response(
            request, 500, "internal_error", "Internal server error", None
        )


def _provider_message(exc: ProviderFailure) -> str:
    provider = exc.provider.upper()
    messages = {
        ProviderErrorCode.AUTH: f"{provider} 模型鉴权失败，请检查 API Key、接口地址和模型配置。",
        ProviderErrorCode.RATE_LIMIT: f"{provider} 模型请求触发限流，请稍后重试或切换 Provider。",
        ProviderErrorCode.TIMEOUT: f"{provider} 模型接口响应超时，执行计划没有生成完成。请稍后重试，或检查网络、模型服务状态和接口配置。",
        ProviderErrorCode.NETWORK: f"{provider} 模型接口网络连接失败，请检查 Docker 网络、代理和接口地址。",
        ProviderErrorCode.SERVER: f"{provider} 模型服务返回异常，请稍后重试或切换 Provider。",
        ProviderErrorCode.EMPTY_CONTENT: f"{provider} 模型返回为空，请重试或切换 Provider。",
        ProviderErrorCode.TRUNCATED: f"{provider} 模型输出被截断，请缩短任务描述或降低输出复杂度后重试。",
        ProviderErrorCode.INVALID_JSON: f"{provider} 模型返回格式不是有效 JSON，请重试或切换 Provider。",
        ProviderErrorCode.INVALID_SCHEMA: f"{provider} 模型返回结构不符合系统要求，请重试或切换 Provider。",
    }
    message = messages.get(exc.code, f"{provider} 模型暂时不可用，请稍后重试。")
    if exc.request_id:
        message += f" 请求 ID：{exc.request_id}"
    return message


def _response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    fields: list[str] | None,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", str(uuid4()))
    response_headers: dict[str, str] = {"X-Trace-ID": trace_id}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        headers=response_headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "fields": fields,
                "trace_id": trace_id,
            }
        },
    )
