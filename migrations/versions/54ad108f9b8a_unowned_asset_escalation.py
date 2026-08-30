"""unowned asset escalation (GL-6)

Adds `unowned_asset_escalation`, tracking each table's unowned-asset backlog
entry (GL-4/GL-6) through routing and escalation. Routing/escalation itself
reuses DQ-1's generic `aida.notification_routing` engine and the org-scoped
`notification_rule` table it already routes quality incidents through --
this table records the outcome (candidate owner, matched rule, dedup key,
delivery status) since `notification_event` is FK-scoped to
`data_quality_incident` and cannot carry a table subject.

`notification_rule_id` is stored as a plain UUID column, not a database
foreign key: DQ-1 shipped `notification_rule`/`notification_event` as ORM
models (`aida.models.NotificationRuleRecord` / `NotificationEventRecord`)
without a migration creating either table, so this migration cannot depend
on `notification_rule` existing in the migration chain yet. The ORM
relationship (`ForeignKey("notification_rule.id", ondelete="SET NULL")`)
is still declared on the model for `Base.metadata.create_all()`-based
tests and for documentation; a later migration should add the missing
`notification_rule`/`notification_event` tables and can promote this
column to a real FK constraint at that point.

Revision ID: 54ad108f9b8a
Revises: d5f8b21c4a03
Create Date: 2026-08-30 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "54ad108f9b8a"
down_revision: str | Sequence[str] | None = "d5f8b21c4a03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unowned_asset_escalation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("first_detected_unowned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("candidate_owner", sa.String(length=255), nullable=True),
        # Soft reference to notification_rule.id -- see module docstring above.
        sa.Column("notification_rule_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(length=30), nullable=True),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(length=64), nullable=True),
        sa.Column("routed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_unowned_asset_escalation_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_unowned_asset_escalation_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unowned_asset_escalation")),
        sa.UniqueConstraint("table_id", name=op.f("uq_unowned_asset_escalation_table_id")),
    )
    op.create_index(
        "ix_unowned_asset_escalation_org_status",
        "unowned_asset_escalation",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_unowned_asset_escalation_organization_id"),
        "unowned_asset_escalation",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_unowned_asset_escalation_table_id"),
        "unowned_asset_escalation",
        ["table_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_unowned_asset_escalation_notification_rule_id"),
        "unowned_asset_escalation",
        ["notification_rule_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_unowned_asset_escalation_notification_rule_id"),
        table_name="unowned_asset_escalation",
    )
    op.drop_index(
        op.f("ix_unowned_asset_escalation_table_id"), table_name="unowned_asset_escalation"
    )
    op.drop_index(
        op.f("ix_unowned_asset_escalation_organization_id"),
        table_name="unowned_asset_escalation",
    )
    op.drop_index(
        "ix_unowned_asset_escalation_org_status", table_name="unowned_asset_escalation"
    )
    op.drop_table("unowned_asset_escalation")
