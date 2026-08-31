"""access review report (OB-7)

Adds `access_review_report`, the WORM-archived self-service entitlement
report table backing `aida.access_review` / `aida.access_review_api`
(module 20, `Docs/20-modules/20-observability-and-audit.md` §7's "Access
review" pack -- this is the per-principal counterpart to that org-wide
compliance pack). Append-only: no update or delete path exists anywhere in
`aida.access_review_api`.

Revision ID: da74c4fd9def
Revises: bb909675ad3c
Create Date: 2026-08-31 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "da74c4fd9def"
down_revision: str | Sequence[str] | None = "bb909675ad3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_review_report",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("subject_principal_id", sa.String(length=255), nullable=False),
        sa.Column("subject_principal_type", sa.String(length=30), nullable=False),
        sa.Column("is_self_service", sa.Boolean(), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("entitlements", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_access_review_report_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_review_report")),
    )
    op.create_index(
        op.f("ix_access_review_report_organization_id"),
        "access_review_report",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_access_review_report_org_subject",
        "access_review_report",
        ["organization_id", "subject_principal_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_access_review_report_org_subject", table_name="access_review_report"
    )
    op.drop_index(
        op.f("ix_access_review_report_organization_id"),
        table_name="access_review_report",
    )
    op.drop_table("access_review_report")
