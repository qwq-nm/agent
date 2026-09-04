from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator


class Capability(StrEnum):
    CHINESE_SEMANTIC = "chinese_semantic"
    LONG_DOCUMENT_SUMMARIZATION = "long_document_summarization"
    CLASSIFICATION_EXTRACTION = "classification_extraction"
    TASK_DECOMPOSITION = "task_decomposition"
    CODE_SECURITY_REASONING = "code_security_reasoning"
    REVERSE_CAUSAL_ANALYSIS = "reverse_causal_analysis"
    EVIDENCE_CONFLICT_RESOLUTION = "evidence_conflict_resolution"
    FINAL_SYNTHESIS = "final_synthesis"
    TOOL_REQUEST = "tool_request"


class LogicalProvider(StrEnum):
    GLM = "glm"
    DEEPSEEK = "deepseek"


class RouteReasonCode(StrEnum):
    GLM_CHINESE_STRENGTH = "glm_chinese_strength"
    GLM_LONG_DOCUMENT_STRENGTH = "glm_long_document_strength"
    GLM_EXTRACTION_STRENGTH = "glm_extraction_strength"
    DEEPSEEK_DECOMPOSITION_FIXED = "deepseek_decomposition_fixed"
    DEEPSEEK_CODE_SECURITY_STRENGTH = "deepseek_code_security_strength"
    DEEPSEEK_REVERSE_CAUSAL_STRENGTH = "deepseek_reverse_causal_strength"
    DEEPSEEK_EVIDENCE_CONFLICT_STRENGTH = "deepseek_evidence_conflict_strength"
    DEEPSEEK_SYNTHESIS_FIXED = "deepseek_synthesis_fixed"
    BALANCED_MODEL_SUGGESTION = "balanced_model_suggestion"
    TOOL_COMPATIBLE = "tool_compatible"
    POLICY_PREFERRED_CAPABILITY = "policy_preferred_capability"
    POLICY_PROVIDER_UNAVAILABLE = "policy_provider_unavailable"
    POLICY_CAPABILITY_MISMATCH = "policy_capability_mismatch"
    POLICY_TOOL_FILTERED = "policy_tool_filtered"


_KEY_PATTERN: Final[str] = r"^[a-z][a-z0-9_-]{0,63}$"
_POLICY_ROUTE_REASON_CODES: Final[frozenset[RouteReasonCode]] = frozenset(
    {
        RouteReasonCode.POLICY_PREFERRED_CAPABILITY,
        RouteReasonCode.POLICY_PROVIDER_UNAVAILABLE,
        RouteReasonCode.POLICY_CAPABILITY_MISMATCH,
        RouteReasonCode.POLICY_TOOL_FILTERED,
    }
)

#: Route-reason codes the decomposition model is allowed to emit. The policy
#: correction codes are backend-owned and must never appear in the model's
#: response schema, otherwise the model occasionally picks one and fails the
#: whole decompose with ``invalid_schema``.
class ModelRouteReasonCode(StrEnum):
    GLM_CHINESE_STRENGTH = "glm_chinese_strength"
    GLM_LONG_DOCUMENT_STRENGTH = "glm_long_document_strength"
    GLM_EXTRACTION_STRENGTH = "glm_extraction_strength"
    DEEPSEEK_DECOMPOSITION_FIXED = "deepseek_decomposition_fixed"
    DEEPSEEK_CODE_SECURITY_STRENGTH = "deepseek_code_security_strength"
    DEEPSEEK_REVERSE_CAUSAL_STRENGTH = "deepseek_reverse_causal_strength"
    DEEPSEEK_EVIDENCE_CONFLICT_STRENGTH = "deepseek_evidence_conflict_strength"
    DEEPSEEK_SYNTHESIS_FIXED = "deepseek_synthesis_fixed"
    BALANCED_MODEL_SUGGESTION = "balanced_model_suggestion"
    TOOL_COMPATIBLE = "tool_compatible"


def _non_whitespace(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be whitespace-only")
    return value


StrictText = Annotated[str, StringConstraints(strict=True), AfterValidator(_non_whitespace)]
SubtaskKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=_KEY_PATTERN),
]
BoundedTitle = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=80),
    AfterValidator(_non_whitespace),
]
BoundedObjective = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=2_000),
    AfterValidator(_non_whitespace),
]
BoundedExpectedOutput = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1_000),
    AfterValidator(_non_whitespace),
]
BoundedSynthesisRequirement = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1_000),
    AfterValidator(_non_whitespace),
]
ToolName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    AfterValidator(_non_whitespace),
]
CapabilityInput = Annotated[Capability, Field(strict=False)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SubtaskSpec(_StrictModel):
    key: SubtaskKey
    title: BoundedTitle
    objective: BoundedObjective
    dependency_keys: list[SubtaskKey] = Field(max_length=64)
    required_capabilities: list[CapabilityInput] = Field(max_length=9)
    proposed_provider: LogicalProvider = Field(strict=False)
    route_reason_code: ModelRouteReasonCode = Field(strict=False)
    allowed_tools: list[ToolName] = Field(max_length=64)
    expected_output: BoundedExpectedOutput
    required: bool

    @field_validator("dependency_keys", "required_capabilities", "allowed_tools")
    @classmethod
    def require_unique_values(cls, value: list[object]) -> list[object]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value


class DecompositionDocument(_StrictModel):
    plan_version: int = Field(ge=1, strict=True)
    goal_summary: Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=2_000),
        AfterValidator(_non_whitespace),
    ]
    subtasks: list[SubtaskSpec] = Field(min_length=1, max_length=64)
    synthesis_requirements: list[BoundedSynthesisRequirement] = Field(
        min_length=1, max_length=32
    )


def validate_decomposition(
    document: DecompositionDocument,
    *,
    expected_plan_version: int,
    max_subtasks: int,
) -> DecompositionDocument:
    """Validate dynamic plan constraints and dependency graph invariants.

    The document is already structurally validated by Pydantic. This second
    pass validates constraints that depend on the caller's policy and on all
    subtask keys together, returning the same object when successful.
    """
    if not isinstance(document, DecompositionDocument):
        raise TypeError("document must be a DecompositionDocument")
    if not isinstance(expected_plan_version, int) or isinstance(
        expected_plan_version, bool
    ):
        raise TypeError("expected_plan_version must be an integer")
    if not isinstance(max_subtasks, int) or isinstance(max_subtasks, bool):
        raise TypeError("max_subtasks must be an integer")
    if max_subtasks < 1:
        raise ValueError("max_subtasks must be at least 1")
    if document.plan_version != expected_plan_version:
        raise ValueError("plan version does not match expected plan version")
    if len(document.subtasks) > max_subtasks:
        raise ValueError("subtask count exceeds max_subtasks")

    keys: dict[str, SubtaskSpec] = {}
    for subtask in document.subtasks:
        if subtask.key in keys:
            raise ValueError(f"duplicate subtask key: {subtask.key}")
        keys[subtask.key] = subtask

    dependencies: dict[str, list[str]] = {}
    for subtask in document.subtasks:
        dependencies[subtask.key] = list(subtask.dependency_keys)
        for dependency_key in subtask.dependency_keys:
            if dependency_key == subtask.key:
                raise ValueError(f"self-dependency for key: {subtask.key}")
            if dependency_key not in keys:
                raise ValueError(f"missing dependency key: {dependency_key}")

    _validate_acyclic(dependencies)
    return document


def _validate_acyclic(dependencies: dict[str, list[str]]) -> None:
    colors: dict[str, int] = {key: 0 for key in dependencies}
    path: list[str] = []

    def visit(key: str) -> None:
        colors[key] = 1
        path.append(key)
        for dependency in dependencies[key]:
            if colors[dependency] == 0:
                visit(dependency)
            elif colors[dependency] == 1:
                cycle_start = path.index(dependency)
                cycle = path[cycle_start:] + [dependency]
                raise ValueError(f"dependency cycle detected: {' -> '.join(cycle)}")
        path.pop()
        colors[key] = 2

    for key in dependencies:
        if colors[key] == 0:
            visit(key)
