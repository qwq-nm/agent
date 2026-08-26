import json
import math
from collections import UserDict
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

import pytest
from pydantic import ValidationError

from secagent.conversation_domain import (
    AttachmentMetadataCreate,
    AttachmentRead,
    AttachmentStatus,
    ConversationCreate,
    ConversationEventCreate,
    ConversationEventRead,
    ConversationMessageKind,
    ConversationMessageRead,
    ConversationMessageRole,
    ConversationMessageStatus,
    ConversationMessageWrite,
    ConversationPatch,
    ConversationRead,
    ConversationSettings,
    ConversationStatus,
    ConversationTurnCreate,
    ConversationTurnRead,
    ConversationTurnStatus,
    TurnBudgetSnapshot,
    UserMessageCreate,
    canonical_json_dumps,
    canonical_json_loads,
)
from secagent.domain import SafetyMode


def _uuid() -> str:
    return str(uuid4())


def _budget() -> TurnBudgetSnapshot:
    return TurnBudgetSnapshot(
        max_subtasks=12,
        max_model_calls_per_subtask=2,
        max_tool_calls_per_subtask=4,
        timeout_seconds=300,
        max_replans=2,
        max_context_tokens=120_000,
    )


def test_conversation_enums_are_exact_and_defaults_are_conservative() -> None:
    assert {item.value for item in ConversationStatus} == {"active", "archived"}
    assert {item.value for item in ConversationMessageRole} == {
        "user",
        "assistant",
        "system",
    }
    assert {item.value for item in ConversationMessageKind} == {
        "user_text",
        "assistant_status",
        "plan",
        "tool_approval",
        "model_failure",
        "assistant_answer",
        "system_notice",
    }
    assert {item.value for item in ConversationMessageStatus} == {
        "pending",
        "streaming",
        "completed",
        "failed",
        "superseded",
    }
    assert {item.value for item in AttachmentStatus} == {
        "pending",
        "ready",
        "failed",
    }
    assert {item.value for item in ConversationTurnStatus} == {
        "created",
        "decomposing",
        "scheduling",
        "running",
        "synthesizing",
        "completed",
        "waiting_tool_approval",
        "waiting_model_decision",
        "replan_requested",
        "superseded",
        "partial",
        "failed_retryable",
        "failed",
        "cancelled",
    }

    created = ConversationCreate()
    assert created.title == "新对话"
    assert created.settings == ConversationSettings()
    assert created.settings.safety_mode is SafetyMode.CONSERVATIVE
    assert created.settings.authorization_scope == ""
    assert created.settings.allowed_targets == []
    assert created.settings.requested_parallelism is None


def test_settings_trim_and_validate_scope_targets_and_parallelism() -> None:
    settings = ConversationSettings(
        authorization_scope="  owned files only  ",
        allowed_targets=["  https://a.example  ", "https://b.example"],
        requested_parallelism=3,
    )
    assert settings.authorization_scope == "owned files only"
    assert settings.allowed_targets == ["https://a.example", "https://b.example"]

    invalid_values = [
        {"authorization_scope": "x" * 4001},
        {"allowed_targets": ["target"] * 2},
        {"allowed_targets": [" "]},
        {"allowed_targets": ["x" * 2049]},
        {"allowed_targets": [str(index) for index in range(21)]},
        {"requested_parallelism": 0},
        {"requested_parallelism": 4},
        {"unexpected": True},
    ]
    for value in invalid_values:
        with pytest.raises(ValidationError):
            ConversationSettings.model_validate(value)


def test_create_patch_and_user_message_reject_server_fields_and_bad_boundaries() -> None:
    assert ConversationCreate(title="  Project Alpha  ").title == "Project Alpha"
    with pytest.raises(ValidationError):
        ConversationCreate(title=" ")
    with pytest.raises(ValidationError):
        ConversationCreate(title="x" * 161)
    with pytest.raises(ValidationError):
        ConversationCreate.model_validate({"owner_id": _uuid()})

    with pytest.raises(ValidationError):
        ConversationPatch()
    with pytest.raises(ValidationError):
        ConversationPatch(title=None)
    with pytest.raises(ValidationError):
        ConversationPatch(settings=None)
    with pytest.raises(ValidationError):
        ConversationPatch.model_validate({"status": "archived"})
    assert ConversationPatch(title="  Renamed  ").title == "Renamed"

    assert UserMessageCreate(content="hello").content == "hello"
    with pytest.raises(ValidationError):
        UserMessageCreate(content="")
    with pytest.raises(ValidationError):
        UserMessageCreate(content="x" * 64_001)
    with pytest.raises(ValidationError):
        UserMessageCreate.model_validate({"content": "hello", "sequence": 1})


@pytest.mark.parametrize("payload", [UserDict(), MappingProxyType({})])
def test_patch_rejects_empty_mapping_implementations(payload) -> None:
    with pytest.raises(ValidationError):
        ConversationPatch.model_validate(payload)


def test_uuid_fields_are_hyphenated_normalized_and_not_arbitrary() -> None:
    canonical = _uuid()
    upper = canonical.upper()
    write = ConversationMessageWrite(
        role=ConversationMessageRole.USER,
        kind=ConversationMessageKind.USER_TEXT,
        content="hello",
        turn_id=upper,
    )
    assert write.turn_id == canonical
    assert write.status is ConversationMessageStatus.COMPLETED

    for invalid in (canonical.replace("-", ""), "x" * 36, "not-a-uuid"):
        with pytest.raises(ValidationError):
            ConversationMessageWrite(
                role="user",
                kind="user_text",
                content="hello",
                turn_id=invalid,
            )

    turn = ConversationTurnCreate(
        trigger_message_id=upper,
        task_id=upper,
        replan_from_turn_id=upper,
        budget=_budget(),
    )
    assert turn.trigger_message_id == canonical
    assert turn.task_id == canonical
    assert turn.replan_from_turn_id == canonical


def test_budget_and_internal_message_boundaries_are_strict() -> None:
    assert _budget().max_context_tokens == 120_000
    assert TurnBudgetSnapshot(
        max_subtasks=0,
        max_model_calls_per_subtask=0,
        max_tool_calls_per_subtask=0,
        timeout_seconds=0,
        max_replans=0,
        max_context_tokens=0,
    ).max_subtasks == 0
    for field in TurnBudgetSnapshot.model_fields:
        values = _budget().model_dump()
        values[field] = -1
        with pytest.raises(ValidationError):
            TurnBudgetSnapshot.model_validate(values)

    with pytest.raises(ValidationError):
        ConversationMessageWrite(
            role="assistant",
            kind="assistant_status",
            content="ok",
            idempotency_key="",
        )
    with pytest.raises(ValidationError):
        ConversationMessageWrite(
            role="assistant",
            kind="assistant_status",
            content="ok",
            idempotency_key="x" * 256,
        )
    with pytest.raises(ValidationError):
        ConversationMessageWrite.model_validate(
            {
                "role": "assistant",
                "kind": "assistant_status",
                "content": "ok",
                "sequence": 1,
            }
        )


def test_attachment_paths_hash_and_metadata_are_strict() -> None:
    attachment = AttachmentMetadataCreate(
        original_name="evidence.zip",
        storage_ref="uploads/2026/evidence.zip",
        relative_path="project/evidence.zip",
        content_type="application/zip",
        size_bytes=0,
        sha256="a" * 64,
    )
    assert attachment.status is AttachmentStatus.READY

    invalid_paths = (
        "/absolute/file.txt",
        "C:/drive/file.txt",
        "C:\\drive\\file.txt",
        "folder\\file.txt",
        "folder/../file.txt",
        "folder/./file.txt",
        "folder//file.txt",
        "./file.txt",
        "../file.txt",
        "folder/\x00file.txt",
        "uploads/NUL",
        "uploads/con.txt",
        "uploads/COM1.log",
        "uploads/file.txt:stream",
        "uploads/trailing./x",
        "uploads/trailing /x",
    )
    for path in invalid_paths:
        with pytest.raises(ValidationError):
            AttachmentMetadataCreate(
                original_name="file.txt",
                storage_ref=path,
                content_type="text/plain",
                size_bytes=1,
                sha256="a" * 64,
            )

    for sha256 in ("A" * 64, "g" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ValidationError):
            AttachmentMetadataCreate(
                original_name="file.txt",
                storage_ref="uploads/file.txt",
                content_type="text/plain",
                size_bytes=1,
                sha256=sha256,
            )

    for field, value in (
        ("original_name", ""),
        ("original_name", "x" * 256),
        ("storage_ref", ""),
        ("storage_ref", "x" * 501),
        ("content_type", "x" * 256),
        ("content_type", ""),
        ("relative_path", ""),
        ("relative_path", "x" * 1001),
        ("size_bytes", -1),
    ):
        valid = attachment.model_dump()
        valid[field] = value
        with pytest.raises(ValidationError):
            AttachmentMetadataCreate.model_validate(valid)


def test_event_payload_and_canonical_json_reject_non_json_values() -> None:
    turn_id = _uuid()
    event = ConversationEventCreate(
        event_type="turn.started",
        payload={"中文": "保留", "nested": [True, None, 2.5]},
        turn_id=turn_id.upper(),
        subtask_id="future-subtask-1",
    )
    assert event.turn_id == turn_id
    assert event.subtask_id == "future-subtask-1"
    encoded = canonical_json_dumps(event.payload)
    assert encoded == '{"nested":[true,null,2.5],"中文":"保留"}'
    assert canonical_json_loads(encoded, dict[str, object]) == event.payload

    invalid_payloads = (
        {1: "non-string-key"},
        {"value": object()},
        {"value": math.nan},
        {"value": math.inf},
        {"value": -math.inf},
        {"value": ("tuple",)},
    )
    for payload in invalid_payloads:
        with pytest.raises((ValidationError, TypeError, ValueError)):
            ConversationEventCreate(event_type="event", payload=payload)
        with pytest.raises((TypeError, ValueError)):
            canonical_json_dumps(payload)

    for raw in ('{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}'):
        with pytest.raises(ValueError):
            canonical_json_loads(raw, dict[str, object])

    with pytest.raises(ValidationError):
        ConversationEventCreate(event_type="")
    with pytest.raises(ValidationError):
        ConversationEventCreate(event_type="x" * 81)
    with pytest.raises(ValidationError):
        ConversationEventCreate(event_type="ok", subtask_id="x" * 37)
    with pytest.raises(ValidationError):
        ConversationEventCreate(event_type="ok", subtask_id=" ")

    surrogate = chr(0xD800)
    with pytest.raises((ValidationError, TypeError, ValueError)):
        ConversationEventCreate(event_type="event", payload={"value": surrogate})
    with pytest.raises((TypeError, ValueError)):
        canonical_json_dumps({"value": surrogate})
    with pytest.raises(ValueError):
        canonical_json_loads(
            json.dumps({"value": surrogate}), dict[str, object]
        )


def test_canonical_json_round_trips_named_dtos() -> None:
    settings = ConversationSettings(
        authorization_scope="owned data",
        safety_mode=SafetyMode.STANDARD,
        allowed_targets=["alpha", "beta"],
        requested_parallelism=2,
    )
    encoded = canonical_json_dumps(settings)
    decoded = canonical_json_loads(encoded, ConversationSettings)
    assert decoded == settings

    now = datetime.now(timezone.utc)
    with pytest.raises(TypeError):
        canonical_json_dumps({"timestamp": now})


def test_request_dtos_reject_bytes_and_non_list_target_containers() -> None:
    with pytest.raises(ValidationError):
        ConversationCreate(title=b"title")
    with pytest.raises(ValidationError):
        UserMessageCreate(content=b"message")
    with pytest.raises(ValidationError):
        ConversationSettings(allowed_targets=("target",))
    with pytest.raises(ValidationError):
        ConversationSettings(allowed_targets={"target"})
    with pytest.raises(ValidationError):
        ConversationSettings(safety_mode=b"standard")
    with pytest.raises(ValidationError):
        ConversationMessageWrite(
            role=b"user",
            kind="user_text",
            content="message",
        )
    with pytest.raises(ValidationError):
        ConversationMessageWrite(
            role="user",
            kind=b"user_text",
            content="message",
        )
    with pytest.raises(ValidationError):
        AttachmentMetadataCreate(
            original_name="file.txt",
            storage_ref="uploads/file.txt",
            content_type="text/plain",
            size_bytes=1,
            sha256=b"a" * 64,
        )
    with pytest.raises(ValidationError):
        AttachmentMetadataCreate(
            original_name="file.txt",
            storage_ref="uploads/file.txt",
            content_type="text/plain",
            size_bytes=1,
            sha256="a" * 64,
            status=b"ready",
        )


def test_exact_maximum_boundaries_and_all_read_dtos_are_valid() -> None:
    conversation_id = _uuid()
    owner_id = _uuid()
    message_id = _uuid()
    turn_id = _uuid()
    attachment_id = _uuid()
    now = datetime.now(timezone.utc)
    targets = [f"{index:02d}" + "x" * 2046 for index in range(20)]
    settings = ConversationSettings(
        authorization_scope="s" * 4_000,
        allowed_targets=targets,
        requested_parallelism=3,
    )
    assert len(settings.allowed_targets) == 20
    assert all(len(item) == 2_048 for item in settings.allowed_targets)
    assert ConversationSettings(requested_parallelism=1).requested_parallelism == 1
    assert len(ConversationCreate(title="t" * 160).title) == 160
    assert len(UserMessageCreate(content="m" * 64_000).content) == 64_000

    message_write = ConversationMessageWrite(
        role="assistant",
        kind="assistant_status",
        content="status",
        idempotency_key="i" * 255,
    )
    assert len(message_write.idempotency_key or "") == 255

    storage_ref = "/".join(["a" * 100] * 4 + ["b" * 96])
    relative_path = "/".join(["c" * 99] * 9 + ["d" * 100])
    attachment = AttachmentRead(
        id=attachment_id,
        message_id=message_id,
        original_name="o" * 255,
        storage_ref=storage_ref,
        relative_path=relative_path,
        content_type="c" * 255,
        size_bytes=0,
        sha256="a" * 64,
        status="ready",
        created_at=now,
    )
    assert len(attachment.storage_ref) == 500
    assert len(attachment.relative_path or "") == 1_000

    conversation = ConversationRead(
        id=conversation_id,
        owner_id=owner_id,
        title="Conversation",
        status="active",
        settings=settings,
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
        created_at=now,
        updated_at=now,
    )
    turn = ConversationTurnRead(
        id=turn_id,
        conversation_id=conversation_id,
        trigger_message_id=message_id,
        plan_version=1,
        status="created",
        budget=_budget(),
        created_at=now,
        updated_at=now,
    )
    event = ConversationEventRead(
        cursor=1,
        conversation_id=conversation_id,
        event_type="e" * 80,
        payload={"ok": True},
        turn_id=turn_id,
        subtask_id="s" * 36,
        created_at=now,
    )
    assert conversation.active_turn_id == turn.id
    assert message.sequence == turn.plan_version == event.cursor == 1

    for model, field, value in (
        (ConversationMessageRead, "sequence", 0),
        (ConversationTurnRead, "plan_version", 0),
        (ConversationEventRead, "cursor", 0),
    ):
        data = {
            ConversationMessageRead: message.model_dump(),
            ConversationTurnRead: turn.model_dump(),
            ConversationEventRead: event.model_dump(),
        }[model]
        data[field] = value
        with pytest.raises(ValidationError):
            model.model_validate(data)
