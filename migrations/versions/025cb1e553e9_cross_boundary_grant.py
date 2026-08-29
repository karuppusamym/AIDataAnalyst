"""cross boundary grant (ADR-0017 phase 4)

Adds cross_boundary_grant: the explicit, audited permission a target
data_domain needs to traverse across the boundary into a source data_domain
(INV-5 — deny-by-default, never inherited). A grant starts PENDING_APPROVAL
and is activated through the existing generic governance_review maker-checker
queue (object_type="CROSS_BOUNDARY_GRANT", see semantic_api.decide_governance_
review) rather than a bespoke approval workflow. No column is added to any
existing table by this migration — same-domain traversal is unaffected, and
nothing is backfilled because no cross-domain edges are produced yet
(that lands with ADR-0017 phase 5's cross-source relationship inference).

Revision ID: 025cb1e553e9
Revises: d3f7a5c8e1b4
Create Date: 2026-08-29 22:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "025cb1e553e9"
down_revision: str | None = "d3f7a5c8e1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cross_boundary_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("source_data_domain_id", sa.Uuid(), nullable=False),
        sa.Column("target_data_domain_id", sa.Uuid(), nullable=False),
        sa.Column("edge_kinds", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "status", sa.String(length=30), nullable=False, server_default="PENDING_APPROVAL"
        ),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_cross_boundary_grant_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_data_domain_id"],
            ["data_domain.id"],
            name=op.f("fk_cross_boundary_grant_source_data_domain_id_data_domain"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_data_domain_id"],
            ["data_domain.id"],
            name=op.f("fk_cross_boundary_grant_target_data_domain_id_data_domain"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cross_boundary_grant")),
    )
    op.create_index(
        op.f("ix_cross_boundary_grant_organization_id"),
        "cross_boundary_grant",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cross_boundary_grant_source_data_domain_id"),
        "cross_boundary_grant",
        ["source_data_domain_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cross_boundary_grant_target_data_domain_id"),
        "cross_boundary_grant",
        ["target_data_domain_id"],
        unique=False,
    )
    op.create_index(
        "ix_cross_boundary_grant_org_pair",
        "cross_boundary_grant",
        ["organization_id", "source_data_domain_id", "target_data_domain_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cross_boundary_grant_org_pair", table_name="cross_boundary_grant")
    op.drop_index(
        op.f("ix_cross_boundary_grant_target_data_domain_id"), table_name="cross_boundary_grant"
    )
    op.drop_index(
        op.f("ix_cross_boundary_grant_source_data_domain_id"), table_name="cross_boundary_grant"
    )
    op.drop_index(
        op.f("ix_cross_boundary_grant_organization_id"), table_name="cross_boundary_grant"
    )
    op.drop_table("cross_boundary_grant")
