"""ontology definitions

Governed ontology v1 (`aida.ontology_models`, `aida.ontology_api`):
`ontology_head` names each organization's ontologies and which version of each
is published; `ontology_version` holds a version's typed definition, its status
and its governance review. The models shipped in bc6ab78 without a migration,
which the ORM-drift gate reported as two missing tables. This creates exactly
what the models declare.

Revision ID: 7c2d94e1b8a3
Revises: e3b8f14c6a92
Create Date: 2026-09-11 02:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c2d94e1b8a3"
down_revision: str | Sequence[str] | None = "e3b8f14c6a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ontology_head",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ontology_key", sa.String(length=100), nullable=False),
        sa.Column("last_version", sa.Integer(), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_ontology_head_organization_id_organization"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ontology_head")),
        sa.UniqueConstraint(
            "organization_id", "ontology_key", name=op.f("uq_ontology_head_organization_id")
        ),
    )
    op.create_table(
        "ontology_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ontology_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED')",
            name=op.f("ck_ontology_version_ontology_status"),
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_ontology_version_governance_review_id_governance_review"),
        ),
        sa.ForeignKeyConstraint(
            ["ontology_id"],
            ["ontology_head.id"],
            name=op.f("fk_ontology_version_ontology_id_ontology_head"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_ontology_version_organization_id_organization"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ontology_version")),
        sa.UniqueConstraint(
            "ontology_id", "version", name=op.f("uq_ontology_version_ontology_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("ontology_version")
    op.drop_table("ontology_head")
