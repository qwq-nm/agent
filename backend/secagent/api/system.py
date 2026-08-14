from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from secagent.auth.dependencies import AuthenticatedUser, current_user
from secagent.repository import TaskRepository

router = APIRouter(prefix="/api")


def get_repository(request: Request) -> Iterator[TaskRepository]:
    with request.app.state.session_factory() as session:
        yield TaskRepository(session)


ActorDep = Annotated[AuthenticatedUser, Depends(current_user)]
RepositoryDep = Annotated[TaskRepository, Depends(get_repository)]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "secagent-x"}


@router.get("/models/status")
def model_status(
    request: Request, actor: ActorDep, repository: RepositoryDep
) -> list[dict]:
    try:
        result = request.app.state.model_router.describe()
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
    repository.record_audit(
        actor.id, "provider.check", "provider", None, "success", {}
    )
    return result


@router.get("/tools")
def tool_status(request: Request, actor: ActorDep) -> list[dict]:
    del actor
    return request.app.state.tool_registry.describe()
