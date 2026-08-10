from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from secagent.domain import TaskCreate, TaskRead
from secagent.repository import TaskRepository

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


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: str, repository: RepositoryDep) -> TaskRead:
    task = repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task
