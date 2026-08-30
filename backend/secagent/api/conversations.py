from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import FormData, UploadFile

from secagent.api.errors import (
    ApiError,
    AttachmentValidationFailed,
    ConversationNotFound,
    ConversationRequestValidationError,
    InvalidIdempotencyKey,
    UnsupportedConversationMediaType,
)
from secagent.auth.dependencies import AuthenticatedUser, current_user
from secagent.conversation_domain import (
    ConversationCreate,
    ConversationDetailRead,
    ConversationPatch,
    ConversationRead,
    IdempotencyKey,
    MessageSendRead,
    MessageSubmission,
)
from secagent.conversation_repository import (
    ConversationRepository,
    MessageIdempotencyConflict,
)
from secagent.repository import TaskRepository
from secagent.services.audit import AuditService
from secagent.services.conversation_events import ConversationEventService
from secagent.services.conversation_service import (
    ArchivedConversationError,
    AttachmentIdempotencyConflict,
    ConversationCommitOutcomeUnknown,
    ConversationReplayCorruptState,
    ConversationService,
)
from secagent.services.conversation_storage import (
    AttachmentStorageError,
    ConversationStorageService,
)


router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _get_turn_control_service(request: Request):
    from secagent.services.turn_control_service import TurnControlService

    return TurnControlService(
        session_factory=request.app.state.session_factory,
        queue=request.app.state.job_queue,
        logical_providers=request.app.state.model_router.logical_assignment_providers(),
        max_parallel=request.app.state.settings.max_parallel_subtasks_per_conversation,
    )


def get_conversation_service(request: Request) -> Iterator[ConversationService]:
    with request.app.state.session_factory() as writer_session:
        conversation_repository = ConversationRepository(writer_session)
        yield ConversationService(
            conversation_repository=conversation_repository,
            task_repository=TaskRepository(writer_session),
            event_writer=ConversationEventService(conversation_repository),
            audit_writer=AuditService(writer_session),
            storage=ConversationStorageService(request.app.state.settings),
            settings=request.app.state.settings,
            session_factory=request.app.state.session_factory,
            dag_queue=getattr(request.app.state, "job_queue", None),
        )


ServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
ActorDep = Annotated[AuthenticatedUser, Depends(current_user)]


@router.post("", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: ConversationCreate, service: ServiceDep, actor: ActorDep
) -> ConversationRead:
    return service.create(actor, payload)


@router.get("", response_model=list[ConversationRead])
def list_conversations(
    service: ServiceDep,
    actor: ActorDep,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ConversationRead]:
    return service.list(actor, limit=limit)


@router.get("/{conversation_id}", response_model=ConversationDetailRead)
def get_conversation(
    conversation_id: str,
    service: ServiceDep,
    actor: ActorDep,
    after_sequence: int = Query(default=0, ge=0),
    message_limit: int = Query(default=100, ge=1, le=500),
    after_plan_version: int = Query(default=0, ge=0),
    turn_limit: int = Query(default=100, ge=1, le=500),
) -> ConversationDetailRead:
    detail = service.get_detail(
        actor,
        conversation_id,
        after_sequence=after_sequence,
        message_limit=message_limit,
        after_plan_version=after_plan_version,
        turn_limit=turn_limit,
    )
    if detail is None:
        raise ConversationNotFound()
    return detail


@router.patch("/{conversation_id}", response_model=ConversationRead)
def patch_conversation(
    conversation_id: str,
    payload: ConversationPatch,
    service: ServiceDep,
    actor: ActorDep,
) -> ConversationRead:
    try:
        return service.patch(actor, conversation_id, payload)
    except KeyError:
        raise ConversationNotFound() from None


@router.delete("/{conversation_id}", response_model=ConversationRead)
def archive_conversation(
    conversation_id: str, service: ServiceDep, actor: ActorDep
) -> ConversationRead:
    try:
        return service.archive(actor, conversation_id)
    except KeyError:
        raise ConversationNotFound() from None


def _idempotency_key(request: Request) -> str:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1:
        raise InvalidIdempotencyKey()
    try:
        return TypeAdapter(IdempotencyKey).validate_python(values[0])
    except ValidationError:
        raise InvalidIdempotencyKey() from None


def _base_media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()


def _parse_json_submission(value: object) -> MessageSubmission:
    try:
        return MessageSubmission.model_validate(value)
    except ValidationError:
        raise ConversationRequestValidationError() from None


async def _json_submission(request: Request) -> MessageSubmission:
    try:
        raw = await request.body()
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        raise ConversationRequestValidationError() from None
    submission = _parse_json_submission(value)
    if submission.relative_paths:
        raise ConversationRequestValidationError()
    return submission


def _multipart_submission(form: FormData) -> tuple[MessageSubmission, list[UploadFile]]:
    payloads: list[str] = []
    uploads: list[UploadFile] = []
    for name, value in form.multi_items():
        if name == "payload" and isinstance(value, str):
            payloads.append(value)
        elif name == "files" and isinstance(value, UploadFile):
            uploads.append(value)
        else:
            raise ConversationRequestValidationError()
    if len(payloads) != 1:
        raise ConversationRequestValidationError()
    try:
        submission = MessageSubmission.model_validate_json(payloads[0])
    except (ValidationError, ValueError):
        raise ConversationRequestValidationError() from None
    return submission, uploads


async def _send(
    service: ConversationService,
    actor: AuthenticatedUser,
    conversation_id: str,
    submission: MessageSubmission,
    uploads: list[UploadFile],
    key: str,
) -> MessageSendRead:
    try:
        return await service.send_message(
            actor, conversation_id, submission, uploads, key
        )
    except KeyError:
        raise ConversationNotFound() from None
    except ArchivedConversationError:
        raise ApiError(409, "conversation_archived", "Conversation is archived") from None
    except MessageIdempotencyConflict:
        raise ApiError(
            409,
            "message_idempotency_conflict",
            "The idempotency key conflicts with another message",
        ) from None
    except AttachmentIdempotencyConflict:
        raise ApiError(
            409,
            "attachment_idempotency_conflict",
            "The attachment set conflicts with the original request",
        ) from None
    except ConversationReplayCorruptState:
        raise ApiError(
            409,
            "conversation_replay_corrupt",
            "The stored replay state is unavailable",
        ) from None
    except ConversationCommitOutcomeUnknown:
        raise ApiError(
            503,
            "conversation_commit_outcome_unknown",
            "Retry the request with the same idempotency key",
        ) from None
    except AttachmentStorageError:
        raise AttachmentValidationFailed() from None


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageSendRead,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: str,
    request: Request,
    response: Response,
    service: ServiceDep,
    actor: ActorDep,
) -> MessageSendRead:
    key = _idempotency_key(request)
    media_type = _base_media_type(request)
    if media_type == "application/json":
        result = await _send(
            service, actor, conversation_id, await _json_submission(request), [], key
        )
    elif media_type == "multipart/form-data":
        try:
            form = await request.form()
        except Exception:
            raise ConversationRequestValidationError() from None
        try:
            submission, uploads = _multipart_submission(form)
            result = await _send(
                service, actor, conversation_id, submission, uploads, key
            )
        finally:
            await form.close()
    else:
        raise UnsupportedConversationMediaType()
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/{conversation_id}/stop")
def stop_conversation_turn(
    conversation_id: str,
    service: ServiceDep,
    actor: ActorDep,
    control: Annotated[
        "TurnControlService", Depends(_get_turn_control_service)
    ],
) -> dict:
    try:
        final_status = control.request_stop(conversation_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except HTTPException:
        raise
    except Exception as exc:  # active-turn conflict surfaces as 409
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"conversation_id": conversation_id, "status": final_status}
