"""Add durable subtask DAG schema.

Revision ID: 20260830_10
Revises: 20260826_09
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260830_10"
down_revision: str | None = "20260826_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _job_link_columns() -> list[sa.Column]:
    return [
        sa.Column("job_kind", sa.String(length=32), nullable=True),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("subtask_id", sa.String(length=36), nullable=True),
        sa.Column("parent_job_run_id", sa.String(length=36), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "subtasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "required_capabilities_json", sa.Text(), server_default="[]",
            nullable=False,
        ),
        sa.Column("proposed_provider", sa.String(length=16), nullable=False),
        sa.Column("assigned_provider", sa.String(length=16), nullable=False),
        sa.Column("route_reason_code", sa.String(length=80), nullable=False),
        sa.Column("route_reason", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("expected_output", sa.Text(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending_dependency",
            nullable=False,
        ),
        sa.Column("status_version", sa.Integer(), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["conversation_turns.id"],
            name="fk_subtasks_turn_id_conversation_turns",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("turn_id", "key", name="uq_subtasks_turn_key"),
    )
    op.create_index(
        "ix_subtasks_turn_id", "subtasks", ["turn_id"], unique=False
    )
    op.create_index(
        "ix_subtasks_turn_id_status", "subtasks", ["turn_id", "status"], unique=False
    )

    op.create_table(
        "subtask_dependencies",
        sa.Column("subtask_id", sa.String(length=36), nullable=False),
        sa.Column("dependency_subtask_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["subtasks.id"],
            name="fk_subtask_dependencies_subtask_id_subtasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dependency_subtask_id"],
            ["subtasks.id"],
            name="fk_subtask_dependencies_dependency_subtask_id_subtasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("subtask_id", "dependency_subtask_id"),
        sa.UniqueConstraint(
            "subtask_id",
            "dependency_subtask_id",
            name="uq_subtask_dependencies_pair",
        ),
    )

    op.create_table(
        "subtask_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subtask_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["subtasks.id"],
            name="fk_subtask_attempts_subtask_id_subtasks",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "subtask_id", "attempt", name="uq_subtask_attempts_subtask_attempt"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_subtask_attempts_idempotency_key"
        ),
    )
    op.create_index(
        "ix_subtask_attempts_subtask_id",
        "subtask_attempts",
        ["subtask_id"],
        unique=False,
    )

    op.create_table(
        "subtask_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("subtask_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("claims_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("inference_notes_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("unresolved_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["subtask_attempts.id"],
            name="fk_subtask_results_attempt_id_subtask_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["subtasks.id"],
            name="fk_subtask_results_subtask_id_subtasks",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_subtask_results_attempt_id", "subtask_results", ["attempt_id"], unique=True
    )
    op.create_index(
        "ix_subtask_results_subtask_id", "subtask_results", ["subtask_id"], unique=False
    )

    op.create_table(
        "model_failures",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("subtask_id", sa.String(length=36), nullable=True),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="waiting_decision",
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=24), nullable=True),
        sa.Column("decided_by", sa.String(length=36), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_model_failures_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["conversation_turns.id"],
            name="fk_model_failures_turn_id_conversation_turns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["subtasks.id"],
            name="fk_model_failures_subtask_id_subtasks",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name="fk_model_failures_decided_by_users",
            ondelete="RESTRICT",
        ),
    )
    for column in ("conversation_id", "turn_id", "subtask_id", "decided_by"):
        op.create_index(
            f"ix_model_failures_{column}", "model_failures", [column], unique=False
        )

    with op.batch_alter_table("job_runs") as batch:
        for column in _job_link_columns():
            batch.add_column(column)
        batch.create_index("ix_job_runs_turn_id", ["turn_id"], unique=False)
        batch.create_index("ix_job_runs_subtask_id", ["subtask_id"], unique=False)

    dag_link = [
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("subtask_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
    ]
    with op.batch_alter_table("model_calls") as batch:
        for column in dag_link:
            batch.add_column(column)
        batch.create_index("ix_model_calls_turn_id", ["turn_id"], unique=False)
        batch.create_index("ix_model_calls_subtask_id", ["subtask_id"], unique=False)
    with op.batch_alter_table("tool_calls") as batch:
        for column in dag_link:
            batch.add_column(column)
        batch.create_index("ix_tool_calls_turn_id", ["turn_id"], unique=False)
        batch.create_index("ix_tool_calls_subtask_id", ["subtask_id"], unique=False)
    with op.batch_alter_table("evidences") as batch:
        for column in dag_link:
            batch.add_column(column)
        batch.create_index("ix_evidences_turn_id", ["turn_id"], unique=False)
        batch.create_index("ix_evidences_subtask_id", ["subtask_id"], unique=False)
    with op.batch_alter_table("approvals") as batch:
        batch.alter_column(
            "step_id",
            existing_type=sa.String(length=36),
            nullable=True,
            existing_foreign_key="fk_approvals_step_id_task_steps",
        )
        for column in dag_link:
            batch.add_column(column)
        batch.create_index("ix_approvals_turn_id", ["turn_id"], unique=False)
        batch.create_index("ix_approvals_subtask_id", ["subtask_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_index("ix_approvals_subtask_id")
        batch.drop_index("ix_approvals_turn_id")
        for name in ("attempt_id", "subtask_id", "turn_id"):
            batch.drop_column(name)
        batch.alter_column("step_id", existing_type=sa.String(length=36), nullable=False)
    with op.batch_alter_table("evidences") as batch:
        batch.drop_index("ix_evidences_subtask_id")
        batch.drop_index("ix_evidences_turn_id")
        for name in ("attempt_id", "subtask_id", "turn_id"):
            batch.drop_column(name)
    with op.batch_alter_table("tool_calls") as batch:
        batch.drop_index("ix_tool_calls_subtask_id")
        batch.drop_index("ix_tool_calls_turn_id")
        for name in ("attempt_id", "subtask_id", "turn_id"):
            batch.drop_column(name)
    with op.batch_alter_table("model_calls") as batch:
        batch.drop_index("ix_model_calls_subtask_id")
        batch.drop_index("ix_model_calls_turn_id")
        for name in ("attempt_id", "subtask_id", "turn_id"):
            batch.drop_column(name)
    with op.batch_alter_table("job_runs") as batch:
        batch.drop_index("ix_job_runs_subtask_id")
        batch.drop_index("ix_job_runs_turn_id")
        for column in reversed(_job_link_columns()):
            batch.drop_column(column.name)

    op.drop_table("model_failures")
    op.drop_table("subtask_results")
    op.drop_table("subtask_attempts")
    op.drop_table("subtask_dependencies")
    op.drop_index("ix_subtasks_turn_id_status", table_name="subtasks")
    op.drop_index("ix_subtasks_turn_id", table_name="subtasks")
    op.drop_table("subtasks")
