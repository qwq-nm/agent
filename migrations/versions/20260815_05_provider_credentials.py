"""Store encrypted provider credentials and explicit clear tombstones.

Revision ID: 20260815_05
Revises: 20260815_04
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260815_05"
down_revision: str | None = "20260815_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("key_hint", sa.String(length=16), nullable=True),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="configured",
        ),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("provider"),
    )
    op.create_index(
        "ix_provider_credentials_updated_by",
        "provider_credentials",
        ["updated_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_credentials_updated_by", table_name="provider_credentials"
    )
    op.drop_table("provider_credentials")
