import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from secagent.auth.dependencies import AuthenticatedUser, current_user
from secagent.auth.stream_tickets import StreamTicketError, StreamTicketService
from secagent.db_models import UserRow
from secagent.domain import UserRole
from secagent.repository import TaskRepository
from secagent.services.task_events import TaskEventService

router = APIRouter(prefix="/api/tasks", tags=["task-events"])


async def stream_task_events(
    session_factory,
    task_id: str,
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
            events = TaskEventService(session).after(task_id, cursor)
        for event in events:
            cursor = event.id
            last_output = time.monotonic()
            yield (
                f"id: {event.id}\n"
                f"event: {event.event_type}\n"
                f"data: {event.payload_json}\n\n"
            )
        if not events and time.monotonic() - last_output >= heartbeat_interval:
            last_output = time.monotonic()
            yield ": heartbeat\n\n"
        polls += 1
        if max_polls is None or polls < max_polls:
            await asyncio.sleep(poll_interval)


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("Last-Event-ID", "0")
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")
    return value


@router.post("/{task_id}/event-ticket")
def create_event_ticket(
    task_id: str,
    request: Request,
    actor: AuthenticatedUser = Depends(current_user),
) -> dict:
    with request.app.state.session_factory() as session:
        repository = TaskRepository(session)
        if repository.get_authorized(task_id, actor) is None:
            raise HTTPException(status_code=404, detail="task not found")
    try:
        ticket = request.app.state.stream_ticket_service.issue(actor.id, task_id)
    except StreamTicketError as exc:
        raise HTTPException(status_code=503, detail="Event streaming unavailable") from exc
    return {"ticket": ticket, "expires_in": 60}


@router.get("/{task_id}/events")
def task_events(
    task_id: str,
    request: Request,
    ticket: str = Query(min_length=1),
) -> StreamingResponse:
    last_event_id = _last_event_id(request)
    expected_user_id = None
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        from secagent.services.auth_service import AuthService, AuthenticationError

        with request.app.state.session_factory() as session:
            try:
                expected_user_id = AuthService.from_session(
                    session, request.app.state.settings
                ).authenticate_access(authorization[7:]).id
            except AuthenticationError as exc:
                raise HTTPException(status_code=401, detail="Invalid stream ticket") from exc
    try:
        claims = request.app.state.stream_ticket_service.consume(
            ticket,
            task_id=task_id,
            expected_user_id=expected_user_id,
        )
    except StreamTicketError as exc:
        raise HTTPException(status_code=401, detail="Invalid stream ticket") from exc
    with request.app.state.session_factory() as session:
        user = session.get(UserRow, claims.sub)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid stream ticket")
        actor = AuthenticatedUser(
            id=user.id,
            username=user.username,
            role=UserRole(user.role),
        )
        if TaskRepository(session).get_authorized(task_id, actor) is None:
            raise HTTPException(status_code=404, detail="task not found")
    return StreamingResponse(
        stream_task_events(
            request.app.state.session_factory,
            task_id,
            last_event_id,
            is_disconnected=request.is_disconnected,
            max_polls=getattr(request.app.state, "event_stream_max_polls", None),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
