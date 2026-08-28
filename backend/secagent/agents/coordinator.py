from __future__ import annotations

from typing import Annotated, Iterable, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from secagent.conversation_decomposition import (
    DecompositionDocument,
    LogicalProvider,
    SubtaskKey,
    validate_decomposition,
)
from secagent.conversation_domain import (
    ConversationSettings,
    SafeClientRelativePath,
    TurnBudgetSnapshot,
    canonical_json_dumps,
)
from secagent.domain import ModelRequest, ModelResponse, ModelStage, RiskLevel
from secagent.providers.router import ModelRouter
from secagent.security.redaction import redact_mapping
from secagent.services.assignment_policy import (
    CAPABILITY_MATRIX,
    AssignmentDecision,
    AssignmentPolicy,
)


def _non_whitespace(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be whitespace-only")
    return value


def _unique(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("values must be unique")
    return values


def _bounded_text(max_length: int):
    return Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=max_length),
        AfterValidator(_non_whitespace),
    ]


Text128 = _bounded_text(128)
Text255 = _bounded_text(255)
Text500 = _bounded_text(500)
Text1000 = _bounded_text(1_000)
Text2000 = _bounded_text(2_000)
Text4000 = _bounded_text(4_000)
Text16000 = _bounded_text(16_000)
Text64000 = _bounded_text(64_000)
ToolName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]{0,119}$",
    ),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CoordinatorRecentMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: Text16000


class CoordinatorAttachment(StrictModel):
    original_name: Text255
    relative_path: SafeClientRelativePath | None
    content_type: Text255
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    scan_summary: Text2000


class CoordinatorCompletedSubtask(StrictModel):
    key: SubtaskKey
    provider: LogicalProvider = Field(strict=False)
    summary: Text4000
    evidence_refs: list[Text128] = Field(max_length=64)

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, value: list[str]) -> list[str]:
        return _unique(value)


class CoordinatorEvidence(StrictModel):
    ref: Text128
    summary: Text4000
    confidence: float = Field(ge=0, le=1, strict=True)


class CoordinatorTool(StrictModel):
    name: ToolName
    risk_level: RiskLevel = Field(strict=False)
    description: Text500


class DecompositionContext(StrictModel):
    current_message: Text64000
    conversation_summary: Annotated[
        str, StringConstraints(strict=True, max_length=16_000)
    ]
    recent_messages: list[CoordinatorRecentMessage] = Field(max_length=40)
    attachments: list[CoordinatorAttachment] = Field(max_length=20)
    settings: ConversationSettings
    completed_subtasks: list[CoordinatorCompletedSubtask] = Field(max_length=64)
    evidence: list[CoordinatorEvidence] = Field(max_length=128)
    unresolved_questions: list[Text1000] = Field(max_length=32)
    available_tools: list[CoordinatorTool] = Field(max_length=128)
    budget: TurnBudgetSnapshot

    @field_validator("conversation_summary")
    @classmethod
    def reject_whitespace_summary(cls, value: str) -> str:
        if value and not value.strip():
            raise ValueError("must be empty or contain non-whitespace text")
        return value

    @field_validator("unresolved_questions")
    @classmethod
    def unique_questions(cls, value: list[str]) -> list[str]:
        return _unique(value)

    @field_validator("available_tools")
    @classmethod
    def unique_tool_names(
        cls, value: list[CoordinatorTool]
    ) -> list[CoordinatorTool]:
        if len(value) != len({tool.name for tool in value}):
            raise ValueError("tool names must be unique")
        return value


class CoordinatorResult(StrictModel):
    document: DecompositionDocument
    assignments: list[AssignmentDecision]
    model_response: ModelResponse


class CoordinatorAgent:
    def __init__(
        self, router: ModelRouter, assignment_policy: AssignmentPolicy
    ) -> None:
        self.router = router
        self.assignment_policy = assignment_policy

    async def decompose(
        self,
        context: DecompositionContext,
        *,
        plan_version: int,
        registered_tools: Iterable[str],
        authorized_tools: Iterable[str],
    ) -> CoordinatorResult:
        if not isinstance(context, DecompositionContext):
            raise TypeError("context must be a DecompositionContext")
        if (
            not isinstance(plan_version, int)
            or isinstance(plan_version, bool)
            or plan_version < 1
        ):
            raise ValueError("plan_version must be a positive integer")
        if context.budget.max_subtasks < 1:
            raise ValueError("max_subtasks budget must be at least 1")

        registered = frozenset(registered_tools)
        authorized = frozenset(authorized_tools)
        context_data = context.model_dump(mode="json")
        context_data["available_tools"] = [
            tool.model_dump(mode="json")
            for tool in context.available_tools
            if tool.name in registered and tool.name in authorized
        ]
        encoded_context = canonical_json_dumps(context_data)
        if (
            len(encoded_context.encode("utf-8"))
            > context.budget.max_context_tokens * 4
        ):
            raise ValueError("context budget exceeded")
        safe_context = redact_mapping(context_data)

        payload = redact_mapping(
            {
                "context": safe_context,
                "capability_matrix": {
                    provider.value: {
                        capability.value: level.value
                        for capability, level in capabilities.items()
                    }
                    for provider, capabilities in CAPABILITY_MATRIX.items()
                },
                "plan_version": plan_version,
                "budget_limits": context.budget.model_dump(mode="json"),
            }
        )
        request = ModelRequest(
            system=(
                "Decompose the bounded authorized conversation context into a "
                "strict dependency-valid plan. Return only the requested JSON."
            ),
            user=canonical_json_dumps(payload),
            response_schema=DecompositionDocument.model_json_schema(),
        )

        response = await self.router.complete(ModelStage.DECOMPOSE, request)
        safe_data = redact_mapping(response.data)
        document = DecompositionDocument.model_validate(safe_data)
        validate_decomposition(
            document,
            expected_plan_version=plan_version,
            max_subtasks=context.budget.max_subtasks,
        )
        assignments = self.assignment_policy.assign(
            document,
            available_providers=self.router.logical_assignment_providers(),
            registered_tools=registered,
            authorized_tools=authorized,
        )
        return CoordinatorResult(
            document=document,
            assignments=assignments,
            model_response=response.model_copy(update={"data": safe_data}),
        )


__all__ = [
    "CoordinatorAgent",
    "CoordinatorAttachment",
    "CoordinatorCompletedSubtask",
    "CoordinatorEvidence",
    "CoordinatorRecentMessage",
    "CoordinatorResult",
    "CoordinatorTool",
    "DecompositionContext",
]
