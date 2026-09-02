"""DQ-8: external quality signals + incident source discriminator

Adds the ``external_quality_signal`` table (immutable, value-free normalized
inbound envelope for a third-party detector quality signal -- Monte Carlo,
Anomalo, ...) and the ``data_quality_incident.source`` discriminator that keeps
externally-sourced incidents distinguishable from internally-computed ones.

``source`` is backfilled to ``'INTERNAL'`` for every existing incident (all
current incidents come from Atlas's own detectors) and then made NOT NULL, so the
final shape matches the ORM's ``default="INTERNAL"`` Python-side default with no
server default -- matching the rest of this codebase's columns.

Revision ID: a1c9e7d4b2f0
Revises: eb8987ff4f66
Create Date: 2026-09-01 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c9e7d4b2f0"
down_revision: str | Sequence[str] | None = "eb8987ff4f66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- incident source discriminator (nullable -> backfill -> NOT NULL) ---
    op.add_column(
        "data_quality_incident",
        sa.Column("source", sa.String(length=30), nullable=True),
    )
    op.execute("UPDATE data_quality_incident SET source = 'INTERNAL' WHERE source IS NULL")
    op.alter_column("data_quality_incident", "source", nullable=False)

    # --- external_quality_signal ---
    op.create_table(
        "external_quality_signal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=True),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("detector_vendor", sa.String(length=50), nullable=False),
        sa.Column("detector_native_id", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column("signal_status", sa.String(length=30), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_external_quality_signal_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_external_quality_signal_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_external_quality_signal_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["column_id"],
            ["metadata_column.id"],
            name=op.f("fk_external_quality_signal_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["data_quality_incident.id"],
            name=op.f("fk_external_quality_signal_incident_id_data_quality_incident"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_quality_signal")),
        sa.UniqueConstraint(
            "organization_id",
            "detector_vendor",
            "detector_native_id",
            "observed_at",
            name="uq_external_quality_signal_dedup",
        ),
    )
    op.create_index(
        "ix_external_quality_signal_source_created",
        "external_quality_signal",
        ["datasource_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_external_quality_signal_org_vendor",
        "external_quality_signal",
        ["organization_id", "detector_vendor"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_quality_signal_organization_id"),
        "external_quality_signal",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_quality_signal_datasource_id"),
        "external_quality_signal",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_quality_signal_table_id"),
        "external_quality_signal",
        ["table_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_quality_signal_column_id"),
        "external_quality_signal",
        ["column_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_quality_signal_incident_id"),
        "external_quality_signal",
        ["incident_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_external_quality_signal_incident_id"), table_name="external_quality_signal"
    )
    op.drop_index(
        op.f("ix_external_quality_signal_column_id"), table_name="external_quality_signal"
    )
    op.drop_index(
        op.f("ix_external_quality_signal_table_id"), table_name="external_quality_signal"
    )
    op.drop_index(
        op.f("ix_external_quality_signal_datasource_id"), table_name="external_quality_signal"
    )
    op.drop_index(
        op.f("ix_external_quality_signal_organization_id"), table_name="external_quality_signal"
    )
    op.drop_index(
        "ix_external_quality_signal_org_vendor", table_name="external_quality_signal"
    )
    op.drop_index(
        "ix_external_quality_signal_source_created", table_name="external_quality_signal"
    )
    op.drop_table("external_quality_signal")
    op.drop_column("data_quality_incident", "source")
