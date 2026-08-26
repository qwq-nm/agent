"""Add durable conversation primitives.

Revision ID: 20260826_09
Revises: 20260826_08
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_09"
down_revision: str | None = "20260826_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "settings_json", sa.Text(), server_default="{}", nullable=False
        ),
        sa.Column("active_turn_id", sa.String(length=36), nullable=True),
        sa.Column(
            "next_message_sequence",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "next_turn_plan_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "next_message_sequence >= 1",
            name="ck_conversations_next_message_sequence_ge_1",
        ),
        sa.CheckConstraint(
            "next_turn_plan_version >= 1",
            name="ck_conversations_next_turn_plan_version_ge_1",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_conversations_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_owner_id", "conversations", ["owner_id"], unique=False
    )
    op.create_index(
        "ix_conversations_active_turn_id",
        "conversations",
        ["active_turn_id"],
        unique=False,
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="completed",
            nullable=False,
        ),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
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
        sa.CheckConstraint(
            "sequence >= 1", name="ck_conversation_messages_sequence_ge_1"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_messages_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_conversation_messages_conversation_sequence",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_conversation_messages_conversation_idempotency_key",
        ),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_messages_turn_id",
        "conversation_messages",
        ["turn_id"],
        unique=False,
    )

    op.create_table(
        "message_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("storage_ref", sa.String(length=500), nullable=False),
        sa.Column("relative_path", sa.String(length=1000), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="ready",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name="ck_message_attachments_size_bytes_ge_0"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["conversation_messages.id"],
            name="fk_message_attachments_message_id_conversation_messages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_message_attachments_message_id",
        "message_attachments",
        ["message_id"],
        unique=False,
    )

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="created",
            nullable=False,
        ),
        sa.Column("budget_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("replan_from_turn_id", sa.String(length=36), nullable=True),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "plan_version >= 1", name="ck_conversation_turns_plan_version_ge_1"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_turns_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_id"],
            ["conversation_messages.id"],
            name="fk_conversation_turns_trigger_message_id_conversation_messages",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_conversation_turns_task_id_tasks",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["replan_from_turn_id"],
            ["conversation_turns.id"],
            name="fk_conversation_turns_replan_from_turn_id_conversation_turns",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "plan_version",
            name="uq_conversation_turns_conversation_plan_version",
        ),
    )
    op.create_index(
        "ix_conversation_turns_conversation_id",
        "conversation_turns",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_turns_trigger_message_id",
        "conversation_turns",
        ["trigger_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_turns_task_id",
        "conversation_turns",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_turns_replan_from_turn_id",
        "conversation_turns",
        ["replan_from_turn_id"],
        unique=False,
    )

    op.create_table(
        "conversation_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("subtask_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_events_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["conversation_turns.id"],
            name="fk_conversation_events_turn_id_conversation_turns",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_conversation_events_conversation_id",
        "conversation_events",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_events_turn_id",
        "conversation_events",
        ["turn_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_events_subtask_id",
        "conversation_events",
        ["subtask_id"],
        unique=False,
    )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.create_foreign_key(
            "fk_conversations_active_turn_id_conversation_turns",
            "conversation_turns",
            ["active_turn_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("conversation_messages") as batch_op:
        batch_op.create_foreign_key(
            "fk_conversation_messages_turn_id_conversation_turns",
            "conversation_turns",
            ["turn_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation_messages") as batch_op:
        batch_op.drop_constraint(
            "fk_conversation_messages_turn_id_conversation_turns",
            type_="foreignkey",
        )
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_constraint(
            "fk_conversations_active_turn_id_conversation_turns",
            type_="foreignkey",
        )

    op.drop_table("conversation_events")
    op.drop_table("message_attachments")
    op.drop_table("conversation_turns")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
