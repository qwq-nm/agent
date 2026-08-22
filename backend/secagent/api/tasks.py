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

from secagent.auth.dependencies import AuthenticatedUser, current_user
from secagent.domain import TaskCreate, TaskRead
from secagent.providers.base import ProviderFailure
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.services.task_service import TaskService
from secagent.services.task_events import TaskEventService
from secagent.services.storage import StorageService

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def get_repository(request: Request) -> Iterator[TaskRepository]:
    with request.app.state.session_factory() as session:
        yield TaskRepository(session)


RepositoryDep = Annotated[TaskRepository, Depends(get_repository)]
ActorDep = Annotated[AuthenticatedUser, Depends(current_user)]


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
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid request body") from exc
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
        request.app.state.provider_runtime_factory.build(repository.session),
        request.app.state.tool_registry,
        request.app.state.settings.data_dir,
        request.app.state.job_queue,
        lease_seconds=request.app.state.settings.job_lease_seconds,
        heartbeat_seconds=request.app.state.settings.job_heartbeat_seconds,
        max_auto_retries=request.app.state.settings.job_auto_retries,
        task_timeout_seconds=request.app.state.settings.task_timeout_seconds,
        max_replans=request.app.state.settings.max_replans,
    )


def require_idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key")
    if value is None or not value.strip() or len(value) > 255:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required and must be at most 255 characters",
        )
    return value


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    request: Request, repository: RepositoryDep, actor: ActorDep
) -> TaskRead:
    payload, upload = await parse_task_create(request)
    task = repository.create_task(payload, actor.id, commit=False)
    settings = request.app.state.settings
    repository.configure_task_budget(
        task.id,
        max_model_calls=settings.max_model_calls_per_task,
        max_input_tokens=settings.max_input_tokens_per_task,
        max_output_tokens=settings.max_output_tokens_per_task,
        max_steps=settings.max_steps_per_task,
    )
    if upload is not None:
        storage = storage_for(request)
        try:
            await storage.save_upload(task.id, upload)
        except Exception as exc:
            repository.rollback()
            storage.remove_workspace(task.id)
            repository.record_audit(
                actor.id,
                "task.create",
                "task",
                task.id,
                "failure",
                {"error_type": type(exc).__name__},
            )
            raise HTTPException(
                status_code=422, detail="Upload validation failed"
            ) from exc
    TaskEventService(repository.session).append(
        task.id,
        "task.created",
        {},
        commit=False,
    )
    repository.record_audit(
        actor.id, "task.create", "task", task.id, "success", {}, commit=False
    )
    repository.commit()
    return task


@router.get("", response_model=list[TaskRead])
def list_tasks(repository: RepositoryDep, actor: ActorDep) -> list[TaskRead]:
    return repository.list_authorized(actor)


@router.get("/{task_id}")
def get_task(task_id: str, repository: RepositoryDep, actor: ActorDep) -> dict:
    task = repository.get_authorized(task_id, actor)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task.model_dump(mode="json") | LedgerService(repository).snapshot(task_id)


@router.post("/{task_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_task(
    task_id: str,
    request: Request,
    repository: RepositoryDep,
    actor: ActorDep,
) -> dict:
    service = task_service_for(request, repository)
    try:
        result = service.run(task_id, actor, require_idempotency_key(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ProviderFailure as exc:
        raise HTTPException(
            status_code=503,
            detail=f"模型服务不可用：{exc.provider} / {exc.code.value}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@router.post("/{task_id}/plan", response_model=TaskRead)
async def plan_task(
    task_id: str,
    request: Request,
    repository: RepositoryDep,
    actor: ActorDep,
) -> TaskRead:
    service = task_service_for(request, repository)
    try:
        return await service.plan(task_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{task_id}/report")
def get_report(
    task_id: str, repository: RepositoryDep, actor: ActorDep
) -> Response:
    task = repository.get_authorized(task_id, actor)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
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
    actor: AuthenticatedUser,
    idempotency_key: str | None = None,
) -> TaskRead:
    service = task_service_for(request, repository)
    try:
        if idempotency_key is None:
            return getattr(service, action)(task_id, actor)
        return getattr(service, action)(task_id, actor, idempotency_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/pause", response_model=TaskRead)
def pause_task(
    task_id: str, request: Request, repository: RepositoryDep, actor: ActorDep
) -> TaskRead:
    return lifecycle_action("pause", task_id, request, repository, actor)


@router.post(
    "/{task_id}/resume",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_task(
    task_id: str, request: Request, repository: RepositoryDep, actor: ActorDep
) -> TaskRead:
    return lifecycle_action(
        "resume",
        task_id,
        request,
        repository,
        actor,
        require_idempotency_key(request),
    )


@router.post("/{task_id}/cancel", response_model=TaskRead)
def cancel_task(
    task_id: str, request: Request, repository: RepositoryDep, actor: ActorDep
) -> TaskRead:
    return lifecycle_action("cancel", task_id, request, repository, actor)


@router.post(
    "/{task_id}/retry",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_task(
    task_id: str, request: Request, repository: RepositoryDep, actor: ActorDep
) -> TaskRead:
    return lifecycle_action(
        "retry",
        task_id,
        request,
        repository,
        actor,
        require_idempotency_key(request),
    )


@router.post("/{task_id}/approve", response_model=TaskRead)
async def approve_task(
    task_id: str,
    payload: ApprovalDecision,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    actor: ActorDep,
) -> TaskRead:
    service = task_service_for(request, repository)
    try:
        idempotency_key = require_idempotency_key(request) if payload.approved else None
        result = await service.approve(
            task_id,
            actor,
            approved=payload.approved,
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
        if payload.approved:
            response.status_code = status.HTTP_202_ACCEPTED
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
