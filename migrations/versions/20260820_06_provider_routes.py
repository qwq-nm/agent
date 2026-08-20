"""Add provider runtime routes."""

from alembic import op
import sqlalchemy as sa


revision = "20260820_06"
down_revision = "20260815_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_routes",
        sa.Column("provider", sa.String(length=32), primary_key=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("api_style", sa.String(length=32), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_provider_routes_updated_by", "provider_routes", ["updated_by"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_routes_updated_by", table_name="provider_routes")
    op.drop_table("provider_routes")
