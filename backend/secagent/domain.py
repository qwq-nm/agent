from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


SAFE_FINISH_REASONS = frozenset(
    {"stop", "length", "tool_calls", "content_filter", "function_call"}
)
MAX_DB_INTEGER = 2_147_483_647


def normalize_finish_reason(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        return "unknown"
    normalized = value.strip().lower()
    return normalized if normalized in SAFE_FINISH_REASONS else "unknown"


def normalize_token_count(value: object) -> int:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_DB_INTEGER
    ):
        return value
    return 0


class TaskScene(StrEnum):
    INCIDENT_RESPONSE = "incident_response"
    SOURCE_AUDIT = "source_audit"
    WEB_ANALYSIS = "web_analysis"


class TaskStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PARSED = "parsed"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UserRole(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FORBIDDEN = "forbidden"


class ModelStage(StrEnum):
    TASK_PARSE = "task_parse"
    PLAN = "plan"
    CRITIC = "critic"
    REPORT = "report"


class RouteMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class TaskCreate(BaseModel):
    goal: str = Field(min_length=3, max_length=4000)
    authorization_scope: str = Field(min_length=3, max_length=2000)
    route_mode: RouteMode = RouteMode.AUTO
    preferred_model: str | None = None
    scene_hint: TaskScene | None = None
    target_url: str | None = None


class TaskRead(TaskCreate):
    id: str
    owner_id: str | None
    scene: TaskScene | None = None
    status: TaskStatus
    is_demo: bool = False


class ModelRequest(BaseModel):
    stage: ModelStage | None = None
    system: str
    user: str
    response_schema: dict[str, Any]


class ModelResponse(BaseModel):
    provider: str
    model: str
    data: dict[str, Any]
    latency_ms: int
    is_demo: bool = False
    request_id: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retry_count: int = 0

    @field_validator("finish_reason", mode="before")
    @classmethod
    def validate_finish_reason(cls, value: object) -> str | None:
        return normalize_finish_reason(value)

    @field_validator("prompt_tokens", "completion_tokens", mode="before")
    @classmethod
    def validate_token_count(cls, value: object) -> int:
        return normalize_token_count(value)


class PlanStep(BaseModel):
    name: str
    purpose: str
    tool_name: str
    params: dict[str, Any]
    risk_level: RiskLevel
    need_human_confirm: bool = False


class ToolResult(BaseModel):
    success: bool
    summary: str
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class ParsedTask(BaseModel):
    scene: TaskScene
    goal: str
    inputs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    authorization_scope: str
    risk_level: RiskLevel
    expected_outputs: list[str] = Field(default_factory=list)


class CriticDecision(BaseModel):
    is_complete: bool
    confidence: float = Field(ge=0, le=1)
    reason: str
    missing_evidence: list[str] = Field(default_factory=list)


class TaskRunResult(BaseModel):
    task_id: str
    status: TaskStatus
    is_demo: bool
    report: str | None = None
