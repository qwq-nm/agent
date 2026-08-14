import asyncio
import json
from datetime import datetime, timezone

import pytest
import jwt

from secagent.api.events import stream_task_events
from secagent.auth.stream_tickets import (
    FakeTicketReplayStore,
    StreamTicketError,
    StreamTicketService,
)
from secagent.services.task_events import TaskEventService


def test_event_payload_is_recursively_redacted_and_bounded(repository) -> None:
    task = repository.list_tasks()[0] if repository.list_tasks() else None
    if task is None:
        from secagent.domain import TaskCreate

        task = repository.create_task(
            TaskCreate(goal="Stream events", authorization_scope="Owned task only")
        )
    service = TaskEventService(repository.session)

    event_id = service.append(
        task.id,
        "task.running",
        {"nested": {"api_key": "sk-secret-value"}, "attempt": 1},
    )
    event = service.after(task.id, 0)[0]

    assert event.id == event_id
    assert json.loads(event.payload_json) == {
        "nested": {"api_key": "***REDACTED***"},
        "attempt": 1,
    }
    with pytest.raises(ValueError, match="16 KiB"):
        service.append(task.id, "task.large", {"value": "x" * 20_000})


def test_events_resume_after_last_event(app, repository) -> None:
    from secagent.domain import TaskCreate

    task = repository.create_task(
        TaskCreate(goal="Resume events", authorization_scope="Owned task only")
    )
    service = TaskEventService(repository.session)
    first = service.append(task.id, "task.queued", {"attempt": 1})
    second = service.append(task.id, "task.running", {"attempt": 1})

    async def collect() -> list[str]:
        return [
            frame
            async for frame in stream_task_events(
                app.state.session_factory,
                task.id,
                first,
                is_disconnected=lambda: asyncio.sleep(0, result=False),
                poll_interval=0,
                max_polls=1,
            )
        ]

    frames = asyncio.run(collect())
    assert frames == [
        f'id: {second}\nevent: task.running\ndata: {{"attempt":1}}\n\n'
    ]


def test_stream_ticket_is_single_use_and_scoped(settings) -> None:
    store = FakeTicketReplayStore()
    service = StreamTicketService(settings.jwt_key(), store)
    ticket = service.issue("alice", "task-1")

    claims = service.consume(ticket, expected_user_id="alice", task_id="task-1")

    assert claims.sub == "alice"
    with pytest.raises(StreamTicketError, match="replay"):
        service.consume(ticket, expected_user_id="alice", task_id="task-1")


@pytest.mark.parametrize(
    ("user_id", "task_id"),
    [("bob", "task-1"), ("alice", "task-2")],
)
def test_stream_ticket_rejects_wrong_scope(settings, user_id, task_id) -> None:
    service = StreamTicketService(settings.jwt_key(), FakeTicketReplayStore())
    ticket = service.issue("alice", "task-1")

    with pytest.raises(StreamTicketError):
        service.consume(ticket, expected_user_id=user_id, task_id=task_id)


def test_stream_ticket_rejects_wrong_purpose(settings) -> None:
    ticket = jwt.encode(
        {
            "sub": "alice",
            "task_id": "task-1",
            "exp": 4_102_444_800,
            "jti": "not-secret-but-never-persist-this",
            "purpose": "access",
        },
        settings.jwt_key(),
        algorithm="HS256",
    )
    service = StreamTicketService(settings.jwt_key(), FakeTicketReplayStore())

    with pytest.raises(StreamTicketError, match="purpose"):
        service.consume(ticket, expected_user_id="alice", task_id="task-1")


def test_event_ticket_route_enforces_owner_and_replay(
    alice_client, bob_client, app, repository
) -> None:
    task = alice_client.post(
        "/api/tasks",
        json={"goal": "Follow progress", "authorization_scope": "Owned task only"},
    ).json()
    event_id = TaskEventService(repository.session).append(
        task["id"], "task.running", {"attempt": 1}
    )
    issued = alice_client.post(f"/api/tasks/{task['id']}/event-ticket")
    assert issued.status_code == 200
    assert set(issued.json()) == {"ticket", "expires_in"}
    ticket = issued.json()["ticket"]
    app.state.event_stream_max_polls = 1

    wrong_user = bob_client.get(
        f"/api/tasks/{task['id']}/events", params={"ticket": ticket}
    )
    assert wrong_user.status_code == 401
    assert set(wrong_user.json()) == {"error"}

    streamed = alice_client.get(
        f"/api/tasks/{task['id']}/events",
        params={"ticket": ticket},
        headers={"Last-Event-ID": "0"},
    )
    assert streamed.status_code == 200
    assert f"id: {event_id}" in streamed.text
    assert "event: task.running" in streamed.text

    replay = alice_client.get(
        f"/api/tasks/{task['id']}/events", params={"ticket": ticket}
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "unauthorized"


def test_task_state_transitions_emit_ordered_events(
    alice_client, repository
) -> None:
    task = alice_client.post(
        "/api/tasks",
        json={"goal": "Track lifecycle", "authorization_scope": "Owned task only"},
    ).json()
    queued = alice_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "events-run-1"},
    )
    paused = alice_client.post(f"/api/tasks/{task['id']}/pause")

    assert queued.status_code == 202
    assert paused.status_code == 200
    assert [
        event.event_type
        for event in TaskEventService(repository.session).after(task["id"], 0)
    ] == ["task.created", "task.queued", "task.paused"]


def test_invalid_last_event_id_does_not_consume_ticket(
    alice_client, app
) -> None:
    task = alice_client.post(
        "/api/tasks",
        json={"goal": "Validate cursor", "authorization_scope": "Owned task only"},
    ).json()
    ticket = alice_client.post(
        f"/api/tasks/{task['id']}/event-ticket"
    ).json()["ticket"]
    app.state.event_stream_max_polls = 1

    invalid = alice_client.get(
        f"/api/tasks/{task['id']}/events",
        params={"ticket": ticket},
        headers={"Last-Event-ID": "not-an-integer"},
    )
    valid = alice_client.get(
        f"/api/tasks/{task['id']}/events",
        params={"ticket": ticket},
    )

    assert invalid.status_code == 400
    assert valid.status_code == 200


def test_event_query_cursor_resumes_without_last_event_header(
    alice_client, app, repository
) -> None:
    task = alice_client.post(
        "/api/tasks",
        json={"goal": "Resume via query", "authorization_scope": "Owned task only"},
    ).json()
    first = TaskEventService(repository.session).append(task["id"], "task.queued", {})
    second = TaskEventService(repository.session).append(task["id"], "task.running", {})
    ticket = alice_client.post(f"/api/tasks/{task['id']}/event-ticket").json()["ticket"]
    app.state.event_stream_max_polls = 1

    streamed = alice_client.get(
        f"/api/tasks/{task['id']}/events", params={"ticket": ticket, "after": str(first)}
    )

    assert streamed.status_code == 200
    assert f"id: {second}" in streamed.text
    assert f"id: {first}" not in streamed.text


def test_task_detail_includes_worker_runtime(alice_client, repository) -> None:
    task = alice_client.post(
        "/api/tasks",
        json={"goal": "Show worker runtime", "authorization_scope": "Owned task only"},
    ).json()
    job = repository.add_job_run(task["id"], "worker-runtime-1", attempt=3)
    job.status = "queued"
    job.worker_id = "worker-1"
    job.heartbeat_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
    repository.commit()

    detail = alice_client.get(f"/api/tasks/{task['id']}").json()

    assert detail["queue_position"] == 1
    assert detail["job_attempt"] == 3
    assert detail["worker_id"] == "worker-1"
    assert detail["worker_heartbeat_at"] == "2026-08-15T00:00:00Z"
