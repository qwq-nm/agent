from collections.abc import Iterator
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from secagent.domain import TaskCreate, TaskRead
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.services.task_service import TaskService
from secagent.services.storage import StorageService

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def get_repository(request: Request) -> Iterator[TaskRepository]:
    with request.app.state.session_factory() as session:
        yield TaskRepository(session)


RepositoryDep = Annotated[TaskRepository, Depends(get_repository)]


async def parse_task_create(request: Request) -> tuple[TaskCreate, UploadFile | None]:
    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("application/json"):
            return TaskCreate.model_validate(await request.json()), None
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            raw_payload = form.get("payload")
            upload = form.get("file")
            if not isinstance(raw_payload, str):
                raise HTTPException(
                    status_code=422, detail="multipart payload is required"
                )
            if upload is not None and not isinstance(upload, StarletteUploadFile):
                raise HTTPException(status_code=422, detail="file must be an upload")
            return TaskCreate.model_validate_json(raw_payload), upload
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    raise HTTPException(
        status_code=415,
        detail="use application/json or multipart/form-data",
    )


def storage_for(request: Request) -> StorageService:
    settings = request.app.state.settings
    return StorageService(
        settings.data_dir,
        upload_max_bytes=settings.upload_max_bytes,
        archive_max_files=settings.archive_max_files,
        archive_max_bytes=settings.archive_max_bytes,
    )


def task_service_for(request: Request, repository: TaskRepository) -> TaskService:
    return TaskService(
        repository,
        request.app.state.model_router,
        request.app.state.tool_registry,
        request.app.state.settings.data_dir,
    )


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(request: Request, repository: RepositoryDep) -> TaskRead:
    payload, upload = await parse_task_create(request)
    task = repository.create_task(payload)
    if upload is not None:
        storage = storage_for(request)
        try:
            await storage.save_upload(task.id, upload)
        except Exception as exc:
            repository.delete_task(task.id)
            storage.remove_workspace(task.id)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return task


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
    service = task_service_for(request, repository)
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


class ApprovalDecision(BaseModel):
    approved: bool
    reason: str = Field(min_length=1, max_length=1000)


def lifecycle_action(
    action: str,
    task_id: str,
    request: Request,
    repository: TaskRepository,
) -> TaskRead:
    service = task_service_for(request, repository)
    try:
        return getattr(service, action)(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/pause", response_model=TaskRead)
def pause_task(
    task_id: str, request: Request, repository: RepositoryDep
) -> TaskRead:
    return lifecycle_action("pause", task_id, request, repository)


@router.post(
    "/{task_id}/resume",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_task(
    task_id: str, request: Request, repository: RepositoryDep
) -> TaskRead:
    return lifecycle_action("resume", task_id, request, repository)


@router.post("/{task_id}/cancel", response_model=TaskRead)
def cancel_task(
    task_id: str, request: Request, repository: RepositoryDep
) -> TaskRead:
    return lifecycle_action("cancel", task_id, request, repository)


@router.post(
    "/{task_id}/retry",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_task(
    task_id: str, request: Request, repository: RepositoryDep
) -> TaskRead:
    return lifecycle_action("retry", task_id, request, repository)


@router.post("/{task_id}/approve", response_model=TaskRead)
async def approve_task(
    task_id: str,
    payload: ApprovalDecision,
    request: Request,
    repository: RepositoryDep,
) -> TaskRead:
    service = task_service_for(request, repository)
    try:
        return await service.approve(
            task_id, approved=payload.approved, reason=payload.reason
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
