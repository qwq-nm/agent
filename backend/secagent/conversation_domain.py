from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, TypeVar
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from secagent.domain import SafetyMode


_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_LOWER_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"|?*')


def contains_unicode_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise ValueError("must be a canonical hyphenated UUID")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError("must be a canonical hyphenated UUID") from exc


def _require_string_enum(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("enum input must be a string")
    return value


CanonicalUUID = Annotated[str, BeforeValidator(_canonical_uuid)]
TrimmedTitle = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=160
    ),
]
TrimmedAuthorizationScope = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, max_length=4_000),
]
TrimmedTarget = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=2_048
    ),
]
EventType = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=80
    ),
]
OpaqueSubtaskID = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=36
    ),
]


def _non_whitespace(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be whitespace-only")
    return value


IdempotencyKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255),
    AfterValidator(_non_whitespace),
]


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ConversationMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationMessageKind(StrEnum):
    USER_TEXT = "user_text"
    ASSISTANT_STATUS = "assistant_status"
    PLAN = "plan"
    TOOL_APPROVAL = "tool_approval"
    MODEL_FAILURE = "model_failure"
    ASSISTANT_ANSWER = "assistant_answer"
    SYSTEM_NOTICE = "system_notice"


class ConversationMessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class AttachmentStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class ConversationTurnStatus(StrEnum):
    CREATED = "created"
    DECOMPOSING = "decomposing"
    SCHEDULING = "scheduling"
    RUNNING = "running"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    WAITING_TOOL_APPROVAL = "waiting_tool_approval"
    WAITING_MODEL_DECISION = "waiting_model_decision"
    REPLAN_REQUESTED = "replan_requested"
    SUPERSEDED = "superseded"
    PARTIAL = "partial"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class ConversationSettings(_StrictModel):
    authorization_scope: TrimmedAuthorizationScope = ""
    safety_mode: SafetyMode = SafetyMode.CONSERVATIVE
    allowed_targets: list[TrimmedTarget] = Field(default_factory=list, max_length=20)
    requested_parallelism: int | None = Field(default=None, ge=1, le=3, strict=True)

    @field_validator("safety_mode", mode="before")
    @classmethod
    def validate_safety_mode_input(cls, value: object) -> object:
        return _require_string_enum(value)

    @field_validator("allowed_targets", mode="before")
    @classmethod
    def require_target_list(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("allowed targets must be a list")
        return value

    @field_validator("allowed_targets")
    @classmethod
    def validate_unique_targets(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed targets must be unique")
        return value


class ConversationCreate(_StrictModel):
    title: TrimmedTitle = "新对话"
    settings: ConversationSettings = Field(default_factory=ConversationSettings)


class ConversationPatch(_StrictModel):
    title: TrimmedTitle | None = None
    settings: ConversationSettings | None = None

    @model_validator(mode="after")
    def require_non_null_patch(self) -> "ConversationPatch":
        if not self.model_fields_set:
            raise ValueError("at least one patch field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("patch fields cannot be null")
        return self


class ConversationRead(_StrictModel):
    id: CanonicalUUID
    owner_id: CanonicalUUID
    title: TrimmedTitle
    status: ConversationStatus
    settings: ConversationSettings
    active_turn_id: CanonicalUUID | None = None
    created_at: datetime
    updated_at: datetime


class ConversationMessageWrite(_StrictModel):
    role: ConversationMessageRole
    kind: ConversationMessageKind
    content: str = Field(min_length=1, max_length=64_000, strict=True)
    status: ConversationMessageStatus = ConversationMessageStatus.COMPLETED
    turn_id: CanonicalUUID | None = None
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=255, strict=True
    )

    @field_validator("role", "kind", "status", mode="before")
    @classmethod
    def validate_enum_inputs(cls, value: object) -> object:
        return _require_string_enum(value)


class ConversationMessageRead(_StrictModel):
    id: CanonicalUUID
    conversation_id: CanonicalUUID
    sequence: int = Field(ge=1, strict=True)
    role: ConversationMessageRole
    kind: ConversationMessageKind
    content: str
    status: ConversationMessageStatus
    turn_id: CanonicalUUID | None = None
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime


class TurnBudgetSnapshot(_StrictModel):
    max_subtasks: int = Field(ge=0, strict=True)
    max_model_calls_per_subtask: int = Field(ge=0, strict=True)
    max_tool_calls_per_subtask: int = Field(ge=0, strict=True)
    timeout_seconds: int = Field(ge=0, strict=True)
    max_replans: int = Field(ge=0, strict=True)
    max_context_tokens: int = Field(ge=0, strict=True)


class ConversationTurnCreate(_StrictModel):
    trigger_message_id: CanonicalUUID
    task_id: CanonicalUUID | None = None
    budget: TurnBudgetSnapshot
    replan_from_turn_id: CanonicalUUID | None = None


class ConversationTurnRead(_StrictModel):
    id: CanonicalUUID
    conversation_id: CanonicalUUID
    trigger_message_id: CanonicalUUID
    task_id: CanonicalUUID | None = None
    plan_version: int = Field(ge=1, strict=True)
    status: ConversationTurnStatus
    budget: TurnBudgetSnapshot
    replan_from_turn_id: CanonicalUUID | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _safe_relative_posix_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    if "\x00" in value or "\\" in value:
        raise ValueError("path must be a safe relative POSIX path")
    if value.startswith("/") or _WINDOWS_DRIVE_RE.match(value):
        raise ValueError("path must be relative")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("path contains an unsafe segment")
    for segment in segments:
        if segment.endswith((".", " ")):
            raise ValueError("path contains a Windows-normalized alias")
        if any(
            character in _WINDOWS_FORBIDDEN_CHARACTERS
            or contains_unicode_control_characters(character)
            for character in segment
        ):
            raise ValueError("path contains a Windows-forbidden character")
        device_stem = segment.split(".", 1)[0].upper()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError("path contains a reserved Windows device name")
    return value


SafeStorageRef = Annotated[
    str,
    BeforeValidator(_safe_relative_posix_path),
    Field(min_length=1, max_length=500),
]
SafeClientRelativePath = Annotated[
    str,
    BeforeValidator(_safe_relative_posix_path),
    Field(min_length=1, max_length=1_000),
]


class MessageSubmission(_StrictModel):
    content: Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=64_000),
        AfterValidator(_non_whitespace),
    ]
    relative_paths: list[SafeClientRelativePath | None] = Field(default_factory=list)


class UserMessageCreate(MessageSubmission):
    """Backward-compatible name for the strict conversation submission DTO."""


class AttachmentMetadataCreate(_StrictModel):
    original_name: str = Field(min_length=1, max_length=255, strict=True)
    storage_ref: SafeStorageRef
    relative_path: SafeClientRelativePath | None = None
    content_type: str = Field(min_length=1, max_length=255, strict=True)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=_LOWER_SHA256_RE.pattern, strict=True)
    status: AttachmentStatus = AttachmentStatus.READY

    @field_validator("status", mode="before")
    @classmethod
    def validate_status_input(cls, value: object) -> object:
        return _require_string_enum(value)


class AttachmentRead(AttachmentMetadataCreate):
    id: CanonicalUUID
    message_id: CanonicalUUID
    created_at: datetime


class MessageWithAttachments(_StrictModel):
    message: ConversationMessageRead
    attachments: list[AttachmentRead] = Field(default_factory=list)


class ConversationDetailRead(_StrictModel):
    conversation: ConversationRead
    messages: list[MessageWithAttachments] = Field(default_factory=list)
    turns: list[ConversationTurnRead] = Field(default_factory=list)
    active_turn: ConversationTurnRead | None = None


class MessageSendRead(_StrictModel):
    message: ConversationMessageRead
    attachments: list[AttachmentRead] = Field(default_factory=list)
    turn: ConversationTurnRead
    replayed: bool


def _validate_json_value(value: object, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        _validate_utf8(value, path)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object key at {path} must be a string")
            _validate_utf8(key, path)
            _validate_json_value(item, f"{path}.{key}")
        return
    raise TypeError(f"unsupported JSON value at {path}: {type(value).__name__}")


def _validate_utf8(value: str, path: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid UTF-8 string at {path}") from exc


def _validated_json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("event payload must be a JSON object")
    _validate_json_value(value)
    return value


class ConversationEventCreate(_StrictModel):
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    turn_id: CanonicalUUID | None = None
    subtask_id: OpaqueSubtaskID | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> dict[str, Any]:
        return _validated_json_object(value)


class ConversationEventRead(_StrictModel):
    cursor: int = Field(ge=1, strict=True)
    conversation_id: CanonicalUUID
    event_type: EventType
    payload: dict[str, Any]
    turn_id: CanonicalUUID | None = None
    subtask_id: OpaqueSubtaskID | None = None
    created_at: datetime

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> dict[str, Any]:
        return _validated_json_object(value)


T = TypeVar("T")


def canonical_json_dumps(value: object) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    _validate_json_value(data)
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def canonical_json_loads(raw: str, target: type[T] | Any) -> T:
    if not isinstance(raw, str):
        raise TypeError("JSON input must be a string")
    data = json.loads(raw, parse_constant=_reject_json_constant)
    _validate_json_value(data)
    return TypeAdapter(target).validate_python(data)
