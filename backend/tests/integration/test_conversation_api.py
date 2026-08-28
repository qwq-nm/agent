import io
import json
from pathlib import Path
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.datastructures import UploadFile
from starlette.requests import Request

from secagent.api.conversations import get_conversation_service
from secagent.api.errors import ForbiddenResource
from secagent.conversation_repository import MessageIdempotencyConflict
from secagent.db_models import (
    AuditEventRow,
    ConversationEventRow,
    ConversationMessageRow,
    ConversationTurnRow,
    MessageAttachmentRow,
    TaskRow,
)
from secagent.services.conversation_service import (
    ArchivedConversationError,
    AttachmentIdempotencyConflict,
    ConversationCommitOutcomeUnknown,
    ConversationReplayCorruptState,
    ConversationService,
)
from secagent.services.conversation_storage import AttachmentStorageError


def _assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert set(response.json()["error"]) == {
        "code",
        "message",
        "fields",
        "trace_id",
    }


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/api/conversations", {"json": {}}),
        ("get", "/api/conversations", {}),
        ("get", "/api/conversations/00000000-0000-0000-0000-000000000001", {}),
        (
            "patch",
            "/api/conversations/00000000-0000-0000-0000-000000000001",
            {"json": {"title": "renamed"}},
        ),
        (
            "delete",
            "/api/conversations/00000000-0000-0000-0000-000000000001",
            {},
        ),
        (
            "post",
            "/api/conversations/00000000-0000-0000-0000-000000000001/messages",
            {
                "json": {"content": "hello", "relative_paths": []},
                "headers": {"Idempotency-Key": "request-1"},
            },
        ),
        (
            "post",
            "/api/conversations/00000000-0000-0000-0000-000000000001/event-ticket",
            {},
        ),
    ],
)
def test_every_authenticated_conversation_route_rejects_anonymous(
    client, method, path, kwargs
) -> None:
    _assert_error(getattr(client, method)(path, **kwargs), 401, "unauthorized")


def test_owner_admin_crud_missing_forbidden_archive_and_strict_validation(
    alice_client, bob_client, admin_client
) -> None:
    created = alice_client.post(
        "/api/conversations",
        json={
            "title": "First investigation",
            "settings": {
                "authorization_scope": "Only the supplied evidence",
                "safety_mode": "conservative",
                "allowed_targets": ["https://example.test"],
                "requested_parallelism": 2,
            },
        },
    )
    assert created.status_code == 201
    conversation = created.json()
    assert conversation["title"] == "First investigation"
    assert alice_client.get("/api/conversations").json()[0]["id"] == conversation["id"]
    assert admin_client.get(f"/api/conversations/{conversation['id']}").status_code == 200

    _assert_error(
        bob_client.get(f"/api/conversations/{conversation['id']}"), 403, "forbidden"
    )
    _assert_error(
        alice_client.get(
            "/api/conversations/00000000-0000-0000-0000-000000000001"
        ),
        404,
        "conversation_not_found",
    )
    _assert_error(
        alice_client.post("/api/conversations", json={"unknown": "value"}),
        422,
        "validation_error",
    )
    _assert_error(
        alice_client.get("/api/conversations", params={"limit": 0}),
        422,
        "validation_error",
    )

    patched = alice_client.patch(
        f"/api/conversations/{conversation['id']}", json={"title": "Renamed"}
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed"

    first_archive = alice_client.delete(f"/api/conversations/{conversation['id']}")
    second_archive = alice_client.delete(f"/api/conversations/{conversation['id']}")
    assert first_archive.status_code == second_archive.status_code == 200
    assert first_archive.json()["status"] == second_archive.json()["status"] == "archived"
    detail = alice_client.get(f"/api/conversations/{conversation['id']}")
    assert detail.status_code == 200
    assert detail.json()["conversation"]["status"] == "archived"


def test_json_message_first_send_and_exact_replay_have_one_durable_graph(
    alice_client, app, fake_queue
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    path = f"/api/conversations/{conversation['id']}/messages"
    payload = {"content": "Analyze the supplied evidence", "relative_paths": []}

    first = alice_client.post(
        path, json=payload, headers={"Idempotency-Key": "  exact-key  "}
    )
    replay = alice_client.post(
        path, json=payload, headers={"Idempotency-Key": "  exact-key  "}
    )

    assert first.status_code == 201
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    assert fake_queue.enqueued == []
    with app.state.session_factory() as session:
        counts = {
            "messages": session.scalar(select(func.count()).select_from(ConversationMessageRow)),
            "tasks": session.scalar(select(func.count()).select_from(TaskRow)),
            "turns": session.scalar(select(func.count()).select_from(ConversationTurnRow)),
            "events": session.scalar(select(func.count()).select_from(ConversationEventRow)),
            "audits": session.scalar(
                select(func.count())
                .select_from(AuditEventRow)
                .where(AuditEventRow.action == "conversation.message.send")
            ),
        }
    assert counts == {"messages": 1, "tasks": 1, "turns": 1, "events": 1, "audits": 1}


@pytest.mark.parametrize("value", [None, "", "   ", "x" * 256])
def test_message_requires_strict_idempotency_key(alice_client, value) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    headers = {} if value is None else {"Idempotency-Key": value}
    response = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "hello", "relative_paths": []},
        headers=headers,
    )
    _assert_error(response, 400, "invalid_idempotency_key")
    assert value in (None, "") or value not in response.text


def test_multipart_preserves_file_order_metadata_and_bytes(
    alice_client, app
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    path = f"/api/conversations/{conversation['id']}/messages"
    data = {
        "payload": '{"content":"inspect attachments","relative_paths":["a/one.txt","b/two.txt"]}'
    }
    files = [
        ("files", ("one.txt", b"first bytes", "text/plain")),
        ("files", ("two.txt", b"second bytes", "text/plain")),
    ]
    headers = {"Idempotency-Key": "multipart-1"}

    first = alice_client.post(path, data=data, files=files, headers=headers)
    replay = alice_client.post(path, data=data, files=files, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    attachments = first.json()["attachments"]
    assert [item["original_name"] for item in attachments] == ["one.txt", "two.txt"]
    assert [item["relative_path"] for item in attachments] == [
        "a/one.txt",
        "b/two.txt",
    ]
    assert [
        (Path(app.state.settings.data_dir) / item["storage_ref"]).read_bytes()
        for item in attachments
    ] == [b"first bytes", b"second bytes"]
    assert {
        path.relative_to(app.state.settings.data_dir).as_posix()
        for path in Path(app.state.settings.data_dir).rglob("*")
        if path.is_file()
    } == {item["storage_ref"] for item in attachments}
    with app.state.session_factory() as session:
        counts = {
            "messages": session.scalar(
                select(func.count()).select_from(ConversationMessageRow)
            ),
            "attachments": session.scalar(
                select(func.count()).select_from(MessageAttachmentRow)
            ),
            "tasks": session.scalar(select(func.count()).select_from(TaskRow)),
            "turns": session.scalar(
                select(func.count()).select_from(ConversationTurnRow)
            ),
            "events": session.scalar(
                select(func.count()).select_from(ConversationEventRow)
            ),
            "audits": session.scalar(
                select(func.count())
                .select_from(AuditEventRow)
                .where(AuditEventRow.action == "conversation.message.send")
            ),
        }
        attachment_ids = set(
            session.scalars(select(MessageAttachmentRow.id)).all()
        )
    assert counts == {
        "messages": 1,
        "attachments": 2,
        "tasks": 1,
        "turns": 1,
        "events": 1,
        "audits": 1,
    }
    assert attachment_ids == {item["id"] for item in attachments}


@pytest.mark.parametrize(
    ("content", "headers", "expected_code"),
    [
        (b'{"content":"x","relative_paths":["file.txt"]}', {"content-type": "application/json"}, "validation_error"),
        (b"plain", {"content-type": "text/plain"}, "unsupported_media_type"),
        (b'{"content":', {"content-type": "application/json"}, "validation_error"),
    ],
)
def test_message_transport_errors_are_sanitized(
    alice_client, content, headers, expected_code
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    headers = headers | {"Idempotency-Key": "do-not-echo-this-key"}
    response = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        content=content,
        headers=headers,
    )

    _assert_error(response, 422 if expected_code == "validation_error" else 415, expected_code)
    assert "do-not-echo-this-key" not in response.text


def test_detail_pagination_is_ordered_and_archive_preserves_history(
    alice_client,
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    path = f"/api/conversations/{conversation['id']}/messages"
    for index in range(3):
        response = alice_client.post(
            path,
            json={"content": f"message {index + 1}", "relative_paths": []},
            headers={"Idempotency-Key": f"pagination-{index + 1}"},
        )
        assert response.status_code == 201

    page = alice_client.get(
        f"/api/conversations/{conversation['id']}",
        params={
            "after_sequence": 1,
            "message_limit": 1,
            "after_plan_version": 1,
            "turn_limit": 1,
        },
    ).json()
    assert [item["message"]["sequence"] for item in page["messages"]] == [2]
    assert [item["plan_version"] for item in page["turns"]] == [2]

    assert alice_client.delete(f"/api/conversations/{conversation['id']}").status_code == 200
    archived = alice_client.get(f"/api/conversations/{conversation['id']}").json()
    assert [item["message"]["sequence"] for item in archived["messages"]] == [1, 2, 3]
    assert [item["plan_version"] for item in archived["turns"]] == [1, 2, 3]


@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (KeyError("secret-key"), 404, "conversation_not_found"),
        (ArchivedConversationError("secret-content"), 409, "conversation_archived"),
        (
            MessageIdempotencyConflict("secret-idempotency-key"),
            409,
            "message_idempotency_conflict",
        ),
        (
            AttachmentIdempotencyConflict("secret-filename.txt"),
            409,
            "attachment_idempotency_conflict",
        ),
        (
            ConversationReplayCorruptState("secret-database-state"),
            409,
            "conversation_replay_corrupt",
        ),
        (
            ConversationCommitOutcomeUnknown("secret-database-url"),
            503,
            "conversation_commit_outcome_unknown",
        ),
        (
            AttachmentStorageError("secret/path/client-file.txt"),
            422,
            "attachment_validation_failed",
        ),
        (ForbiddenResource("conversation"), 403, "forbidden"),
    ],
)
def test_every_message_service_error_has_a_sanitized_stable_mapping(
    alice_client, app, exception, status, code
) -> None:
    class RaisingService:
        calls = 0

        async def send_message(self, *_args, **_kwargs):
            self.calls += 1
            raise exception

    service = RaisingService()
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        response = alice_client.post(
            "/api/conversations/00000000-0000-0000-0000-000000000099/messages",
            json={"content": "client-secret-content", "relative_paths": []},
            headers={"Idempotency-Key": "client-secret-key"},
        )
    finally:
        app.dependency_overrides.pop(get_conversation_service, None)

    _assert_error(response, status, code)
    assert service.calls == 1
    for secret in (
        "secret-key",
        "secret-content",
        "secret-idempotency-key",
        "secret-filename.txt",
        "secret-database-state",
        "secret-database-url",
        "secret/path/client-file.txt",
        "client-secret-content",
        "client-secret-key",
    ):
        assert secret not in response.text
    if code == "conversation_commit_outcome_unknown":
        assert "same idempotency key" in response.json()["error"]["message"].lower()


@pytest.mark.parametrize(
    "files",
    [
        [
            ("payload", (None, '{"content":"one","relative_paths":[]}')),
            ("payload", (None, '{"content":"two","relative_paths":[]}')),
        ],
        [("files", ("only.txt", b"bytes", "text/plain"))],
        [
            ("payload", ("payload.json", b"{}", "application/json")),
            ("files", ("accepted.txt", b"accepted", "text/plain")),
        ],
        [
            ("files", (None, "not-an-upload")),
            ("unknown", ("rejected.txt", b"rejected", "text/plain")),
            ("files", ("accepted.txt", b"accepted", "text/plain")),
        ],
    ],
)
def test_rejected_multipart_structures_close_every_returned_upload(
    alice_client, monkeypatch, files
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    closed: list[str | None] = []
    original_close = UploadFile.close

    async def tracked_close(self) -> None:
        closed.append(self.filename)
        await original_close(self)

    monkeypatch.setattr(UploadFile, "close", tracked_close)
    response = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        files=files,
        headers={"Idempotency-Key": "rejected-form"},
    )

    _assert_error(response, 422, "validation_error")
    expected_uploads = [item[1][0] for item in files if item[1][0] is not None]
    assert closed == expected_uploads


def test_success_replay_dto_failure_and_mapped_failure_close_all_uploads(
    alice_client, app, monkeypatch
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    path = f"/api/conversations/{conversation['id']}/messages"
    closed: list[str | None] = []
    original_close = UploadFile.close

    async def tracked_close(self) -> None:
        closed.append(self.filename)
        await original_close(self)

    monkeypatch.setattr(UploadFile, "close", tracked_close)

    def multipart(key: str, payload: str):
        return alice_client.post(
            path,
            files=[
                ("payload", (None, payload)),
                ("files", ("one.txt", b"first", "text/plain")),
                ("files", ("two.txt", b"second", "text/plain")),
            ],
            headers={"Idempotency-Key": key},
        )

    payload = json.dumps(
        {"content": "close uploads", "relative_paths": ["one.txt", "two.txt"]}
    )
    first = multipart("close-success", payload)
    replay = multipart("close-success", payload)
    invalid = multipart("close-invalid", "not-json")

    class RaisingService:
        async def send_message(self, *_args, **_kwargs):
            raise AttachmentStorageError("private-storage-path")

    app.dependency_overrides[get_conversation_service] = lambda: RaisingService()
    try:
        mapped = multipart("close-mapped", payload)
    finally:
        app.dependency_overrides.pop(get_conversation_service, None)

    assert first.status_code == 201
    assert replay.status_code == 200
    _assert_error(invalid, 422, "validation_error")
    _assert_error(mapped, 422, "attachment_validation_failed")
    assert closed == ["one.txt", "two.txt"] * 4


@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (KeyError("private"), 404, "conversation_not_found"),
        (ArchivedConversationError("private"), 409, "conversation_archived"),
        (
            MessageIdempotencyConflict("private"),
            409,
            "message_idempotency_conflict",
        ),
        (
            AttachmentIdempotencyConflict("private"),
            409,
            "attachment_idempotency_conflict",
        ),
        (
            ConversationReplayCorruptState("private"),
            409,
            "conversation_replay_corrupt",
        ),
        (
            ConversationCommitOutcomeUnknown("private"),
            503,
            "conversation_commit_outcome_unknown",
        ),
        (
            AttachmentStorageError("private"),
            422,
            "attachment_validation_failed",
        ),
        (ForbiddenResource("conversation"), 403, "forbidden"),
    ],
)
def test_every_mapped_multipart_service_result_closes_all_uploads(
    alice_client, app, monkeypatch, exception, status, code
) -> None:
    class RaisingService:
        async def send_message(self, *_args, **_kwargs):
            raise exception

    closed: list[str | None] = []
    original_close = UploadFile.close

    async def tracked_close(self) -> None:
        closed.append(self.filename)
        await original_close(self)

    monkeypatch.setattr(UploadFile, "close", tracked_close)
    app.dependency_overrides[get_conversation_service] = lambda: RaisingService()
    try:
        response = alice_client.post(
            "/api/conversations/00000000-0000-0000-0000-000000000099/messages",
            files=[
                (
                    "payload",
                    (None, '{"content":"mapped","relative_paths":["x.txt"]}'),
                ),
                ("files", ("x.txt", b"private", "text/plain")),
            ],
            headers={"Idempotency-Key": "mapped-multipart-close"},
        )
    finally:
        app.dependency_overrides.pop(get_conversation_service, None)

    _assert_error(response, status, code)
    assert closed == ["x.txt"]


def test_unexpected_service_failure_still_closes_form_uploads(
    app, seeded_analyst, monkeypatch
) -> None:
    class RaisingService:
        async def send_message(self, *_args, **_kwargs):
            raise RuntimeError("unexpected-private-detail")

    app.dependency_overrides[get_conversation_service] = lambda: RaisingService()
    closed: list[str | None] = []
    original_close = UploadFile.close

    async def tracked_close(self) -> None:
        closed.append(self.filename)
        await original_close(self)

    monkeypatch.setattr(UploadFile, "close", tracked_close)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": seeded_analyst.username, "password": "Correct-Horse-9"},
            )
            client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
            response = client.post(
                "/api/conversations/00000000-0000-0000-0000-000000000099/messages",
                files=[
                    (
                        "payload",
                        (None, '{"content":"unexpected","relative_paths":["x.txt"]}'),
                    ),
                    ("files", ("x.txt", b"x", "text/plain")),
                ],
                headers={"Idempotency-Key": "unexpected-close"},
            )
    finally:
        app.dependency_overrides.pop(get_conversation_service, None)

    _assert_error(response, 500, "internal_error")
    assert "unexpected-private-detail" not in response.text
    assert closed == ["x.txt"]


def test_parser_before_return_failure_uses_framework_cleanup_and_sanitized_422(
    alice_client, monkeypatch
) -> None:
    temporary = SpooledTemporaryFile()
    upload = UploadFile(temporary, filename="framework-owned-secret.txt")

    async def parser_failure(_request):
        await upload.close()
        raise ValueError("parser-private-detail")

    monkeypatch.setattr(Request, "form", parser_failure)
    response = alice_client.post(
        "/api/conversations/00000000-0000-0000-0000-000000000099/messages",
        content=b"broken multipart",
        headers={
            "Content-Type": "multipart/form-data; boundary=broken",
            "Idempotency-Key": "parser-before-return",
        },
    )

    _assert_error(response, 422, "validation_error")
    assert temporary.closed
    assert "framework-owned-secret.txt" not in response.text
    assert "parser-private-detail" not in response.text


def _unsafe_zip_bytes() -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("../escape.txt", "secret")
    return output.getvalue()


@pytest.mark.parametrize(
    ("payload", "files"),
    [
        ('{"content":"count","relative_paths":["one.txt"]}', []),
        (
            '{"content":"filename","relative_paths":["safe.txt"]}',
            [("files", ("../unsafe.txt", b"secret", "text/plain"))],
        ),
        (
            '{"content":"zip","relative_paths":["unsafe.zip"]}',
            [("files", ("unsafe.zip", _unsafe_zip_bytes(), "application/zip"))],
        ),
    ],
)
def test_attachment_count_path_and_zip_failures_are_sanitized(
    alice_client, payload, files
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    response = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        files=[("payload", (None, payload)), *files],
        headers={"Idempotency-Key": "attachment-failure"},
    )

    _assert_error(response, 422, "attachment_validation_failed")
    for secret in ("unsafe.txt", "unsafe.zip", "escape.txt", "secret"):
        assert secret not in response.text


def test_zero_file_multipart_first_send_and_exact_replay(alice_client) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    path = f"/api/conversations/{conversation['id']}/messages"
    files = [
        (
            "payload",
            (None, '{"content":"multipart text only","relative_paths":[]}'),
        )
    ]
    headers = {"Idempotency-Key": "multipart-zero-files"}

    first = alice_client.post(path, files=files, headers=headers)
    replay = alice_client.post(path, files=files, headers=headers)

    assert first.status_code == 201
    assert first.json()["attachments"] == []
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}


def test_attachment_size_failure_is_sanitized(alice_client, app) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    original_limit = app.state.settings.max_attachment_total_bytes
    app.state.settings.max_attachment_total_bytes = 1
    try:
        response = alice_client.post(
            f"/api/conversations/{conversation['id']}/messages",
            files=[
                (
                    "payload",
                    (None, '{"content":"size","relative_paths":["large.txt"]}'),
                ),
                ("files", ("large.txt", b"private bytes", "text/plain")),
            ],
            headers={"Idempotency-Key": "attachment-size"},
        )
    finally:
        app.state.settings.max_attachment_total_bytes = original_limit

    _assert_error(response, 422, "attachment_validation_failed")
    assert "large.txt" not in response.text
    assert "private bytes" not in response.text


@pytest.mark.parametrize(
    ("content_type", "expected_status"),
    [
        ("APPLICATION/JSON; Charset=UTF-8", 201),
        ("application/json-suffix", 415),
        ("prefix-application/json", 415),
        ("multipart/form-data-suffix", 415),
    ],
)
def test_message_media_type_comparison_is_exact_and_case_insensitive(
    alice_client, content_type, expected_status
) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    response = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        content=b'{"content":"media type","relative_paths":[]}',
        headers={
            "Content-Type": content_type,
            "Idempotency-Key": f"media-{expected_status}-{content_type}",
        },
    )

    assert response.status_code == expected_status
    if expected_status == 415:
        _assert_error(response, 415, "unsupported_media_type")


def test_duplicate_idempotency_header_is_rejected(alice_client) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    response = alice_client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "duplicate header", "relative_paths": []},
        headers=[
            ("Idempotency-Key", "first-key"),
            ("Idempotency-Key", "second-key"),
        ],
    )
    _assert_error(response, 400, "invalid_idempotency_key")
    assert "first-key" not in response.text
    assert "second-key" not in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
    ],
)
def test_conversation_list_query_boundaries(alice_client, params) -> None:
    _assert_error(
        alice_client.get("/api/conversations", params=params),
        422,
        "validation_error",
    )


@pytest.mark.parametrize(
    "params",
    [
        {"after_sequence": -1},
        {"message_limit": 0},
        {"message_limit": 501},
        {"after_plan_version": -1},
        {"turn_limit": 0},
        {"turn_limit": 501},
    ],
)
def test_conversation_detail_query_boundaries(alice_client, params) -> None:
    conversation = alice_client.post("/api/conversations", json={}).json()
    _assert_error(
        alice_client.get(
            f"/api/conversations/{conversation['id']}", params=params
        ),
        422,
        "validation_error",
    )


def test_request_scoped_service_composition_has_one_clean_fresh_writer(
    alice_client, app, monkeypatch
) -> None:
    original_factory = app.state.session_factory

    class TrackingFactory:
        def __init__(self) -> None:
            self.sessions = []
            self.closed: set[int] = set()

        def __call__(self):
            session = original_factory()
            self.sessions.append(session)
            original_close = session.close

            def tracked_close() -> None:
                self.closed.add(id(session))
                original_close()

            session.close = tracked_close
            return session

    tracking = TrackingFactory()
    app.state.session_factory = tracking
    seen_writers: list[int] = []

    def inspect_service(service: ConversationService) -> None:
        writer = service.session
        assert service.conversation_repository.session is writer
        assert service.task_repository.session is writer
        assert service.event_writer.session is writer
        assert service.audit_writer.session is writer
        assert service.session_factory is tracking
        assert not writer.in_transaction()
        assert not writer.new
        assert not writer.dirty
        assert not writer.deleted
        assert any(session is not writer for session in tracking.sessions)
        seen_writers.append(id(writer))

    original_create = ConversationService.create
    original_patch = ConversationService.patch
    original_archive = ConversationService.archive
    original_send = ConversationService.send_message

    def checked_create(self, *args, **kwargs):
        inspect_service(self)
        return original_create(self, *args, **kwargs)

    def checked_patch(self, *args, **kwargs):
        inspect_service(self)
        return original_patch(self, *args, **kwargs)

    def checked_archive(self, *args, **kwargs):
        inspect_service(self)
        return original_archive(self, *args, **kwargs)

    async def checked_send(self, *args, **kwargs):
        inspect_service(self)
        return await original_send(self, *args, **kwargs)

    monkeypatch.setattr(ConversationService, "create", checked_create)
    monkeypatch.setattr(ConversationService, "patch", checked_patch)
    monkeypatch.setattr(ConversationService, "archive", checked_archive)
    monkeypatch.setattr(ConversationService, "send_message", checked_send)
    try:
        created = alice_client.post("/api/conversations", json={})
        conversation_id = created.json()["id"]
        patched = alice_client.patch(
            f"/api/conversations/{conversation_id}", json={"title": "Tracked"}
        )
        sent = alice_client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "tracked send", "relative_paths": []},
            headers={"Idempotency-Key": "tracked-send"},
        )
        archived = alice_client.delete(f"/api/conversations/{conversation_id}")
    finally:
        app.state.session_factory = original_factory

    assert [created.status_code, patched.status_code, sent.status_code, archived.status_code] == [
        201,
        200,
        201,
        200,
    ]
    assert len(seen_writers) == 4
    assert len(set(seen_writers)) == 4
    assert set(seen_writers) <= tracking.closed


def test_request_writer_closes_on_mapped_exception_and_response_validation_failure(
    alice_client, app, monkeypatch
) -> None:
    original_factory = app.state.session_factory

    class TrackingFactory:
        def __init__(self) -> None:
            self.closed: set[int] = set()

        def __call__(self):
            session = original_factory()
            original_close = session.close

            def tracked_close() -> None:
                self.closed.add(id(session))
                original_close()

            session.close = tracked_close
            return session

    tracking = TrackingFactory()
    writer_ids: list[int] = []

    async def mapped_failure(self, *_args, **_kwargs):
        writer_ids.append(id(self.session))
        raise ArchivedConversationError("private-exception")

    def invalid_response(self, *_args, **_kwargs):
        writer_ids.append(id(self.session))
        return {"private": "invalid-response"}

    monkeypatch.setattr(ConversationService, "send_message", mapped_failure)
    monkeypatch.setattr(ConversationService, "create", invalid_response)
    app.state.session_factory = tracking
    try:
        mapped = alice_client.post(
            "/api/conversations/00000000-0000-0000-0000-000000000099/messages",
            json={"content": "mapped teardown", "relative_paths": []},
            headers={"Idempotency-Key": "mapped-teardown"},
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            client.headers["Authorization"] = alice_client.headers["Authorization"]
            invalid = client.post("/api/conversations", json={})
    finally:
        app.state.session_factory = original_factory

    _assert_error(mapped, 409, "conversation_archived")
    _assert_error(invalid, 500, "internal_error")
    assert len(writer_ids) == 2
    assert set(writer_ids) <= tracking.closed
    assert "private-exception" not in mapped.text
    assert "invalid-response" not in invalid.text


def test_all_new_routes_make_zero_queue_provider_or_model_calls(
    alice_client, client, app, fake_queue, monkeypatch
) -> None:
    calls: list[str] = []

    def unexpected_runtime_build(*_args, **_kwargs):
        calls.append("provider_runtime_factory.build")
        raise AssertionError("conversation route built provider runtime")

    async def unexpected_model_complete(*_args, **_kwargs):
        calls.append("model_router.complete")
        raise AssertionError("conversation route called model router")

    async def unexpected_provider_complete(*_args, **_kwargs):
        calls.append("provider.complete")
        raise AssertionError("conversation route called provider")

    monkeypatch.setattr(
        app.state.provider_runtime_factory, "build", unexpected_runtime_build
    )
    monkeypatch.setattr(app.state.model_router, "complete", unexpected_model_complete)
    for provider in app.state.model_router.providers.values():
        monkeypatch.setattr(provider, "complete", unexpected_provider_complete)

    created = alice_client.post("/api/conversations", json={})
    conversation_id = created.json()["id"]
    listed = alice_client.get("/api/conversations")
    detail = alice_client.get(f"/api/conversations/{conversation_id}")
    patched = alice_client.patch(
        f"/api/conversations/{conversation_id}", json={"title": "No model"}
    )
    sent = alice_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "No provider call", "relative_paths": []},
        headers={"Idempotency-Key": "zero-provider-model"},
    )
    issued = alice_client.post(
        f"/api/conversations/{conversation_id}/event-ticket"
    )
    app.state.conversation_event_stream_max_polls = 1
    streamed = client.get(
        f"/api/conversations/{conversation_id}/events",
        params={"ticket": issued.json()["ticket"]},
    )
    archived = alice_client.delete(f"/api/conversations/{conversation_id}")

    assert [
        created.status_code,
        listed.status_code,
        detail.status_code,
        patched.status_code,
        sent.status_code,
        issued.status_code,
        streamed.status_code,
        archived.status_code,
    ] == [201, 200, 200, 200, 201, 200, 200, 200]
    assert calls == []
    assert fake_queue.enqueued == []
