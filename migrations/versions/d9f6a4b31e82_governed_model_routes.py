"""governed model route configurations

Revision ID: d9f6a4b31e82
Revises: c8e5f3a20d71
Create Date: 2026-08-26 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9f6a4b31e82"
down_revision: str | Sequence[str] | None = "c8e5f3a20d71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_route_configuration",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("route_key", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("provider_type", sa.String(length=50), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("endpoint_alias", sa.String(length=255), nullable=False),
        sa.Column("credential_reference", sa.String(length=1000), nullable=True),
        sa.Column("data_residency", sa.String(length=100), nullable=False),
        sa.Column("retention_policy", sa.String(length=50), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_model_route_configuration_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_route_configuration")),
        sa.UniqueConstraint(
            "organization_id",
            "route_key",
            "version",
            name=op.f("uq_model_route_configuration_organization_id_route_key_version"),
        ),
    )
    op.create_index(
        op.f("ix_model_route_configuration_organization_id"),
        "model_route_configuration",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_model_route_org_status",
        "model_route_configuration",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_model_route_org_status", table_name="model_route_configuration")
    op.drop_index(
        op.f("ix_model_route_configuration_organization_id"),
        table_name="model_route_configuration",
    )
    op.drop_table("model_route_configuration")
