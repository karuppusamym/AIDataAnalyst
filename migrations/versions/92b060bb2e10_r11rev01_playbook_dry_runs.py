"""R11-REV01: stored playbook dry-runs, and the run each one binds

Revision ID: 92b060bb2e10
Revises: f7c2d9a4b61e
Create Date: 2026-09-19

One table, declared in `aida.review_batch_models.PlaybookDryRunRecord`. A stored dry-run is
the version of a playbook's preview -- the rule version, which subjects it matched and the
evidence version of each -- that a later run can be bound to: the run re-evaluates, compares
itself to this record, and by default refuses to act on anything the steward did not see.
Once a run is bound, the record names it.

**Value-free (INV-6).** Ids, codes, counts and SHA-256 digests. `subject_versions` holds
`[subject id, evidence version]` pairs only, never the values a preview displays.

**Tenancy (INV-5).** `organization_id` with RESTRICT. `playbook_id` CASCADEs: a preview of a
deleted rule binds nothing.

No backfill: the table starts empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "92b060bb2e10"
down_revision: str | Sequence[str] | None = "f7c2d9a4b61e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "playbook_dry_run"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("playbook_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("match_digest", sa.String(length=64), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("tables_truncated", sa.Boolean(), nullable=False),
        sa.Column("columns_truncated", sa.Boolean(), nullable=False),
        sa.Column("auto_apply_max_items", sa.Integer(), nullable=False),
        sa.Column("predicted_disposition", sa.String(length=20), nullable=False),
        sa.Column("change_counts", sa.JSON(), nullable=False),
        sa.Column("subject_versions", sa.JSON(), nullable=False),
        sa.Column("evaluated_by", sa.String(length=255), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bound_by", sa.String(length=255), nullable=True),
        sa.Column("bound_binding_status", sa.String(length=20), nullable=True),
        sa.Column("bound_run_outcome", sa.String(length=30), nullable=True),
        sa.Column("bound_bulk_action_run_id", sa.Uuid(), nullable=True),
        sa.Column("bound_bulk_stewardship_operation_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "predicted_disposition IN ('NO_MATCHES', 'AUTOMATIC', 'HUMAN_REVIEW')",
            name=op.f(f"ck_{_TABLE}_predicted_disposition"),
        ),
        sa.CheckConstraint(
            "bound_run_outcome IS NULL OR bound_run_outcome IN "
            "('NO_MATCHES', 'AUTO_APPLIED', 'QUEUED_FOR_REVIEW')",
            name=op.f(f"ck_{_TABLE}_bound_run_outcome"),
        ),
        sa.CheckConstraint(
            "bound_binding_status IS NULL OR bound_binding_status IN ('MATCHES', 'DIFFERS')",
            name=op.f(f"ck_{_TABLE}_bound_binding_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_TABLE}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["playbook_id"],
            ["metadata_playbook.id"],
            name=op.f(f"fk_{_TABLE}_playbook_id_metadata_playbook"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(
        op.f(f"ix_{_TABLE}_organization_id"), _TABLE, ["organization_id"], unique=False
    )
    op.create_index(
        "ix_playbook_dry_run_org_playbook",
        _TABLE,
        ["organization_id", "playbook_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_playbook_dry_run_org_playbook", table_name=_TABLE)
    op.drop_index(op.f(f"ix_{_TABLE}_organization_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
