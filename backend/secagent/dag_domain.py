"""Strict contracts for the durable subtask DAG execution phase."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from secagent.conversation_decomposition import SubtaskKey
from secagent.conversation_domain import CanonicalUUID


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


def _non_whitespace(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be whitespace-only")
    return value


def _unique(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("values must be unique")
    return values


class JobKind(StrEnum):
    TURN_DECOMPOSE = "turn_decompose"
    SUBTASK_EXECUTE = "subtask_execute"
    TURN_SYNTHESIZE = "turn_synthesize"


class SubtaskStatus(StrEnum):
    PENDING_DEPENDENCY = "pending_dependency"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_TOOL_APPROVAL = "waiting_tool_approval"
    WAITING_MODEL_DECISION = "waiting_model_decision"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Terminal subtask states never leave their state once entered.
TERMINAL_SUBTASK_STATUSES: frozenset[SubtaskStatus] = frozenset(
    {
        SubtaskStatus.COMPLETED,
        SubtaskStatus.INCOMPLETE,
        SubtaskStatus.SKIPPED,
        SubtaskStatus.SUPERSEDED,
        SubtaskStatus.FAILED,
        SubtaskStatus.CANCELLED,
    }
)


class ModelFailureStage(StrEnum):
    DECOMPOSE = "decompose"
    SUBTASK = "subtask"
    SYNTHESIZE = "synthesize"


class ModelFailureStatus(StrEnum):
    WAITING_DECISION = "waiting_decision"
    RESOLVED = "resolved"


class ModelFailureDecision(StrEnum):
    RETRY_SAME = "retry_same"
    REASSIGN = "reassign"
    SKIP_AND_REPLAN = "skip_and_replan"
    TERMINATE_TURN = "terminate_turn"


class SubtaskTransitionError(RuntimeError):
    """A conditional subtask state transition did not apply."""


class ModelFailureAlreadyResolved(RuntimeError):
    """A model failure decision was already recorded."""


_BoundedText = Annotated[
    str, StringConstraints(strict=True, min_length=1), AfterValidator(_non_whitespace)
]


class ClaimDocument(_StrictModel):
    """One factual statement; at least one resolvable reference is mandatory."""

    statement: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2_000), AfterValidator(_non_whitespace)]
    evidence_ref: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)] | None = None
    attachment_ref: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)] | None = None
    upstream_key: SubtaskKey | None = None

    @field_validator("evidence_ref", "attachment_ref")
    @classmethod
    def reject_whitespace_refs(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("references must not be whitespace-only")
        return value

    @model_validator(mode="after")
    def require_one_reference(self) -> "ClaimDocument":
        if (
            self.evidence_ref is None
            and self.attachment_ref is None
            and self.upstream_key is None
        ):
            raise ValueError("a factual claim requires at least one reference")
        return self


class SubtaskResultDocument(_StrictModel):
    """Worker output contract (spec 7.3), validated before persistence."""

    status: Literal["completed", "incomplete", "failed"]
    summary: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4_000), AfterValidator(_non_whitespace)]
    claims: list[ClaimDocument] = Field(default_factory=list, max_length=64)
    evidence_refs: list[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]] = Field(default_factory=list, max_length=64)
    inference_notes: list[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1_000)]] = Field(default_factory=list, max_length=32)
    unresolved: list[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1_000)]] = Field(default_factory=list, max_length=32)

    @field_validator("evidence_refs")
    @classmethod
    def require_unique_evidence_refs(cls, value: list[str]) -> list[str]:
        return _unique(value)

    @field_validator("inference_notes", "unresolved")
    @classmethod
    def require_unique_notes(cls, value: list[str]) -> list[str]:
        return _unique(value)


class ModelFailureCreate(_StrictModel):
    turn_id: CanonicalUUID
    subtask_id: CanonicalUUID | None = None
    stage: ModelFailureStage = Field(strict=False)
    provider: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]
    model: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    error_code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]
    detail: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2_000), AfterValidator(_non_whitespace)]


class ModelFailureDecisionInput(_StrictModel):
    decision: ModelFailureDecision
    target_provider: Literal["glm", "deepseek"] | None = None


class SubtaskRead(_StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)

    id: str
    turn_id: str
    key: str
    title: str
    objective: str
    required_capabilities: list[str]
    proposed_provider: str
    assigned_provider: str
    route_reason_code: str
    route_reason: str
    allowed_tools: list[str]
    expected_output: str
    required: bool
    status: SubtaskStatus
    status_version: int
    result_summary: str | None


class SubtaskAttemptRead(_StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)

    id: str
    subtask_id: str
    attempt: int
    provider: str
    model: str
    idempotency_key: str
    status: str
    error_code: str | None


class SubtaskResultRead(_StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)

    id: str
    attempt_id: str
    subtask_id: str
    status: str
    summary: str
    claims: list[ClaimDocument]
    evidence_refs: list[str]
    inference_notes: list[str]
    unresolved: list[str]


class ModelFailureRead(_StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)

    id: str
    conversation_id: str
    turn_id: str
    subtask_id: str | None
    stage: ModelFailureStage
    provider: str
    model: str
    error_code: str
    detail: str
    status: ModelFailureStatus
    decision: ModelFailureDecision | None
    decided_by: str | None


def dag_command_id(*parts: str) -> str:
    """Stable idempotency key for one logical DAG job."""
    return hashlib.sha256(("secagent:" + ":".join(parts)).encode()).hexdigest()


def turn_decompose_command_id(turn_id: str, plan_version: int) -> str:
    return dag_command_id("turn", turn_id, "decompose", f"v{plan_version}")


def subtask_execute_command_id(subtask_id: str, attempt: int) -> str:
    return dag_command_id("subtask", subtask_id, "execute", f"a{attempt}")


def turn_synthesize_command_id(turn_id: str) -> str:
    # Constant per turn: the unique command id is what guarantees that at
    # most one synthesize job can ever exist for a turn.
    return dag_command_id("turn", turn_id, "synthesize")


__all__ = [
    "ClaimDocument",
    "JobKind",
    "ModelFailureAlreadyResolved",
    "ModelFailureCreate",
    "ModelFailureDecision",
    "ModelFailureDecisionInput",
    "ModelFailureRead",
    "ModelFailureStage",
    "ModelFailureStatus",
    "SubtaskAttemptRead",
    "SubtaskRead",
    "SubtaskResultDocument",
    "SubtaskResultRead",
    "SubtaskStatus",
    "SubtaskTransitionError",
    "TERMINAL_SUBTASK_STATUSES",
    "dag_command_id",
]
