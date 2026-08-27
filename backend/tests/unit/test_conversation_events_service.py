from types import SimpleNamespace
from uuid import uuid4

import pytest

from secagent.auth.dependencies import AuthenticatedUser
from secagent.conversation_domain import ConversationEventRead
from secagent.domain import UserRole
from secagent.services.conversation_events import (
    MAX_CONVERSATION_EVENT_PAYLOAD_BYTES,
    ConversationEventService,
    encode_redacted_event_payload,
)


def _uuid() -> str:
    return str(uuid4())


class RecordingRepository:
    def __init__(self) -> None:
        self.session = object()
        self.calls = []

    def append_event(self, actor, conversation_id, payload, *, commit=True):
        self.calls.append((actor, conversation_id, payload, commit))
        return SimpleNamespace(
            cursor=1,
            conversation_id=conversation_id,
            event_type=payload.event_type,
            payload=payload.payload,
            turn_id=payload.turn_id,
            subtask_id=payload.subtask_id,
            created_at="unused",
        )


def test_encoder_deeply_redacts_then_uses_canonical_json() -> None:
    encoded = encode_redacted_event_payload(
        {
            "z": [{"api_key": "sk-secret-value", "note": "Bearer abcdef"}],
            "a": {"nested": {"token": "raw-token"}},
        }
    )
    assert encoded == (
        '{"a":{"nested":{"token":"***REDACTED***"}},'
        '"z":[{"api_key":"***REDACTED***","note":"***REDACTED***"}]}'
    )
    assert "secret" not in encoded
    assert "abcdef" not in encoded


def test_encoder_accepts_exact_16384_utf8_bytes_and_rejects_16385() -> None:
    accepted = encode_redacted_event_payload({"safe": "x" * 16_373})
    assert len(accepted.encode("utf-8")) == MAX_CONVERSATION_EVENT_PAYLOAD_BYTES
    with pytest.raises(ValueError, match="16 KiB"):
        encode_redacted_event_payload({"safe": "x" * 16_374})


def test_message_created_boundary_is_typed_whitelisted_and_flush_only() -> None:
    repository = RecordingRepository()
    service = ConversationEventService(repository)
    actor = AuthenticatedUser(id=_uuid(), username="alice", role=UserRole.ANALYST)
    conversation_id = _uuid()
    message_id = _uuid()
    turn_id = _uuid()
    task_id = _uuid()

    result = service.append_message_created(
        actor,
        conversation_id=conversation_id,
        message_id=message_id,
        turn_id=turn_id,
        task_id=task_id,
        sequence=2,
        plan_version=3,
        attachment_count=4,
    )

    assert result.event_type == "conversation.message.created"
    assert result.payload == {
        "attachment_count": 4,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "plan_version": 3,
        "sequence": 2,
        "task_id": task_id,
        "turn_id": turn_id,
    }
    assert repository.calls[0][3] is False
    assert service.session is repository.session

    with pytest.raises(TypeError):
        service.append_message_created(
            actor,
            conversation_id=conversation_id,
            message_id=message_id,
            turn_id=turn_id,
            task_id=task_id,
            sequence=2,
            plan_version=3,
            attachment_count=4,
            content="must never persist",
        )
