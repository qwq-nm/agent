from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from secagent.config import Settings
from secagent.conversation_domain import (
    AttachmentRead,
    ConversationDetailRead,
    ConversationMessageRead,
    ConversationRead,
    ConversationTurnRead,
    IdempotencyKey,
    MessageSendRead,
    MessageSubmission,
    MessageWithAttachments,
    TurnBudgetSnapshot,
)


def _uuid() -> str:
    return str(uuid4())


def _read_models():
    now = datetime.now(timezone.utc)
    conversation_id = _uuid()
    owner_id = _uuid()
    message_id = _uuid()
    turn_id = _uuid()
    conversation = ConversationRead(
        id=conversation_id,
        owner_id=owner_id,
        title="Conversation",
        status="active",
        settings={},
        active_turn_id=turn_id,
        created_at=now,
        updated_at=now,
    )
    message = ConversationMessageRead(
        id=message_id,
        conversation_id=conversation_id,
        sequence=1,
        role="user",
        kind="user_text",
        content="hello",
        status="completed",
        turn_id=turn_id,
        idempotency_key="request-1",
        created_at=now,
        updated_at=now,
    )
    attachment = AttachmentRead(
        id=_uuid(),
        message_id=message_id,
        original_name="proof.txt",
        storage_ref=f"conversations/{conversation_id}/messages/{message_id}/uploads/a",
        relative_path="evidence/proof.txt",
        content_type="text/plain",
        size_bytes=5,
        sha256="a" * 64,
        created_at=now,
    )
    turn = ConversationTurnRead(
        id=turn_id,
        conversation_id=conversation_id,
        trigger_message_id=message_id,
        task_id=_uuid(),
        plan_version=1,
        status="created",
        budget=TurnBudgetSnapshot(
            max_subtasks=12,
            max_model_calls_per_subtask=4,
            max_tool_calls_per_subtask=6,
            timeout_seconds=180,
            max_replans=2,
            max_context_tokens=32_000,
        ),
        created_at=now,
        updated_at=now,
    )
    return conversation, message, attachment, turn


def test_message_submission_is_strict_and_preserves_content_and_safe_paths() -> None:
    submission = MessageSubmission(
        content="  keep surrounding whitespace  ",
        relative_paths=["evidence/one.txt", None],
    )
    assert submission.content == "  keep surrounding whitespace  "
    assert submission.relative_paths == ["evidence/one.txt", None]

    for content in ("", " \t\r\n", "x" * 64_001, b"bytes"):
        with pytest.raises(ValidationError):
            MessageSubmission(content=content)
    for relative_paths in (
        ["../escape.txt"],
        ["folder\\file.txt"],
        ["folder/CON.txt"],
        ["folder/file.txt:stream"],
        ["folder/trailing./file.txt"],
    ):
        with pytest.raises(ValidationError):
            MessageSubmission(content="ok", relative_paths=relative_paths)
    with pytest.raises(ValidationError):
        MessageSubmission.model_validate({"content": "ok", "unknown": True})


def test_idempotency_key_is_strict_non_whitespace_and_not_normalized() -> None:
    from pydantic import TypeAdapter

    adapter = TypeAdapter(IdempotencyKey)
    assert adapter.validate_python("  exact key  ") == "  exact key  "
    assert len(adapter.validate_python("x" * 255)) == 255
    for value in ("", " \t", "x" * 256, b"bytes"):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_composite_read_dtos_are_strict_and_nested() -> None:
    conversation, message, attachment, turn = _read_models()
    item = MessageWithAttachments(message=message, attachments=[attachment])
    detail = ConversationDetailRead(
        conversation=conversation,
        messages=[item],
        turns=[turn],
        active_turn=turn,
    )
    sent = MessageSendRead(
        message=message,
        attachments=[attachment],
        turn=turn,
        replayed=False,
    )
    assert detail.messages[0].attachments[0].message_id == message.id
    assert detail.active_turn == sent.turn
    with pytest.raises(ValidationError):
        MessageWithAttachments.model_validate(
            {"message": message, "attachments": [], "raw": {}}
        )
    with pytest.raises(ValidationError):
        ConversationDetailRead.model_validate(
            {
                "conversation": conversation,
                "messages": [],
                "turns": [],
                "active_turn": None,
                "orm": object(),
            }
        )
    with pytest.raises(ValidationError):
        MessageSendRead.model_validate(
            {
                "message": message,
                "attachments": [],
                "turn": turn,
                "replayed": False,
                "raw": {},
            }
        )


def test_conversation_settings_defaults_and_exact_positive_boundaries() -> None:
    settings = Settings()
    assert settings.max_parallel_subtasks_per_conversation == 3
    assert settings.max_subtasks_per_turn == 12
    assert settings.max_model_calls_per_subtask == 4
    assert settings.max_tool_calls_per_subtask == 6
    assert settings.subtask_timeout_seconds == 180
    assert settings.max_replans_per_turn == 2
    assert settings.max_conversation_context_tokens == 32_000
    assert settings.max_attachments_per_message == 20
    assert settings.max_attachment_total_bytes == 209_715_200

    fields = (
        "max_parallel_subtasks_per_conversation",
        "max_subtasks_per_turn",
        "max_model_calls_per_subtask",
        "max_tool_calls_per_subtask",
        "subtask_timeout_seconds",
        "max_replans_per_turn",
        "max_conversation_context_tokens",
        "max_attachments_per_message",
        "max_attachment_total_bytes",
    )
    for field in fields:
        assert getattr(Settings(**{field: 1}), field) == 1
        for invalid in (0, -1, True, False):
            with pytest.raises(ValidationError):
                Settings(**{field: invalid})
    with pytest.raises(ValidationError):
        Settings(max_parallel_subtasks_per_conversation=4)


def test_conversation_settings_use_normal_uppercase_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_SUBTASKS_PER_TURN", "7")
    monkeypatch.setenv("MAX_ATTACHMENT_TOTAL_BYTES", "12345")
    settings = Settings()
    assert settings.max_subtasks_per_turn == 7
    assert settings.max_attachment_total_bytes == 12_345
