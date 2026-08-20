"""Bind model calls to their logical task attempt.

Revision ID: 20260815_04
Revises: 20260815_03
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260815_04"
down_revision: str | None = "20260815_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_calls",
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("model_calls", "attempt")
