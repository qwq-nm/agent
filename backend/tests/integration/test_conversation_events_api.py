import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from secagent.api import conversation_events as conversation_events_module
from secagent.api.conversation_events import stream_conversation_events
from secagent.auth.dependencies import AuthenticatedUser
from secagent.auth import stream_tickets as stream_ticket_module
from secagent.auth.stream_tickets import ConversationStreamTicketService
from secagent.conversation_domain import ConversationCreate, ConversationEventCreate
from secagent.conversation_repository import ConversationRepository
from secagent.db import Base
from secagent.db_models import ConversationRow, UserRow
from secagent.domain import UserRole
from secagent.main import create_app
from secagent.queue.fake import FakeJobQueue


def _assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("configuration", ["absent", "unreadable", "malformed"])
def test_real_signing_configuration_failure_maps_only_event_ticket_to_stream_503(
    settings, tmp_path: Path, configuration: str
) -> None:
    secret_file = None
    if configuration == "unreadable":
        secret_file = tmp_path / "missing-signing-key"
    elif configuration == "malformed":
        secret_file = tmp_path / "malformed-signing-key"
        secret_file.write_bytes(b"\xff\xfe\xfa")
    candidate = settings.model_copy(
        update={"jwt_signing_key": None, "jwt_signing_key_file": secret_file}
    )
    application = create_app(candidate, job_queue=FakeJobQueue())
    Base.metadata.create_all(application.state.session_factory.kw["bind"])

    with TestClient(application) as test_client:
        headers = {"Authorization": "Bearer syntactically-valid-credential"}
        issuance = test_client.post(
            "/api/conversations/00000000-0000-0000-0000-000000000099/event-ticket",
            headers=headers,
        )
        conversation_rest = test_client.get("/api/conversations", headers=headers)
        legacy_task = test_client.get("/api/tasks", headers=headers)

    _assert_error(issuance, 503, "event_stream_unavailable")
    _assert_error(conversation_rest, 503, "service_unavailable")
    _assert_error(legacy_task, 503, "service_unavailable")


def test_conversation_stream_generator_emits_exact_canonical_resumable_frames(
    app, seeded_analyst
) -> None:
    actor = AuthenticatedUser(
        id=seeded_analyst.id,
        username=seeded_analyst.username,
        role=UserRole(seeded_analyst.role),
    )
    with app.state.session_factory() as session:
        repository = ConversationRepository(session)
        conversation = repository.create_conversation(actor, ConversationCreate())
        first = repository.append_event(
            actor,
            conversation.id,
            ConversationEventCreate(event_type="first", payload={"z": "中文", "a": 1}),
        )
        second = repository.append_event(
            actor,
            conversation.id,
            ConversationEventCreate(event_type="second", payload={"ok": True}),
        )

    async def collect(cursor: int) -> list[str]:
        return [
            frame
            async for frame in stream_conversation_events(
                app.state.session_factory,
                actor,
                conversation.id,
                cursor,
                is_disconnected=lambda: False,
                poll_interval=0,
                max_polls=1,
            )
        ]

    assert asyncio.run(collect(first.cursor)) == [
        f'id: {second.cursor}\nevent: second\ndata: {{"ok":true}}\n\n'
    ]
    assert asyncio.run(collect(second.cursor)) == []


def test_conversation_stream_generator_heartbeat_and_disconnect(
    app, seeded_analyst
) -> None:
    actor = AuthenticatedUser(
        id=seeded_analyst.id,
        username=seeded_analyst.username,
        role=UserRole(seeded_analyst.role),
    )
    with app.state.session_factory() as session:
        conversation = ConversationRepository(session).create_conversation(
            actor, ConversationCreate()
        )

    async def heartbeat() -> list[str]:
        return [
            frame
            async for frame in stream_conversation_events(
                app.state.session_factory,
                actor,
                conversation.id,
                0,
                is_disconnected=lambda: asyncio.sleep(0, result=False),
                poll_interval=0,
                heartbeat_interval=0,
                max_polls=1,
            )
        ]

    disconnected_calls = 0

    def disconnected() -> bool:
        nonlocal disconnected_calls
        disconnected_calls += 1
        return True

    async def disconnect() -> list[str]:
        return [
            frame
            async for frame in stream_conversation_events(
                app.state.session_factory,
                actor,
                conversation.id,
                0,
                is_disconnected=disconnected,
                poll_interval=0,
                max_polls=3,
            )
        ]

    assert asyncio.run(heartbeat()) == [": heartbeat\n\n"]
    assert asyncio.run(disconnect()) == []
    assert disconnected_calls == 1


def test_stream_generator_uses_fresh_closed_session_per_poll_and_sleeps(
    monkeypatch,
) -> None:
    sessions: list[object] = []
    closed: list[object] = []
    calls: list[tuple[object, int, int]] = []
    sleeps: list[float] = []

    class Context:
        def __init__(self) -> None:
            self.session = object()
            sessions.append(self.session)

        def __enter__(self):
            return self.session

        def __exit__(self, *_args) -> None:
            closed.append(self.session)

    responses = [
        [SimpleNamespace(cursor=7, event_type="typed.event", payload={"b": 2, "a": 1})],
        [],
    ]

    def events_after(self, _actor, _conversation_id, *, cursor, limit):
        calls.append((self.session, cursor, limit))
        return responses[len(calls) - 1]

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ConversationRepository, "events_after", events_after)
    monkeypatch.setattr(conversation_events_module.asyncio, "sleep", sleep)
    actor = AuthenticatedUser(id="user-1", username="alice", role=UserRole.ANALYST)

    async def collect() -> list[str]:
        return [
            frame
            async for frame in stream_conversation_events(
                Context,
                actor,
                "conversation-1",
                0,
                is_disconnected=lambda: False,
                poll_interval=0.25,
                heartbeat_interval=1_000,
                max_polls=2,
            )
        ]

    assert asyncio.run(collect()) == [
        'id: 7\nevent: typed.event\ndata: {"a":1,"b":2}\n\n'
    ]
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert closed == sessions
    assert calls == [(sessions[0], 0, 1_000), (sessions[1], 7, 1_000)]
    assert sleeps == [0.25]


def test_ticket_issuance_stream_resume_cursor_precedence_and_single_use(
    alice_client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    sent = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "create event", "relative_paths": []},
        headers={"Idempotency-Key": "event-source-1"},
    ).json()
    cursor = sent["message"]["sequence"]
    issued = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    )
    assert issued.status_code == 200
    assert set(issued.json()) == {"ticket", "expires_in"}
    assert issued.json()["expires_in"] == 60
    ticket = issued.json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    streamed = alice_client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket, "after": "0"},
        headers={"Last-Event-ID": "999999"},
    )
    assert streamed.status_code == 200
    assert "event: conversation.message.created" in streamed.text
    assert f"id: {cursor}" in streamed.text

    replay = alice_client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )
    _assert_error(replay, 401, "invalid_stream_ticket")


def test_last_event_id_header_resumes_when_query_cursor_is_absent(
    alice_client, client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    sent = []
    for index in range(2):
        sent.append(
            alice_client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={"content": f"event {index + 1}", "relative_paths": []},
                headers={"Idempotency-Key": f"header-resume-{index + 1}"},
            ).json()
        )
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    streamed = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
        headers={"Last-Event-ID": str(sent[0]["message"]["sequence"])},
    )

    assert streamed.status_code == 200
    assert f"id: {sent[1]['message']['sequence']}" in streamed.text
    assert f"id: {sent[0]['message']['sequence']}\n" not in streamed.text


def test_invalid_cursor_and_invalid_bearer_do_not_burn_ticket(
    alice_client, client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    invalid_cursor = alice_client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket, "after": "+1"},
    )
    invalid_bearer = alice_client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
        headers={"Authorization": "Basic abc"},
    )
    ticket_only = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    _assert_error(invalid_cursor, 400, "invalid_event_cursor")
    _assert_error(invalid_bearer, 401, "invalid_stream_ticket")
    assert ticket_only.status_code == 200


@pytest.mark.parametrize(
    ("source", "raw_cursor"),
    [
        ("query", "9" * 5_000),
        ("header", str(2**63)),
    ],
)
def test_decimal_cursor_beyond_db_bigint_is_safe_and_burns_normally(
    alice_client, client, app, monkeypatch, source: str, raw_cursor: str
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1
    observed: list[int] = []

    def no_events(self, _actor, _conversation_id, *, cursor, limit):
        assert limit == 1_000
        observed.append(cursor)
        return []

    monkeypatch.setattr(ConversationRepository, "events_after", no_events)
    params = {"ticket": ticket}
    headers = None
    if source == "query":
        params["after"] = raw_cursor
    else:
        headers = {"Last-Event-ID": raw_cursor}

    streamed = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params=params,
        headers=headers,
    )
    replay = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    assert streamed.status_code == 200
    assert observed == [2**63 - 1]
    _assert_error(replay, 401, "invalid_stream_ticket")


def test_conversation_ticket_owner_admin_forbidden_and_missing(
    alice_client, bob_client, admin_client
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    _assert_error(
        bob_client.post(f"/api/conversations/{conversation['id']}/event-ticket"),
        403,
        "forbidden",
    )
    assert (
        admin_client.post(
            f"/api/conversations/{conversation['id']}/event-ticket"
        ).status_code
        == 200
    )
    _assert_error(
        alice_client.post(
            "/api/conversations/00000000-0000-0000-0000-000000000099/event-ticket"
        ),
        404,
        "conversation_not_found",
    )


def test_admin_ticket_can_read_an_owned_conversation_stream(
    alice_client, admin_client, client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    sent = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "admin visible", "relative_paths": []},
        headers={"Idempotency-Key": "admin-stream"},
    ).json()
    ticket = admin_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    streamed = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    assert streamed.status_code == 200
    assert f"id: {sent['message']['sequence']}" in streamed.text


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer alpha beta"},
        [
            ("Authorization", "Bearer first"),
            ("Authorization", "Bearer second"),
        ],
    ],
)
def test_malformed_or_duplicate_authorization_does_not_burn_ticket(
    alice_client, client, app, headers
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    invalid = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
        headers=headers,
    )
    valid = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    _assert_error(invalid, 401, "invalid_stream_ticket")
    assert valid.status_code == 200


def test_wrong_valid_bearer_scope_does_not_burn_and_matching_bearer_consumes(
    alice_client, bob_client, client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    path = f"/api/conversations/{conversation['id']}/events"
    first_ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    wrong_user = client.get(
        path,
        params={"ticket": first_ticket},
        headers={"Authorization": bob_client.headers["Authorization"]},
    )
    ticket_only = client.get(path, params={"ticket": first_ticket})

    _assert_error(wrong_user, 401, "invalid_stream_ticket")
    assert ticket_only.status_code == 200

    second_ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    matching = client.get(
        path,
        params={"ticket": second_ticket},
        headers={"Authorization": alice_client.headers["Authorization"]},
    )
    assert matching.status_code == 200


def test_cryptographically_invalid_bearer_does_not_burn_ticket(
    alice_client, client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    invalid = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
        headers={"Authorization": "Bearer abc.def.ghi"},
    )
    valid = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    _assert_error(invalid, 401, "invalid_stream_ticket")
    assert valid.status_code == 200


def test_authentication_configuration_failure_does_not_burn_ticket(
    alice_client, client, app, tmp_path: Path
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1
    original_key = app.state.settings.jwt_signing_key
    original_file = app.state.settings.jwt_signing_key_file
    app.state.settings.jwt_signing_key = None
    app.state.settings.jwt_signing_key_file = tmp_path / "missing-jwt-key"
    try:
        unavailable = client.get(
            f"/api/conversations/{conversation['id']}/events",
            params={"ticket": ticket},
            headers={"Authorization": alice_client.headers["Authorization"]},
        )
    finally:
        app.state.settings.jwt_signing_key = original_key
        app.state.settings.jwt_signing_key_file = original_file

    valid = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )
    _assert_error(unavailable, 503, "event_stream_unavailable")
    assert valid.status_code == 200


def test_wrong_resource_scope_does_not_burn_ticket(alice_client, client, app) -> None:
    first = alice_client.post("/api/conversations", json={}).json()
    second = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{first['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    wrong = client.get(
        f"/api/conversations/{second['id']}/events",
        params={"ticket": ticket},
    )
    valid = client.get(
        f"/api/conversations/{first['id']}/events",
        params={"ticket": ticket},
    )

    _assert_error(wrong, 401, "invalid_stream_ticket")
    assert valid.status_code == 200


def test_post_consume_missing_and_forbidden_conversations_burn_tickets(
    alice_client, client, app, seeded_bob
) -> None:
    missing = alice_client.post("/api/conversations", json={}).json()
    missing_ticket = alice_client.post(
        f"/api/conversations/{missing['id']}/event-ticket"
    ).json()["ticket"]
    with app.state.session_factory() as session:
        session.delete(session.get(ConversationRow, missing["id"]))
        session.commit()

    first_missing = client.get(
        f"/api/conversations/{missing['id']}/events",
        params={"ticket": missing_ticket},
    )
    replay_missing = client.get(
        f"/api/conversations/{missing['id']}/events",
        params={"ticket": missing_ticket},
    )
    _assert_error(first_missing, 404, "conversation_not_found")
    _assert_error(replay_missing, 401, "invalid_stream_ticket")

    forbidden = alice_client.post("/api/conversations", json={}).json()
    forbidden_ticket = alice_client.post(
        f"/api/conversations/{forbidden['id']}/event-ticket"
    ).json()["ticket"]
    with app.state.session_factory() as session:
        row = session.get(ConversationRow, forbidden["id"])
        row.owner_id = seeded_bob.id
        session.commit()

    first_forbidden = client.get(
        f"/api/conversations/{forbidden['id']}/events",
        params={"ticket": forbidden_ticket},
    )
    replay_forbidden = client.get(
        f"/api/conversations/{forbidden['id']}/events",
        params={"ticket": forbidden_ticket},
    )
    _assert_error(first_forbidden, 403, "forbidden")
    _assert_error(replay_forbidden, 401, "invalid_stream_ticket")


def test_inactive_ticket_subject_burns_ticket(alice_client, client, app) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1
    with app.state.session_factory() as session:
        user = session.get(UserRow, conversation["owner_id"])
        user.is_active = False
        session.commit()

    inactive = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )
    with app.state.session_factory() as session:
        user = session.get(UserRow, conversation["owner_id"])
        user.is_active = True
        session.commit()
    replay = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    _assert_error(inactive, 401, "invalid_stream_ticket")
    _assert_error(replay, 401, "invalid_stream_ticket")


def test_inactive_ticket_subject_with_matching_bearer_burns_ticket(
    alice_client, client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1
    with app.state.session_factory() as session:
        user = session.get(UserRow, conversation["owner_id"])
        user.is_active = False
        session.commit()

    inactive = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
        headers={"Authorization": alice_client.headers["Authorization"]},
    )
    with app.state.session_factory() as session:
        user = session.get(UserRow, conversation["owner_id"])
        user.is_active = True
        session.commit()
    replay = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    _assert_error(inactive, 401, "invalid_stream_ticket")
    _assert_error(replay, 401, "invalid_stream_ticket")


def test_archived_conversation_stream_remains_readable(alice_client, client, app) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    sent = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "persisted archived event", "relative_paths": []},
        headers={"Idempotency-Key": "archive-stream-1"},
    ).json()
    assert alice_client.delete(f"/api/conversations/{conversation['id']}").status_code == 200
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]
    app.state.conversation_event_stream_max_polls = 1

    streamed = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    assert streamed.status_code == 200
    assert f"id: {sent['message']['sequence']}" in streamed.text


class _FailingReplayStore:
    def __init__(self) -> None:
        self.calls = 0

    def consume(self, _jti_hash: str, _ttl_seconds: int) -> bool:
        self.calls += 1
        raise RuntimeError("redis://secret-host/replay")


class _ReplayBeforeApply(RuntimeError):
    pass


class _ReplayOutcomeUnknown(RuntimeError):
    pass


class _SemanticFailingReplayStore:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0
        self.applied_before_error = False

    def consume(self, _jti_hash: str, _ttl_seconds: int) -> bool:
        self.calls += 1
        if isinstance(self.error, _ReplayOutcomeUnknown):
            self.applied_before_error = True
        raise self.error


@pytest.mark.parametrize(
    ("error", "applied_before_error"),
    [
        (_ReplayBeforeApply("before-apply-secret"), False),
        (_ReplayOutcomeUnknown("outcome-unknown-secret"), True),
    ],
)
def test_distinct_replay_store_exceptions_are_503_without_automatic_retry(
    alice_client, client, app, error: Exception, applied_before_error: bool
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    store = _SemanticFailingReplayStore(error)
    service = ConversationStreamTicketService(app.state.settings.jwt_key(), store)
    ticket = service.issue(conversation["owner_id"], conversation["id"])
    app.state.conversation_stream_ticket_service = service

    response = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )

    _assert_error(response, 503, "event_stream_unavailable")
    assert store.calls == 1
    assert store.applied_before_error is applied_before_error
    assert ticket not in response.text
    assert str(error) not in response.text


def test_ticket_infrastructure_failures_are_sanitized_503(
    alice_client, client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    app.state.conversation_stream_ticket_service = ConversationStreamTicketService(
        None, _FailingReplayStore()
    )
    issuance = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    )
    _assert_error(issuance, 503, "event_stream_unavailable")
    assert "secret-host" not in issuance.text

    store = _FailingReplayStore()
    app.state.conversation_stream_ticket_service = ConversationStreamTicketService(
        app.state.settings.jwt_key(), store
    )
    ticket = app.state.conversation_stream_ticket_service.issue(
        conversation["owner_id"], conversation["id"]
    )
    consume = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )
    _assert_error(consume, 503, "event_stream_unavailable")
    assert store.calls == 1
    assert ticket not in consume.text
    assert "secret-host" not in consume.text


def test_jwt_encode_and_decode_failures_map_to_sanitized_route_503(
    alice_client, client, app, monkeypatch
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()

    def fail_encode(*_args, **_kwargs):
        raise RuntimeError("encode-private-secret")

    monkeypatch.setattr(stream_ticket_module.jwt, "encode", fail_encode)
    issuance = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    )
    _assert_error(issuance, 503, "event_stream_unavailable")
    assert "encode-private-secret" not in issuance.text

    monkeypatch.undo()
    ticket = alice_client.post(
        f"/api/conversations/{conversation['id']}/event-ticket"
    ).json()["ticket"]

    def fail_decode(*_args, **_kwargs):
        raise RuntimeError("decode-private-secret")

    monkeypatch.setattr(stream_ticket_module.jwt, "decode", fail_decode)
    consumption = client.get(
        f"/api/conversations/{conversation['id']}/events",
        params={"ticket": ticket},
    )
    _assert_error(consumption, 503, "event_stream_unavailable")
    assert ticket not in consumption.text
    assert "decode-private-secret" not in consumption.text
