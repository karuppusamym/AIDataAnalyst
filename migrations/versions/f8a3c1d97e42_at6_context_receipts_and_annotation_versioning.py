"""AT-6: context receipts -- grounding-fragment digests on agent_run, and
append-only metadata_business_annotation_version

`metadata_business_annotation` used to carry its own content and be mutated
in place on every re-approval, which made it impossible to know what content
an `AgentRun` was grounded on once a later approval overwrote it -- see
`Docs/review-2026-08/atlan-context/00-decisions.md` §1 and tracker row AT-6.

This migration:

1. Creates `metadata_business_annotation_version` (append-only content,
   mirroring the existing `AssetDocumentation`/`AssetDocumentationVersion` and
   `GlossaryTerm`/`GlossaryTermVersion` parent/version split).
2. Copies every existing `metadata_business_annotation` row's content into a
   version 1 `APPROVED` row -- carrying forward what we have as the starting
   history rather than fabricating anything (the tracker's own "history
   cannot be backfilled" note is about *before* this migration, not about
   this one-time copy of already-live content).
3. Drops the now-superseded content columns from `metadata_business_annotation`,
   leaving it identity/pointer-only.
4. Adds `agent_run.grounding_fragment_digests` (JSON, default `[]`) for the
   per-fragment SHA-256 digests computed at grounding-assembly time.

Revision ID: f8a3c1d97e42
Revises: 09be3ab5b008
Create Date: 2026-09-01 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a3c1d97e42"
down_revision: str | Sequence[str] | None = "09be3ab5b008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_business_annotation_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("annotation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("business_name", sa.String(255), nullable=False),
        sa.Column("business_description", sa.Text(), nullable=False),
        sa.Column("table_role", sa.String(50), nullable=False),
        sa.Column("grain_statement", sa.String(1000), nullable=False),
        sa.Column("synonyms", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("suggested_questions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["annotation_id"], ["metadata_business_annotation.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("annotation_id", "version"),
    )
    op.create_index(
        op.f("ix_metadata_business_annotation_version_organization_id"),
        "metadata_business_annotation_version",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_metadata_business_annotation_version_annotation_id"),
        "metadata_business_annotation_version",
        ["annotation_id"],
    )
    op.create_index(
        "ix_metadata_business_annotation_version_org_status",
        "metadata_business_annotation_version",
        ["organization_id", "status"],
    )

    # Carry every currently-live annotation's content forward as its version 1,
    # APPROVED row -- this is copying what is already live, not backfilling
    # history that was never kept.
    op.execute(
        sa.text(
            """
            INSERT INTO metadata_business_annotation_version
                (id, organization_id, annotation_id, version, status,
                 business_name, business_description, table_role, grain_statement,
                 synonyms, suggested_questions, tags, confidence,
                 approved_by, approved_at, created_at, updated_at)
            SELECT gen_random_uuid(), organization_id, id, version, 'APPROVED',
                   business_name, business_description, table_role, grain_statement,
                   synonyms, suggested_questions, tags, confidence,
                   approved_by, approved_at, approved_at, approved_at
            FROM metadata_business_annotation
            """
        )
    )

    op.drop_column("metadata_business_annotation", "version")
    op.drop_column("metadata_business_annotation", "business_name")
    op.drop_column("metadata_business_annotation", "business_description")
    op.drop_column("metadata_business_annotation", "table_role")
    op.drop_column("metadata_business_annotation", "grain_statement")
    op.drop_column("metadata_business_annotation", "synonyms")
    op.drop_column("metadata_business_annotation", "suggested_questions")
    op.drop_column("metadata_business_annotation", "tags")
    op.drop_column("metadata_business_annotation", "confidence")
    op.drop_column("metadata_business_annotation", "approved_by")
    op.drop_column("metadata_business_annotation", "approved_at")

    op.add_column(
        "agent_run",
        sa.Column("grounding_fragment_digests", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("agent_run", "grounding_fragment_digests")

    op.add_column(
        "metadata_business_annotation", sa.Column("version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "metadata_business_annotation", sa.Column("business_name", sa.String(255), nullable=True)
    )
    op.add_column(
        "metadata_business_annotation", sa.Column("business_description", sa.Text(), nullable=True)
    )
    op.add_column(
        "metadata_business_annotation", sa.Column("table_role", sa.String(50), nullable=True)
    )
    op.add_column(
        "metadata_business_annotation",
        sa.Column("grain_statement", sa.String(1000), nullable=True),
    )
    op.add_column(
        "metadata_business_annotation",
        sa.Column("synonyms", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "metadata_business_annotation",
        sa.Column("suggested_questions", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "metadata_business_annotation",
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "metadata_business_annotation", sa.Column("confidence", sa.Float(), nullable=True)
    )
    op.add_column(
        "metadata_business_annotation", sa.Column("approved_by", sa.String(255), nullable=True)
    )
    op.add_column(
        "metadata_business_annotation",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        sa.text(
            """
            UPDATE metadata_business_annotation AS a
            SET version = v.version,
                business_name = v.business_name,
                business_description = v.business_description,
                table_role = v.table_role,
                grain_statement = v.grain_statement,
                synonyms = v.synonyms,
                suggested_questions = v.suggested_questions,
                tags = v.tags,
                confidence = v.confidence,
                approved_by = v.approved_by,
                approved_at = v.approved_at
            FROM metadata_business_annotation_version AS v
            WHERE v.annotation_id = a.id AND v.status = 'APPROVED'
            """
        )
    )

    op.alter_column("metadata_business_annotation", "version", nullable=False)
    op.alter_column("metadata_business_annotation", "business_name", nullable=False)
    op.alter_column("metadata_business_annotation", "business_description", nullable=False)
    op.alter_column("metadata_business_annotation", "table_role", nullable=False)
    op.alter_column("metadata_business_annotation", "grain_statement", nullable=False)
    op.alter_column("metadata_business_annotation", "confidence", nullable=False)
    op.alter_column("metadata_business_annotation", "approved_by", nullable=False)
    op.alter_column("metadata_business_annotation", "approved_at", nullable=False)

    op.drop_table("metadata_business_annotation_version")
