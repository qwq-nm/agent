from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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


class ProviderCredentialRow(Base):
    __tablename__ = "provider_credentials"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), default="configured", server_default="configured"
    )
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ProviderRouteRow(Base):
    __tablename__ = "provider_routes"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    base_url: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(120))
    api_style: Mapped[str] = mapped_column(String(32))
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
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
    safety_mode: Mapped[str] = mapped_column(
        String(24), default="conservative", server_default="conservative"
    )
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


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "next_message_sequence >= 1",
            name="ck_conversations_next_message_sequence_ge_1",
        ),
        CheckConstraint(
            "next_turn_plan_version >= 1",
            name="ck_conversations_next_turn_plan_version_ge_1",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_conversations_owner_id_users",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(
        String(32), default="active", server_default="active"
    )
    settings_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}"
    )
    active_turn_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "conversation_turns.id",
            name="fk_conversations_active_turn_id_conversation_turns",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    next_message_sequence: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    next_turn_plan_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
        onupdate=now_utc,
        server_default=func.now(),
    )


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(
            "sequence >= 1", name="ck_conversation_messages_sequence_ge_1"
        ),
        UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_conversation_messages_conversation_sequence",
        ),
        UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_conversation_messages_conversation_idempotency_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversations.id",
            name="fk_conversation_messages_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default="completed", server_default="completed"
    )
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "conversation_turns.id",
            name="fk_conversation_messages_turn_id_conversation_turns",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
        onupdate=now_utc,
        server_default=func.now(),
    )


class MessageAttachmentRow(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (
        CheckConstraint(
            "size_bytes >= 0", name="ck_message_attachments_size_bytes_ge_0"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversation_messages.id",
            name="fk_message_attachments_message_id_conversation_messages",
            ondelete="CASCADE",
        ),
        index=True,
    )
    original_name: Mapped[str] = mapped_column(String(255))
    storage_ref: Mapped[str] = mapped_column(String(500))
    relative_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default="ready", server_default="ready"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, server_default=func.now()
    )


class ConversationTurnRow(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint(
            "plan_version >= 1", name="ck_conversation_turns_plan_version_ge_1"
        ),
        UniqueConstraint(
            "conversation_id",
            "plan_version",
            name="uq_conversation_turns_conversation_plan_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversations.id",
            name="fk_conversation_turns_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        index=True,
    )
    trigger_message_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversation_messages.id",
            name="fk_conversation_turns_trigger_message_id_conversation_messages",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "tasks.id",
            name="fk_conversation_turns_task_id_tasks",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    plan_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(32), default="created", server_default="created"
    )
    budget_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    replan_from_turn_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "conversation_turns.id",
            name="fk_conversation_turns_replan_from_turn_id_conversation_turns",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
        onupdate=now_utc,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConversationEventRow(Base):
    __tablename__ = "conversation_events"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversations.id",
            name="fk_conversation_events_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        index=True,
    )
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "conversation_turns.id",
            name="fk_conversation_events_turn_id_conversation_turns",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    subtask_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, server_default=func.now()
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
