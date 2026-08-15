from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


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

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, _exc: Exception) -> JSONResponse:
        return _response(
            request, 500, "internal_error", "Internal server error", None
        )


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
