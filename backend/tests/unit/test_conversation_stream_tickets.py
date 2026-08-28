import hashlib
import io
import logging
from datetime import datetime, timezone

import jwt
import pytest

from secagent.auth.stream_tickets import (
    ConversationStreamTicketError,
    ConversationStreamTicketService,
    ConversationStreamUnavailable,
    FakeTicketReplayStore,
    StreamTicketError,
    StreamTicketService,
)
from secagent.main import create_app
from secagent.queue.fake import FakeJobQueue
from secagent.security.access_log import (
    TicketQueryRedactionFilter,
    install_access_log_redaction,
)


class RecordingReplayStore:
    def __init__(self, result=True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def consume(self, jti_hash: str, ttl_seconds: int) -> bool:
        self.calls.append((jti_hash, ttl_seconds))
        if self.error is not None:
            raise self.error
        return self.result


def test_conversation_ticket_has_strict_claims_ttl_and_hashes_jti(settings) -> None:
    store = RecordingReplayStore()
    now = datetime(2035, 8, 27, 12, 0, tzinfo=timezone.utc)
    service = ConversationStreamTicketService(
        settings.jwt_key(), store, now=lambda: now
    )

    ticket = service.issue("user-1", "conversation-1")
    decoded = jwt.decode(
        ticket,
        settings.jwt_key(),
        algorithms=["HS256"],
        options={"verify_exp": False},
    )

    assert set(decoded) == {"sub", "conversation_id", "exp", "jti", "purpose"}
    assert decoded["purpose"] == "conversation_events"
    assert decoded["exp"] == int(now.timestamp()) + 60

    claims = service.consume(
        ticket,
        conversation_id="conversation-1",
        expected_user_id="user-1",
    )
    assert claims.sub == "user-1"
    assert store.calls == [(hashlib.sha256(decoded["jti"].encode()).hexdigest(), 60)]
    assert decoded["jti"] not in store.calls[0][0]

    second = jwt.decode(
        service.issue("user-1", "conversation-1"),
        settings.jwt_key(),
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert second["jti"] != decoded["jti"]


@pytest.mark.parametrize(
    ("conversation_id", "expected_user_id"),
    [("conversation-2", "user-1"), ("conversation-1", "user-2")],
)
def test_wrong_conversation_scope_does_not_burn_ticket(
    settings, conversation_id, expected_user_id
) -> None:
    store = RecordingReplayStore()
    service = ConversationStreamTicketService(settings.jwt_key(), store)
    ticket = service.issue("user-1", "conversation-1")

    with pytest.raises(ConversationStreamTicketError):
        service.consume(
            ticket,
            conversation_id=conversation_id,
            expected_user_id=expected_user_id,
        )

    assert store.calls == []


def test_conversation_ticket_rejects_non_strict_claim_types_before_burn(settings) -> None:
    store = RecordingReplayStore()
    ticket = jwt.encode(
        {
            "sub": "user-1",
            "conversation_id": "conversation-1",
            "exp": "4102444800",
            "jti": "strict-claims-jti",
            "purpose": "conversation_events",
        },
        settings.jwt_key(),
        algorithm="HS256",
    )
    service = ConversationStreamTicketService(settings.jwt_key(), store)

    with pytest.raises(ConversationStreamTicketError):
        service.consume(
            ticket,
            conversation_id="conversation-1",
            expected_user_id="user-1",
        )

    assert store.calls == []


@pytest.mark.parametrize("case", ["expired", "missing", "extra", "bad-signature"])
def test_invalid_crypto_and_required_claims_do_not_burn_ticket(settings, case) -> None:
    store = RecordingReplayStore()
    payload = {
        "sub": "user-1",
        "conversation_id": "conversation-1",
        "exp": 4_102_444_800,
        "jti": "invalid-claims-jti",
        "purpose": "conversation_events",
    }
    key = settings.jwt_key()
    if case == "expired":
        payload["exp"] = 1
    elif case == "missing":
        payload.pop("jti")
    elif case == "extra":
        payload["unexpected"] = "not-allowed"
    elif case == "bad-signature":
        key = "different-signing-key-at-least-32-bytes"
    ticket = jwt.encode(payload, key, algorithm="HS256")

    with pytest.raises(ConversationStreamTicketError):
        ConversationStreamTicketService(settings.jwt_key(), store).consume(
            ticket,
            conversation_id="conversation-1",
            expected_user_id="user-1",
        )

    assert store.calls == []


def test_task_and_conversation_tickets_are_not_cross_accepted(settings) -> None:
    store = FakeTicketReplayStore()
    task_service = StreamTicketService(settings.jwt_key(), store)
    conversation_service = ConversationStreamTicketService(settings.jwt_key(), store)
    task_ticket = task_service.issue("user-1", "task-1")
    conversation_ticket = conversation_service.issue("user-1", "conversation-1")

    with pytest.raises(ConversationStreamTicketError):
        conversation_service.consume(
            task_ticket,
            conversation_id="conversation-1",
            expected_user_id="user-1",
        )
    with pytest.raises(StreamTicketError):
        task_service.consume(
            conversation_ticket,
            task_id="task-1",
            expected_user_id="user-1",
        )


def test_conversation_ticket_distinguishes_invalid_and_infrastructure(settings) -> None:
    unavailable = ConversationStreamTicketService(None, RecordingReplayStore())
    with pytest.raises(ConversationStreamUnavailable):
        unavailable.issue("user-1", "conversation-1")

    failing_store = RecordingReplayStore(error=RuntimeError("redis-secret"))
    service = ConversationStreamTicketService(settings.jwt_key(), failing_store)
    ticket = service.issue("user-1", "conversation-1")
    with pytest.raises(ConversationStreamUnavailable) as caught:
        service.consume(ticket, conversation_id="conversation-1")
    assert "redis-secret" not in str(caught.value)
    assert len(failing_store.calls) == 1


def test_conversation_ticket_wrong_purpose_and_replay_false_are_invalid(settings) -> None:
    wrong_purpose_store = RecordingReplayStore()
    wrong_purpose = jwt.encode(
        {
            "sub": "user-1",
            "conversation_id": "conversation-1",
            "exp": 4_102_444_800,
            "jti": "wrong-purpose-jti",
            "purpose": "task_events",
        },
        settings.jwt_key(),
        algorithm="HS256",
    )
    service = ConversationStreamTicketService(settings.jwt_key(), wrong_purpose_store)

    with pytest.raises(ConversationStreamTicketError):
        service.consume(wrong_purpose, conversation_id="conversation-1")
    assert wrong_purpose_store.calls == []

    replay_store = RecordingReplayStore(result=False)
    replay_service = ConversationStreamTicketService(settings.jwt_key(), replay_store)
    ticket = replay_service.issue("user-1", "conversation-1")
    with pytest.raises(ConversationStreamTicketError):
        replay_service.consume(ticket, conversation_id="conversation-1")
    assert len(replay_store.calls) == 1


def test_conversation_ticket_wraps_jwt_encode_and_decode_infrastructure_failures(
    settings, monkeypatch
) -> None:
    service = ConversationStreamTicketService(settings.jwt_key(), RecordingReplayStore())
    monkeypatch.setattr(jwt, "encode", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("encode-secret")))
    with pytest.raises(ConversationStreamUnavailable) as encode_error:
        service.issue("user-1", "conversation-1")
    assert "encode-secret" not in str(encode_error.value)

    monkeypatch.undo()
    store = RecordingReplayStore()
    service = ConversationStreamTicketService(settings.jwt_key(), store)
    ticket = service.issue("user-1", "conversation-1")
    monkeypatch.setattr(jwt, "decode", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("decode-secret")))
    with pytest.raises(ConversationStreamUnavailable) as decode_error:
        service.consume(ticket, conversation_id="conversation-1")
    assert "decode-secret" not in str(decode_error.value)
    assert store.calls == []


def test_access_log_filter_redacts_plain_repeated_and_encoded_ticket_names() -> None:
    raw_ticket = "eyJ.secret.signature"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "GET",
            "/api/conversations/c/events?ticket="
            + raw_ticket
            + "&%74icket=second&ticket=third&not_ticket=keep",
            "1.1",
            200,
        ),
        None,
    )

    assert TicketQueryRedactionFilter().filter(record)
    rendered = record.getMessage()

    assert raw_ticket not in rendered
    assert "second" not in rendered
    assert "third" not in rendered
    assert rendered.count("[REDACTED]") == 3
    assert "not_ticket=keep" in rendered


def test_configured_uvicorn_access_logger_redacts_an_issued_jwt(settings) -> None:
    ticket = ConversationStreamTicketService(
        settings.jwt_key(), RecordingReplayStore()
    ).issue("user-1", "conversation-1")
    logger = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    original_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        install_access_log_redaction()
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            f"/api/conversations/conversation-1/events?ticket={ticket}",
            "1.1",
            200,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)

    rendered = stream.getvalue()
    assert ticket not in rendered
    assert "[REDACTED]" in rendered


def test_create_app_shares_replay_store_and_tolerates_absent_signing_key(
    settings,
) -> None:
    candidate = settings.model_copy(
        update={"jwt_signing_key": None, "jwt_signing_key_file": None}
    )
    app = create_app(candidate, job_queue=FakeJobQueue())

    assert app.state.stream_ticket_service.signing_key is None
    assert app.state.conversation_stream_ticket_service.signing_key is None
    assert (
        app.state.stream_ticket_service.replay_store
        is app.state.conversation_stream_ticket_service.replay_store
    )


@pytest.mark.parametrize("malformed", [False, True])
def test_create_app_tolerates_unreadable_or_malformed_signing_file(
    settings, tmp_path, malformed
) -> None:
    secret_file = tmp_path / "jwt-signing-key"
    if malformed:
        secret_file.write_bytes(b"\xff\xfe\xfa")
    candidate = settings.model_copy(
        update={"jwt_signing_key": None, "jwt_signing_key_file": secret_file}
    )

    app = create_app(candidate, job_queue=FakeJobQueue())

    assert app.state.stream_ticket_service.signing_key is None
    assert app.state.conversation_stream_ticket_service.signing_key is None
