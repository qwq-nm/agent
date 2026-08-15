import time
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from redis import Redis
from sqlalchemy import text

from secagent.auth.dependencies import AuthenticatedUser, current_user, require_admin
from secagent.domain import ModelRequest, ModelStage
from secagent.providers.base import ProviderFailure
from secagent.providers.http_client import safe_request_id
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_text
from secagent.services.provider_credentials import ProviderCredentialStore

router = APIRouter(prefix="/api")


def get_repository(request: Request) -> Iterator[TaskRepository]:
    with request.app.state.session_factory() as session:
        yield TaskRepository(session)


ActorDep = Annotated[AuthenticatedUser, Depends(current_user)]
AdminDep = Annotated[AuthenticatedUser, Depends(require_admin)]
RepositoryDep = Annotated[TaskRepository, Depends(get_repository)]


class ProviderCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepseek", "glm", "mock"]


class ProviderCheckRead(BaseModel):
    provider: str
    model: str
    status: Literal["ok", "failed"]
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int
    error_code: str | None = None


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "secagent-x"}


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok", "service": "secagent-x"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {
        "database": "ok",
        "redis": "ok",
        "model_configuration": "ok",
        "jwt": "ok",
    }
    try:
        with request.app.state.session_factory() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "failed"

    try:
        redis = Redis.from_url(
            request.app.state.settings.redis_url,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        try:
            redis.ping()
        finally:
            redis.close()
    except Exception:
        checks["redis"] = "failed"

    settings = request.app.state.settings
    try:
        with request.app.state.session_factory() as session:
            keys = ProviderCredentialStore(session, settings).resolve_keys(
                {
                    "deepseek": settings.deepseek_key(),
                    "glm": settings.glm_key(),
                }
            )
        if settings.model_mode == "live" and not (
            keys.get("deepseek") and keys.get("glm")
        ):
            checks["model_configuration"] = "failed"
    except Exception:
        checks["model_configuration"] = "failed"

    try:
        if not settings.jwt_key():
            checks["jwt"] = "failed"
    except (OSError, ValueError):
        checks["jwt"] = "failed"

    status_code = 200 if all(value == "ok" for value in checks.values()) else 503
    status = "ok" if status_code == 200 else "not_ready"
    return JSONResponse(
        status_code=status_code,
        content={"status": status, "checks": checks},
    )


@router.post("/models/check", response_model=ProviderCheckRead)
async def provider_check(
    payload: ProviderCheckRequest,
    request: Request,
    actor: AdminDep,
    repository: RepositoryDep,
) -> ProviderCheckRead:
    runtime = None
    try:
        runtime = request.app.state.provider_runtime_factory.build(repository.session)
        provider = runtime.providers.get(payload.provider)
        model = redact_text(
            str(
                getattr(
                    provider,
                    "model",
                    "deterministic-mock" if payload.provider == "mock" else "unknown",
                )
            )
        )
        started = time.perf_counter()
        result: ProviderCheckRead
        if provider is None:
            result = ProviderCheckRead(
                provider=payload.provider,
                model=model,
                status="failed",
                latency_ms=0,
                error_code="auth",
            )
        elif payload.provider == "mock":
            result = ProviderCheckRead(
                provider=payload.provider,
                model=model,
                status="ok",
                latency_ms=0,
            )
        else:
            stage = (
                ModelStage.PLAN
                if payload.provider == "deepseek"
                else ModelStage.TASK_PARSE
            )
            try:
                response = await provider.complete(
                    ModelRequest(
                        stage=stage,
                        system=(
                            "Return a minimal JSON object proving the configured "
                            "model is reachable."
                        ),
                        user='{"health_check":true}',
                        response_schema={
                            "title": "ProviderHealthCheck",
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    )
                )
                result = ProviderCheckRead(
                    provider=payload.provider,
                    model=redact_text(response.model or model),
                    status="ok",
                    request_id=safe_request_id(response.request_id),
                    input_tokens=response.prompt_tokens,
                    output_tokens=response.completion_tokens,
                    latency_ms=response.latency_ms,
                )
            except ProviderFailure as exc:
                result = ProviderCheckRead(
                    provider=payload.provider,
                    model=model,
                    status="failed",
                    request_id=safe_request_id(exc.request_id),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_code=exc.code.value,
                )
            except Exception:
                result = ProviderCheckRead(
                    provider=payload.provider,
                    model=model,
                    status="failed",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_code="server",
                )
    finally:
        if runtime is not None:
            await runtime.aclose()

    repository.record_audit(
        actor.id,
        "provider.check",
        "provider",
        payload.provider,
        result.status,
        {"error_code": result.error_code} if result.error_code else {},
    )
    return result


@router.get("/models/status")
async def model_status(
    request: Request, actor: ActorDep, repository: RepositoryDep
) -> list[dict]:
    runtime = None
    try:
        runtime = request.app.state.provider_runtime_factory.build(repository.session)
        result = runtime.describe()
    except Exception as exc:
        repository.record_audit(
            actor.id,
            "provider.check",
            "provider",
            None,
            "failure",
            {"error_type": type(exc).__name__},
        )
        raise
    finally:
        if runtime is not None:
            await runtime.aclose()
    repository.record_audit(
        actor.id, "provider.check", "provider", None, "success", {}
    )
    return result


@router.get("/tools")
def tool_status(request: Request, actor: ActorDep) -> list[dict]:
    del actor
    return request.app.state.tool_registry.describe()
