"""Add task safety mode.

Revision ID: 20260821_07
Revises: 20260820_06
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260821_07"
down_revision: str | None = "20260820_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "safety_mode",
            sa.String(length=24),
            server_default="conservative",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "safety_mode")
