from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from secagent.api.errors import (
    ApiError,
    ConversationNotFound,
    EventStreamUnavailable,
    InvalidStreamTicket,
)
from secagent.auth.dependencies import AuthenticatedUser
from secagent.auth.stream_tickets import (
    ConversationStreamTicketError,
    ConversationStreamUnavailable,
)
from secagent.auth.tokens import TokenValidationError, decode_access_token
from secagent.conversation_domain import canonical_json_dumps
from secagent.conversation_repository import ConversationRepository
from secagent.db_models import UserRow
from secagent.domain import UserRole
from secagent.services.auth_service import (
    AuthenticationConfigurationError,
    AuthenticationError,
    AuthService,
)


router = APIRouter(prefix="/api/conversations", tags=["conversation-events"])
_CURSOR_RE = re.compile(r"^[0-9]+$")
_BEARER_RE = re.compile(r"^Bearer[ \t]+([A-Za-z0-9\-._~+/]+=*)$", re.IGNORECASE)
_EVENT_TICKET_BEARER = HTTPBearer(auto_error=False)
_MAX_EVENT_CURSOR = 2**63 - 1
_MAX_EVENT_CURSOR_TEXT = str(_MAX_EVENT_CURSOR)


async def stream_conversation_events(
    session_factory,
    actor: AuthenticatedUser,
    conversation_id: str,
    last_event_id: int,
    *,
    is_disconnected: Callable[[], Awaitable[bool] | bool],
    poll_interval: float = 1.0,
    heartbeat_interval: float = 15.0,
    max_polls: int | None = None,
) -> AsyncIterator[str]:
    cursor = last_event_id
    last_output = time.monotonic()
    polls = 0
    while max_polls is None or polls < max_polls:
        disconnected = is_disconnected()
        if inspect.isawaitable(disconnected):
            disconnected = await disconnected
        if disconnected:
            return
        with session_factory() as session:
            events = ConversationRepository(session).events_after(
                actor, conversation_id, cursor=cursor, limit=1_000
            )
        for event in events:
            last_output = time.monotonic()
            yield (
                f"id: {event.cursor}\n"
                f"event: {event.event_type}\n"
                f"data: {canonical_json_dumps(event.payload)}\n\n"
            )
            cursor = event.cursor
        if not events and time.monotonic() - last_output >= heartbeat_interval:
            last_output = time.monotonic()
            yield ": heartbeat\n\n"
        polls += 1
        if max_polls is None or polls < max_polls:
            await asyncio.sleep(poll_interval)


def _event_cursor(request: Request, after: str | None) -> int:
    if after is not None:
        raw = after
    else:
        values = request.headers.getlist("last-event-id")
        if len(values) > 1:
            raise ApiError(400, "invalid_event_cursor", "Invalid event cursor")
        raw = values[0] if values else "0"
    if _CURSOR_RE.fullmatch(raw) is None:
        raise ApiError(400, "invalid_event_cursor", "Invalid event cursor")
    normalized = raw.lstrip("0") or "0"
    if len(normalized) > len(_MAX_EVENT_CURSOR_TEXT) or (
        len(normalized) == len(_MAX_EVENT_CURSOR_TEXT)
        and normalized > _MAX_EVENT_CURSOR_TEXT
    ):
        return _MAX_EVENT_CURSOR
    return int(normalized, 10)


def _optional_bearer(request: Request) -> str | None:
    values = request.headers.getlist("authorization")
    if not values:
        return None
    if len(values) != 1:
        raise InvalidStreamTicket()
    match = _BEARER_RE.fullmatch(values[0])
    if match is None:
        raise InvalidStreamTicket()
    return match.group(1)


def _authenticate_optional_bearer(request: Request) -> str | None:
    credential = _optional_bearer(request)
    if credential is None:
        return None
    try:
        signing_key = request.app.state.settings.jwt_key()
    except (OSError, ValueError):
        raise EventStreamUnavailable() from None
    if signing_key is None:
        raise EventStreamUnavailable()
    try:
        return decode_access_token(credential, signing_key).sub
    except TokenValidationError:
        raise InvalidStreamTicket() from None


def _event_ticket_actor(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_EVENT_TICKET_BEARER)
    ],
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with request.app.state.session_factory() as session:
        try:
            user = AuthService.from_session(
                session, request.app.state.settings
            ).authenticate_access(credentials.credentials)
        except AuthenticationError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        except AuthenticationConfigurationError:
            raise EventStreamUnavailable() from None
    return AuthenticatedUser.model_validate(user.model_dump())


@router.post("/{conversation_id}/event-ticket")
def create_conversation_event_ticket(
    conversation_id: str,
    request: Request,
    actor: Annotated[AuthenticatedUser, Depends(_event_ticket_actor)],
) -> dict[str, str | int]:
    with request.app.state.session_factory() as session:
        if ConversationRepository(session).get_conversation(actor, conversation_id) is None:
            raise ConversationNotFound()
    try:
        ticket = request.app.state.conversation_stream_ticket_service.issue(
            actor.id, conversation_id
        )
    except ConversationStreamUnavailable:
        raise EventStreamUnavailable() from None
    return {"ticket": ticket, "expires_in": 60}


@router.get("/{conversation_id}/events")
def conversation_events(
    conversation_id: str,
    request: Request,
    ticket: Annotated[str, Query(min_length=1)],
    after: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    cursor = _event_cursor(request, after)
    expected_user_id = _authenticate_optional_bearer(request)
    try:
        claims = request.app.state.conversation_stream_ticket_service.consume(
            ticket,
            conversation_id=conversation_id,
            expected_user_id=expected_user_id,
        )
    except ConversationStreamTicketError:
        raise InvalidStreamTicket() from None
    except ConversationStreamUnavailable:
        raise EventStreamUnavailable() from None

    with request.app.state.session_factory() as session:
        user = session.get(UserRow, claims.sub)
        if user is None or not user.is_active:
            raise InvalidStreamTicket()
        actor = AuthenticatedUser(
            id=user.id,
            username=user.username,
            role=UserRole(user.role),
        )
        if ConversationRepository(session).get_conversation(
            actor, conversation_id
        ) is None:
            raise ConversationNotFound()

    return StreamingResponse(
        stream_conversation_events(
            request.app.state.session_factory,
            actor,
            conversation_id,
            cursor,
            is_disconnected=request.is_disconnected,
            max_polls=getattr(
                request.app.state,
                "conversation_event_stream_max_polls",
                None,
            ),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
