from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from secagent.db import Base


def new_id() -> str:
    return str(uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class RefreshSessionRow(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replaced_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("refresh_sessions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    goal: Mapped[str] = mapped_column(Text)
    authorization_scope: Mapped[str] = mapped_column(Text)
    route_mode: Mapped[str] = mapped_column(String(16))
    preferred_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    scene_hint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    status_version: Mapped[int] = mapped_column(Integer, default=0)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0)
    max_model_calls: Mapped[int] = mapped_column(Integer, default=8)
    max_input_tokens: Mapped[int] = mapped_column(Integer, default=120_000)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=24_000)
    max_steps: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    budget_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    orchestration_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}"
    )
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class JobRunRow(Base):
    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    broker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    command_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class TaskEventRow(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class TaskStepRow(Base):
    __tablename__ = "task_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "step_index"),
        UniqueConstraint("task_id", "idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    step_index: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(Text)
    tool_name: Mapped[str] = mapped_column(String(120))
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16))
    need_human_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    model_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    route_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ModelCallRow(Base):
    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(120), default="unknown")
    stage: Mapped[str] = mapped_column(String(40))
    route_reason: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_summary: Mapped[str] = mapped_column(Text)
    finish_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"), nullable=True, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(120))
    params_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class EvidenceRow(Base):
    __tablename__ = "evidences"
    __table_args__ = (UniqueConstraint("task_id", "sha256", "source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="CASCADE"), nullable=True, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    file_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class ToolCallEvidenceRow(Base):
    __tablename__ = "tool_call_evidences"

    tool_call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidences.id", ondelete="CASCADE"), primary_key=True
    )
    step_attempt: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(120))
    risk_level: Mapped[str] = mapped_column(String(16))
    params_summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReportRow(Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("task_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    evidence_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(80), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )
