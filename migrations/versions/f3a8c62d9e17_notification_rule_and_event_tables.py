"""notification rule and event tables (DQ-1 migration gap)

DQ-1 ("Notification and escalation routing") shipped `notification_rule`
and `notification_event` as ORM models (`aida.models.NotificationRuleRecord`
/ `NotificationEventRecord`) with no migration creating either table --
flagged in `54ad108f9b8a_unowned_asset_escalation.py`'s docstring, which
also stored `unowned_asset_escalation.notification_rule_id` as a soft
(non-FK) UUID column for the same reason.

This migration adds the two missing tables and promotes that soft
reference to a real foreign key, matching the `ForeignKey(...,
ondelete="SET NULL")` already declared on
`UnownedAssetEscalation.notification_rule_id` in the ORM.

Revision ID: f3a8c62d9e17
Revises: 90f077415a93
Create Date: 2026-08-30 14:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a8c62d9e17"
down_revision: str | Sequence[str] | None = "90f077415a93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("escalation_after_minutes", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_notification_rule_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_rule")),
    )
    op.create_index(
        "ix_notification_rule_org_enabled",
        "notification_rule",
        ["organization_id", "enabled"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_rule_organization_id"),
        "notification_rule",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "notification_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_notification_event_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["data_quality_incident.id"],
            name=op.f("fk_notification_event_incident_id_data_quality_incident"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["notification_rule.id"],
            name=op.f("fk_notification_event_rule_id_notification_rule"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_event")),
    )
    op.create_index(
        "ix_notification_event_org_status",
        "notification_event",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_notification_event_incident",
        "notification_event",
        ["incident_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_event_organization_id"),
        "notification_event",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_event_rule_id"),
        "notification_event",
        ["rule_id"],
        unique=False,
    )

    op.create_foreign_key(
        op.f("fk_unowned_asset_escalation_notification_rule_id_notification_rule"),
        "unowned_asset_escalation",
        "notification_rule",
        ["notification_rule_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_unowned_asset_escalation_notification_rule_id_notification_rule"),
        "unowned_asset_escalation",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_notification_event_rule_id"), table_name="notification_event")
    op.drop_index(op.f("ix_notification_event_organization_id"), table_name="notification_event")
    op.drop_index("ix_notification_event_incident", table_name="notification_event")
    op.drop_index("ix_notification_event_org_status", table_name="notification_event")
    op.drop_table("notification_event")
    op.drop_index(op.f("ix_notification_rule_organization_id"), table_name="notification_rule")
    op.drop_index("ix_notification_rule_org_enabled", table_name="notification_rule")
    op.drop_table("notification_rule")
