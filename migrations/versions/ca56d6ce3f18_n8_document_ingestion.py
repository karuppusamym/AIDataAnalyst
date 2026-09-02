"""n8 document ingestion

Adds `document`, `document_section`, `document_mapping`, `document_claim`
(N8): the data-dictionary-spreadsheet special case of document ingestion --
upload, parse, structural map, and reviewed description claims. See
`aida.document_ingestion` and `aida.models.Document`/`DocumentSection`/
`DocumentMapping`/`DocumentClaim` for the full design.

Revision ID: ca56d6ce3f18
Revises: 62341fa9f017
Create Date: 2026-09-01 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ca56d6ce3f18"
down_revision: str | Sequence[str] | None = "62341fa9f017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=20), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("section_count", sa.Integer(), nullable=False),
        sa.Column("parse_error_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("media_type IN ('CSV')", name=op.f("ck_document_media_type_is_supported")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_document_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_document_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document")),
    )
    op.create_index(
        op.f("ix_document_organization_id"), "document", ["organization_id"], unique=False
    )
    op.create_index(op.f("ix_document_project_id"), "document", ["project_id"], unique=False)
    op.create_index(
        "ix_document_org_project", "document", ["organization_id", "project_id"], unique=False
    )

    op.create_table(
        "document_section",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("raw_schema_name", sa.String(length=255), nullable=True),
        sa.Column("raw_table_name", sa.String(length=255), nullable=False),
        sa.Column("raw_column_name", sa.String(length=255), nullable=True),
        sa.Column("raw_description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_document_section_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_document_section_document_id_document"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_section")),
        sa.UniqueConstraint(
            "document_id", "ordinal", name=op.f("uq_document_section_document_id")
        ),
    )
    op.create_index(
        op.f("ix_document_section_organization_id"),
        "document_section",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_section_document_id"), "document_section", ["document_id"], unique=False
    )

    op.create_table(
        "document_mapping",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("document_section_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=10), nullable=False),
        sa.Column("subject_id", sa.String(length=100), nullable=True),
        sa.Column("mapping_kind", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('TABLE', 'COLUMN')",
            name=op.f("ck_document_mapping_subject_type_is_supported"),
        ),
        sa.CheckConstraint(
            "mapping_kind IN ('STRUCTURAL', 'SUGGESTED', 'UNMATCHED')",
            name=op.f("ck_document_mapping_mapping_kind_is_supported"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_document_mapping_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_section_id"],
            ["document_section.id"],
            name=op.f("fk_document_mapping_document_section_id_document_section"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_mapping")),
        sa.UniqueConstraint(
            "document_section_id", name=op.f("uq_document_mapping_document_section_id")
        ),
    )
    op.create_index(
        op.f("ix_document_mapping_organization_id"),
        "document_mapping",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_mapping_document_section_id"),
        "document_mapping",
        ["document_section_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_mapping_subject_id"), "document_mapping", ["subject_id"], unique=False
    )

    op.create_table(
        "document_claim",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("document_section_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=10), nullable=False),
        sa.Column("subject_id", sa.String(length=100), nullable=False),
        sa.Column("predicate", sa.String(length=20), nullable=False),
        sa.Column("object_value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('TABLE', 'COLUMN')",
            name=op.f("ck_document_claim_subject_type_is_supported"),
        ),
        sa.CheckConstraint(
            "predicate IN ('DESCRIBES')", name=op.f("ck_document_claim_predicate_is_supported")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_document_claim_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_section_id"],
            ["document_section.id"],
            name=op.f("fk_document_claim_document_section_id_document_section"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_document_claim_governance_review_id_governance_review"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_claim")),
        sa.UniqueConstraint(
            "governance_review_id", name=op.f("uq_document_claim_governance_review_id")
        ),
    )
    op.create_index(
        op.f("ix_document_claim_organization_id"),
        "document_claim",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_claim_document_section_id"),
        "document_claim",
        ["document_section_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_claim_subject_id"), "document_claim", ["subject_id"], unique=False
    )
    op.create_index(
        "ix_document_claim_org_status", "document_claim", ["organization_id", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_document_claim_org_status", table_name="document_claim")
    op.drop_index(op.f("ix_document_claim_subject_id"), table_name="document_claim")
    op.drop_index(op.f("ix_document_claim_document_section_id"), table_name="document_claim")
    op.drop_index(op.f("ix_document_claim_organization_id"), table_name="document_claim")
    op.drop_table("document_claim")

    op.drop_index(op.f("ix_document_mapping_subject_id"), table_name="document_mapping")
    op.drop_index(op.f("ix_document_mapping_document_section_id"), table_name="document_mapping")
    op.drop_index(op.f("ix_document_mapping_organization_id"), table_name="document_mapping")
    op.drop_table("document_mapping")

    op.drop_index(op.f("ix_document_section_document_id"), table_name="document_section")
    op.drop_index(op.f("ix_document_section_organization_id"), table_name="document_section")
    op.drop_table("document_section")

    op.drop_index("ix_document_org_project", table_name="document")
    op.drop_index(op.f("ix_document_project_id"), table_name="document")
    op.drop_index(op.f("ix_document_organization_id"), table_name="document")
    op.drop_table("document")
