import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from secagent.api.errors import ApiError
from secagent.auth.dependencies import AuthenticatedUser, auth_service, require_admin
from secagent.db_models import JobRunRow
from secagent.domain import UserRole
from secagent.repository import TaskRepository
from secagent.services.auth_service import (
    AuthService,
    LastActiveAdminError,
    UserAlreadyExistsError,
    UserLimitReachedError,
    UserNotFoundError,
)


router = APIRouter(prefix="/api/admin", tags=["admin"])
AdminDep = Annotated[AuthenticatedUser, Depends(require_admin)]
AuthServiceDep = Annotated[AuthService, Depends(auth_service)]


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=256)
    role: UserRole


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)

    @model_validator(mode="after")
    def require_change(self) -> "UserUpdateRequest":
        if self.is_active is None and self.password is None:
            raise ValueError("is_active or password is required")
        return self


class AdminUserRead(BaseModel):
    id: str
    username: str
    role: UserRole
    is_active: bool


class AuditEventRead(BaseModel):
    id: int
    actor_id: str | None
    actor_username: str | None = None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    details: dict
    created_at: datetime


@router.get("/users", response_model=list[AdminUserRead])
def list_users(actor: AdminDep, service: AuthServiceDep) -> list[AdminUserRead]:
    del actor
    return [
        AdminUserRead.model_validate(row, from_attributes=True)
        for row in service.list_users()
    ]


@router.get("/workers")
def worker_summary(request: Request, actor: AdminDep) -> dict[str, int]:
    del actor
    with request.app.state.session_factory() as session:
        active = int(
            session.scalar(
                select(func.count())
                .select_from(JobRunRow)
                .where(JobRunRow.status == "running")
            )
            or 0
        )
        queued = int(
            session.scalar(
                select(func.count())
                .select_from(JobRunRow)
                .where(
                    JobRunRow.status.in_(("pending_publish", "publishing", "queued"))
                )
            )
            or 0
        )
    online = _online_workers(request.app.state.job_queue)
    return {
        "online": online,
        "active": active,
        "capacity": request.app.state.settings.worker_concurrency,
        "queued": queued,
    }


def _online_workers(queue: object) -> int:
    try:
        from secagent.queue.celery_app import celery

        stats = celery.control.inspect(timeout=0.2).stats() or {}
        return len(stats)
    except Exception:
        del queue
        return 0


@router.post(
    "/users", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED
)
def create_user(
    payload: UserCreateRequest,
    actor: AdminDep,
    service: AuthServiceDep,
) -> AdminUserRead:
    try:
        service.create_user(
            payload.username,
            payload.password,
            payload.role,
            actor_id=actor.id,
        )
    except UserAlreadyExistsError:
        raise ApiError(
            409, "username_conflict", "Username already exists"
        ) from None
    except UserLimitReachedError:
        raise ApiError(
            409, "user_limit_reached", "The team user limit has been reached"
        ) from None
    row = service.repository.get_user_by_username(payload.username)
    if row is None:
        raise ApiError(500, "internal_error", "Internal server error")
    return AdminUserRead.model_validate(row, from_attributes=True)


@router.patch("/users/{user_id}", response_model=AdminUserRead)
def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    actor: AdminDep,
    service: AuthServiceDep,
) -> AdminUserRead:
    try:
        row = service.update_user(
            user_id,
            actor_id=actor.id,
            is_active=payload.is_active,
            password=payload.password,
        )
    except UserNotFoundError:
        raise ApiError(404, "not_found", "User not found") from None
    except LastActiveAdminError:
        raise ApiError(
            409,
            "last_active_admin",
            "The last active administrator cannot be disabled",
        ) from None
    return AdminUserRead.model_validate(row, from_attributes=True)


@router.get("/audit-events", response_model=list[AuditEventRead])
def list_audit_events(
    actor: AdminDep,
    service: AuthServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: Annotated[int | None, Query(ge=1)] = None,
    actor_filter: Annotated[str | None, Query(alias="actor", max_length=80)] = None,
    action: Annotated[str | None, Query(max_length=120)] = None,
    outcome: Annotated[str | None, Query(max_length=32)] = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> list[AuditEventRead]:
    del actor
    users = service.list_users()
    actor_names = {user.id: user.username for user in users}
    actor_ids = None
    if actor_filter:
        needle = actor_filter.casefold()
        actor_ids = {
            user.id
            for user in users
            if user.id == actor_filter or needle in user.username.casefold()
        }
    rows = TaskRepository(service.session).list_audit_events(
        limit=limit,
        before=before,
        actor_ids=actor_ids,
        action=action,
        outcome=outcome,
        created_after=created_after,
        created_before=created_before,
    )
    return [
        AuditEventRead(
            id=row.id,
            actor_id=row.actor_id,
            actor_username=actor_names.get(row.actor_id),
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            outcome=row.outcome,
            details=json.loads(row.details_json),
            created_at=row.created_at,
        )
        for row in rows
    ]
