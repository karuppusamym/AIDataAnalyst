"""enterprise metadata ingestion and connector certification

Revision ID: 7d2f9a41c6e3
Revises: 1b7e4c9a62d0
Create Date: 2026-08-27 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2f9a41c6e3"
down_revision: str | Sequence[str] | None = "1b7e4c9a62d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_ingestion_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid()),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("envelope_version", sa.String(20), nullable=False),
        sa.Column("producer", sa.String(200), nullable=False),
        sa.Column("transport", sa.String(20), nullable=False),
        sa.Column("snapshot_type", sa.String(20), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("object_counts", sa.JSON(), nullable=False),
        sa.Column("change_counts", sa.JSON(), nullable=False),
        sa.Column("submitted_by", sa.String(255), nullable=False),
        sa.Column("error_class", sa.String(100)),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("datasource_id", "idempotency_key"),
    )
    for column in ("organization_id", "datasource_id", "analysis_run_id"):
        op.create_index(
            op.f(f"ix_metadata_ingestion_job_{column}"), "metadata_ingestion_job", [column]
        )
    op.create_index(
        "ix_metadata_ingestion_org_status",
        "metadata_ingestion_job",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_metadata_ingestion_source_created",
        "metadata_ingestion_job",
        ["datasource_id", "created_at"],
    )

    op.create_table(
        "connector_certification_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("connector_type", sa.String(50), nullable=False),
        sa.Column("connector_version", sa.String(50), nullable=False),
        sa.Column("suite_version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("initiated_by", sa.String(255), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "datasource_id"):
        op.create_index(
            op.f(f"ix_connector_certification_run_{column}"),
            "connector_certification_run",
            [column],
        )
    op.create_index(
        "ix_connector_cert_source_created",
        "connector_certification_run",
        ["datasource_id", "created_at"],
    )
    op.create_index(
        "ix_connector_cert_org_status",
        "connector_certification_run",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("connector_certification_run")
    op.drop_table("metadata_ingestion_job")
