"""Persist task orchestration checkpoints and budget boundaries.

Revision ID: 20260815_03
Revises: 20260815_02
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260815_03"
down_revision: str | None = "20260815_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("max_steps", sa.Integer(), server_default="20", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("budget_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "orchestration_json", sa.Text(), server_default="{}", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "orchestration_json")
    op.drop_column("tasks", "budget_deadline_at")
    op.drop_column("tasks", "max_steps")
