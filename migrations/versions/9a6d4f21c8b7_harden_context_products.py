"""harden context products

Revision ID: 9a6d4f21c8b7
Revises: 4c8e2a71b903
Create Date: 2026-08-29 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a6d4f21c8b7"
down_revision: str | Sequence[str] | None = "4c8e2a71b903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_context_product_version_positive",
        "context_product_version",
        "version > 0",
    )
    op.create_check_constraint(
        "ck_context_product_version_status",
        "context_product_version",
        "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPERSEDED', "
        "'REJECTED', 'DEPRECATION_REVIEW', 'DEPRECATED')",
    )
    op.create_index(
        "uq_context_product_version_one_published",
        "context_product_version",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )

    op.create_table(
        "context_product_role_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("role_name", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_context_product_role_binding_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["context_product_version_id"],
            ["context_product_version.id"],
            name=op.f(
                "fk_context_product_role_binding_context_product_version_id_context_product_version"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_product_role_binding")),
        sa.UniqueConstraint(
            "context_product_version_id",
            "role_name",
            name="uq_context_product_role_binding_version_role",
        ),
    )
    op.create_index(
        op.f("ix_context_product_role_binding_organization_id"),
        "context_product_role_binding",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_role_binding_context_product_version_id"),
        "context_product_role_binding",
        ["context_product_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_context_product_role_binding_org_role",
        "context_product_role_binding",
        ["organization_id", "role_name", "context_product_version_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            """
            INSERT INTO context_product_role_binding
                (id, organization_id, context_product_version_id, role_name)
            SELECT gen_random_uuid(), version.organization_id, version.id, role_name
            FROM context_product_version AS version
            CROSS JOIN LATERAL json_array_elements_text(version.allowed_consumer_roles) role_name
            ON CONFLICT DO NOTHING
            """
        )
    )

    op.create_table(
        "context_product_consumption_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("principal_type", sa.String(length=30), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("correlation_id", sa.String(length=100), nullable=False),
        sa.Column("product_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("policy_decision", sa.String(length=30), nullable=False),
        sa.Column("quality_snapshot", sa.JSON(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_context_product_consumption_edge_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["context_product_version_id"],
            ["context_product_version.id"],
            name=op.f(
                "fk_context_product_consumption_edge_context_product_version_id_context_product_version"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_product_consumption_edge")),
    )
    op.create_index(
        op.f("ix_context_product_consumption_edge_organization_id"),
        "context_product_consumption_edge",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_consumption_edge_context_product_version_id"),
        "context_product_consumption_edge",
        ["context_product_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_consumption_edge_correlation_id"),
        "context_product_consumption_edge",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_context_product_consumption_version_time",
        "context_product_consumption_edge",
        ["context_product_version_id", "consumed_at"],
        unique=False,
    )
    op.create_index(
        "ix_context_product_consumption_org_principal_time",
        "context_product_consumption_edge",
        ["organization_id", "principal_id", "consumed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_product_consumption_org_principal_time",
        table_name="context_product_consumption_edge",
    )
    op.drop_index(
        "ix_context_product_consumption_version_time",
        table_name="context_product_consumption_edge",
    )
    op.drop_index(
        op.f("ix_context_product_consumption_edge_correlation_id"),
        table_name="context_product_consumption_edge",
    )
    op.drop_index(
        op.f("ix_context_product_consumption_edge_context_product_version_id"),
        table_name="context_product_consumption_edge",
    )
    op.drop_index(
        op.f("ix_context_product_consumption_edge_organization_id"),
        table_name="context_product_consumption_edge",
    )
    op.drop_table("context_product_consumption_edge")
    op.drop_index(
        "ix_context_product_role_binding_org_role",
        table_name="context_product_role_binding",
    )
    op.drop_index(
        op.f("ix_context_product_role_binding_context_product_version_id"),
        table_name="context_product_role_binding",
    )
    op.drop_index(
        op.f("ix_context_product_role_binding_organization_id"),
        table_name="context_product_role_binding",
    )
    op.drop_table("context_product_role_binding")
    op.drop_index(
        "uq_context_product_version_one_published",
        table_name="context_product_version",
    )
    op.drop_constraint(
        "ck_context_product_version_status",
        "context_product_version",
        type_="check",
    )
    op.drop_constraint(
        "ck_context_product_version_positive",
        "context_product_version",
        type_="check",
    )
