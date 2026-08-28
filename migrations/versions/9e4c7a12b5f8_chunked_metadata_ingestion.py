"""durable chunked metadata ingestion

Revision ID: 9e4c7a12b5f8
Revises: 7d2f9a41c6e3
Create Date: 2026-08-27 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e4c7a12b5f8"
down_revision: str | Sequence[str] | None = "7d2f9a41c6e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_ingestion_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid()),
        sa.Column("batch_key", sa.String(200), nullable=False),
        sa.Column("envelope_version", sa.String(20), nullable=False),
        sa.Column("producer", sa.String(200), nullable=False),
        sa.Column("snapshot_type", sa.String(20), nullable=False),
        sa.Column("expected_chunks", sa.Integer(), nullable=False),
        sa.Column("received_chunks", sa.Integer(), nullable=False),
        sa.Column("processed_chunks", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(255)),
        sa.Column("object_counts", sa.JSON(), nullable=False),
        sa.Column("change_counts", sa.JSON(), nullable=False),
        sa.Column("submitted_by", sa.String(255), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_class", sa.String(100)),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("datasource_id", "batch_key"),
        sa.UniqueConstraint("temporal_workflow_id"),
    )
    for column in ("organization_id", "datasource_id", "analysis_run_id"):
        op.create_index(
            op.f(f"ix_metadata_ingestion_batch_{column}"),
            "metadata_ingestion_batch",
            [column],
        )
    op.create_index(
        "ix_ingestion_batch_source_created",
        "metadata_ingestion_batch",
        ["datasource_id", "created_at"],
    )
    op.create_index(
        "ix_ingestion_batch_org_status",
        "metadata_ingestion_batch",
        ["organization_id", "status"],
    )

    op.create_table(
        "metadata_ingestion_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_number", sa.Integer(), nullable=False),
        sa.Column("chunk_key", sa.String(200), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("object_counts", sa.JSON(), nullable=False),
        sa.Column("change_counts", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["metadata_ingestion_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id", "chunk_number", name="uq_ingestion_chunk_batch_number"
        ),
        sa.UniqueConstraint("batch_id", "chunk_key", name="uq_ingestion_chunk_batch_key"),
    )
    for column in ("organization_id", "datasource_id", "batch_id"):
        op.create_index(
            op.f(f"ix_metadata_ingestion_chunk_{column}"),
            "metadata_ingestion_chunk",
            [column],
        )
    op.create_index(
        "ix_ingestion_chunk_batch_status",
        "metadata_ingestion_chunk",
        ["batch_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("metadata_ingestion_chunk")
    op.drop_table("metadata_ingestion_batch")
