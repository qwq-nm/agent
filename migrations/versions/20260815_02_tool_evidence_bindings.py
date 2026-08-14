"""Bind canonical evidence to the tool call and step attempt that observed it.

Revision ID: 20260815_02
Revises: 20260814_01
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260815_02"
down_revision: str | None = "20260814_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_call_evidences",
        sa.Column("tool_call_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("step_attempt", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tool_call_id"], ["tool_calls.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidences.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tool_call_id", "evidence_id"),
    )


def downgrade() -> None:
    op.drop_table("tool_call_evidences")
