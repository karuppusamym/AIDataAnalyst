"""R11-FP08: an Atlas-authored, versioned description store for a routine

Revision ID: b7d24e1c8f03
Revises: c9f3b6a1d472
Create Date: 2026-09-16

Tables and views already had the whole description lifecycle -- a draft, an
append-only versioned store, a review type, a withdrawal and a reinstatement.
A routine had none of it: `asset_documentation` is keyed by `table_id` and a
routine is not a table, so the only thing the platform could say about a
procedure was the source system's own comment, overwritten by every rescan.

Three tables, in the shape the existing pairs already have (see
`e2f7f81de0a1_asset_description_drafts` and
`d5e8a2c7f9b1_column_description_drafts`), plus one widened check constraint:

* `routine_documentation` -- the identity row, one per routine.
* `routine_documentation_version` -- append-only content. Its
  `source_definition_version_id` is the one column the table and column
  versions have no analogue for: a routine has a real immutable
  definition-version table, so a published description can *name* the body it
  describes instead of only carrying a digest of it. Drift is then a version-id
  comparison rather than a re-derived hash, and a reinstatement can refuse to
  republish prose about a body that has since moved.
* `routine_description_draft` -- the drafted proposal.
  `uq_routine_description_draft_open` is a partial unique index: one open draft
  (DRAFT or PENDING_APPROVAL) per routine, enforced by the database rather than
  by a read-then-insert two concurrent generation requests could both pass. It
  is declared identically here and in `__table_args__`, because
  `tests/test_migration_orm_drift.py` runs `compare_metadata` and a
  predicate spelled differently on the two sides reads as drift.

The withdrawal subject-type check widens to admit `ROUTINE`, following
`d8a3f1c6b204`'s pattern for `ANNOTATION` exactly: a named constant, a
`drop_constraint(type_="check")` then a `create_check_constraint`. `downgrade`
restores the narrower form, and -- as in that revision -- the constraint cannot
be created while any routine withdrawal exists, so the downgrade *fails* rather
than deleting a governed record to make room for an older schema.

`DescriptionWithdrawal` and `GovernanceReview.object_type` are the only two
shared tables in the description family that genuinely discriminate on a subject
kind, and only the first is a constraint. Deliberately **not** widened, each
recorded as a decision in `models.py`: `document_claim.subject_type` and
`document_mapping.subject_type` (a data dictionary describes tables and
columns), `model_import_change.subject_type` (the model workbook has no routine
sheet), and `metadata_object_description.object_type` (a routine already owns
its source comment directly).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d24e1c8f03"
down_revision: str | Sequence[str] | None = "c9f3b6a1d472"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN = "status IN ('DRAFT', 'PENDING_APPROVAL')"
_SUBJECT_TYPE_CHECK = "ck_description_withdrawal_withdrawal_subject_type_is_supported"


def upgrade() -> None:
    op.create_table(
        "routine_documentation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_routine_documentation_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_routine_documentation_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f("fk_routine_documentation_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routine_documentation")),
        sa.UniqueConstraint("routine_id", name="uq_routine_documentation_routine_id"),
    )
    op.create_index(
        op.f("ix_routine_documentation_datasource_id"),
        "routine_documentation",
        ["datasource_id"],
    )
    op.create_index(
        op.f("ix_routine_documentation_organization_id"),
        "routine_documentation",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_routine_documentation_routine_id"), "routine_documentation", ["routine_id"]
    )

    op.create_table(
        "routine_documentation_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("documentation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_definition_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["documentation_id"],
            ["routine_documentation.id"],
            name=op.f(
                "fk_routine_documentation_version_documentation_id_routine_documentation"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_routine_documentation_version_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        # SET NULL, not CASCADE: losing the provenance edge to the captured
        # definition must never delete a governed description.
        sa.ForeignKeyConstraint(
            ["source_definition_version_id"],
            ["metadata_routine_definition_version.id"],
            name=op.f(
                "fk_routine_documentation_version_source_definition_version_id_"
                "metadata_routine_definition_version"
            ),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routine_documentation_version")),
        sa.UniqueConstraint(
            "documentation_id",
            "version",
            name=op.f("uq_routine_documentation_version_documentation_id"),
        ),
    )
    op.create_index(
        op.f("ix_routine_documentation_version_documentation_id"),
        "routine_documentation_version",
        ["documentation_id"],
    )
    op.create_index(
        op.f("ix_routine_documentation_version_organization_id"),
        "routine_documentation_version",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_routine_documentation_version_source_definition_version_id"),
        "routine_documentation_version",
        ["source_definition_version_id"],
    )
    op.create_index(
        "ix_routine_documentation_version_org_status",
        "routine_documentation_version",
        ["organization_id", "status"],
    )

    op.create_table(
        "routine_description_draft",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
        sa.Column("drafted_text", sa.Text(), nullable=False),
        sa.Column("text_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("accuracy_score", sa.Float(), nullable=False),
        sa.Column("clarity_score", sa.Float(), nullable=False),
        sa.Column("style_score", sa.Float(), nullable=False),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("base_description_version", sa.Integer(), nullable=True),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("published_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_routine_description_draft_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_routine_description_draft_governance_review_id_governance_review"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_routine_description_draft_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_version_id"],
            ["routine_documentation_version.id"],
            name=op.f(
                "fk_routine_description_draft_published_version_id_"
                "routine_documentation_version"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f("fk_routine_description_draft_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routine_description_draft")),
        sa.UniqueConstraint(
            "governance_review_id",
            name=op.f("uq_routine_description_draft_governance_review_id"),
        ),
    )
    op.create_index(
        op.f("ix_routine_description_draft_datasource_id"),
        "routine_description_draft",
        ["datasource_id"],
    )
    op.create_index(
        op.f("ix_routine_description_draft_organization_id"),
        "routine_description_draft",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_routine_description_draft_published_version_id"),
        "routine_description_draft",
        ["published_version_id"],
    )
    op.create_index(
        op.f("ix_routine_description_draft_routine_id"),
        "routine_description_draft",
        ["routine_id"],
    )
    op.create_index(
        "ix_routine_description_draft_org_status",
        "routine_description_draft",
        ["organization_id", "status"],
    )
    op.create_index(
        "uq_routine_description_draft_open",
        "routine_description_draft",
        ["routine_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN),
        sqlite_where=sa.text(_OPEN),
    )

    # R11-FP08: a routine description is withdrawn and reinstated through the
    # same governed request every other description uses.
    op.drop_constraint(op.f(_SUBJECT_TYPE_CHECK), "description_withdrawal", type_="check")
    op.create_check_constraint(
        op.f(_SUBJECT_TYPE_CHECK),
        "description_withdrawal",
        "subject_type IN ('TABLE', 'COLUMN', 'ANNOTATION', 'ROUTINE')",
    )


def downgrade() -> None:
    # Restores the narrower check. This deliberately *fails* while any routine
    # withdrawal exists, rather than deleting a governed record to make room
    # for an older schema -- `d8a3f1c6b204`'s own rule for `ANNOTATION`.
    op.drop_constraint(op.f(_SUBJECT_TYPE_CHECK), "description_withdrawal", type_="check")
    op.create_check_constraint(
        op.f(_SUBJECT_TYPE_CHECK),
        "description_withdrawal",
        "subject_type IN ('TABLE', 'COLUMN', 'ANNOTATION')",
    )

    op.drop_index("uq_routine_description_draft_open", table_name="routine_description_draft")
    op.drop_index(
        "ix_routine_description_draft_org_status", table_name="routine_description_draft"
    )
    op.drop_index(
        op.f("ix_routine_description_draft_routine_id"),
        table_name="routine_description_draft",
    )
    op.drop_index(
        op.f("ix_routine_description_draft_published_version_id"),
        table_name="routine_description_draft",
    )
    op.drop_index(
        op.f("ix_routine_description_draft_organization_id"),
        table_name="routine_description_draft",
    )
    op.drop_index(
        op.f("ix_routine_description_draft_datasource_id"),
        table_name="routine_description_draft",
    )
    op.drop_table("routine_description_draft")

    op.drop_index(
        "ix_routine_documentation_version_org_status",
        table_name="routine_documentation_version",
    )
    op.drop_index(
        op.f("ix_routine_documentation_version_source_definition_version_id"),
        table_name="routine_documentation_version",
    )
    op.drop_index(
        op.f("ix_routine_documentation_version_organization_id"),
        table_name="routine_documentation_version",
    )
    op.drop_index(
        op.f("ix_routine_documentation_version_documentation_id"),
        table_name="routine_documentation_version",
    )
    op.drop_table("routine_documentation_version")

    op.drop_index(
        op.f("ix_routine_documentation_routine_id"), table_name="routine_documentation"
    )
    op.drop_index(
        op.f("ix_routine_documentation_organization_id"), table_name="routine_documentation"
    )
    op.drop_index(
        op.f("ix_routine_documentation_datasource_id"), table_name="routine_documentation"
    )
    op.drop_table("routine_documentation")
