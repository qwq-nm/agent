from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from secagent.domain import TaskCreate, TaskRead
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.services.task_service import TaskService

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def get_repository(request: Request) -> Iterator[TaskRepository]:
    with request.app.state.session_factory() as session:
        yield TaskRepository(session)


RepositoryDep = Annotated[TaskRepository, Depends(get_repository)]


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, repository: RepositoryDep) -> TaskRead:
    return repository.create_task(payload)


@router.get("", response_model=list[TaskRead])
def list_tasks(repository: RepositoryDep) -> list[TaskRead]:
    return repository.list_tasks()


@router.get("/{task_id}")
def get_task(task_id: str, repository: RepositoryDep) -> dict:
    task = repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task.model_dump(mode="json") | LedgerService(repository).snapshot(task_id)


@router.post("/{task_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_task(
    task_id: str,
    request: Request,
    repository: RepositoryDep,
) -> dict:
    if repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    service = TaskService(
        repository,
        request.app.state.model_router,
        request.app.state.tool_registry,
        request.app.state.settings.data_dir,
    )
    result = await service.run(task_id)
    return result.model_dump(mode="json")


@router.get("/{task_id}/report")
def get_report(task_id: str, repository: RepositoryDep) -> Response:
    snapshot = LedgerService(repository).snapshot(task_id)
    if not snapshot["reports"]:
        raise HTTPException(status_code=404, detail="report not found")
    return Response(
        snapshot["reports"][-1]["content"],
        media_type="text/markdown",
    )
