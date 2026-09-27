"""drop search_index, vector_embedding, abac_policy and abac_decision

Revision ID: c93a5f1d47e8
Revises: b7c41e9d52a0
Create Date: 2026-09-11 22:10:00.000000

Review 2026-09-11, row R11-X2. Four tables with no reader and no writer:

* `search_index` and `vector_embedding` were only ever created, by the AU-8
  drift-reconciliation revision. Full-text search runs through
  `aida.full_text_index` and embeddings through the `embedding` table
  (`aida.vector_store`), which carries the packed vector, its norm and the
  index signature that makes two vectors comparable -- none of which
  `vector_embedding` has. It is not the future home of the planned document
  embeddings either: its unique key has no chunk dimension, so it cannot hold
  a chunked document at all.
* `abac_policy` and `abac_decision` outlived their module. The `abac.py` /
  `abac_api.py` pair that read and wrote them was deleted earlier; policy
  evaluation now lives in `aida.policy_engine` and its decisions are recorded
  as audit events.

Nothing in the codebase has ever written any of the four, so a deployment can
only hold rows in them if something outside this repository did. `downgrade`
recreates the tables and their indexes, not their contents.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c93a5f1d47e8"
down_revision: str | Sequence[str] | None = "b7c41e9d52a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_abac_decision_principal", table_name="abac_decision")
    op.drop_index("ix_abac_decision_org_created", table_name="abac_decision")
    op.drop_table("abac_decision")

    op.drop_index("ix_abac_policy_org_status", table_name="abac_policy")
    op.drop_table("abac_policy")

    op.drop_index("ix_vector_embedding_org_type", table_name="vector_embedding")
    op.drop_table("vector_embedding")

    op.drop_index("ix_search_index_org_status", table_name="search_index")
    op.drop_table("search_index")


def downgrade() -> None:
    op.create_table(
        "search_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("index_key", sa.String(length=100), nullable=False),
        sa.Column("index_type", sa.String(length=30), nullable=False, server_default="GIN"),
        sa.Column("source_table", sa.String(length=100), nullable=False),
        sa.Column("text_columns", sa.JSON(), nullable=False),
        sa.Column("language", sa.String(length=30), nullable=False, server_default="english"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column("last_rebuilt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("organization_id", "index_key"),
    )
    op.create_index("ix_search_index_org_status", "search_index", ["organization_id", "status"])

    op.create_table(
        "vector_embedding",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "datasource_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datasource.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("object_type", sa.String(length=50), nullable=False),
        sa.Column("object_id", sa.String(length=100), nullable=False, index=True),
        sa.Column("display_name", sa.String(length=500), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("organization_id", "object_type", "object_id"),
    )
    op.create_index(
        "ix_vector_embedding_org_type", "vector_embedding", ["organization_id", "object_type"]
    )

    op.create_table(
        "abac_policy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("policy_key", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("effect", sa.String(length=10), nullable=False),
        sa.Column("subject_conditions", sa.JSON(), nullable=False),
        sa.Column("resource_conditions", sa.JSON(), nullable=False),
        sa.Column("environment_conditions", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("organization_id", "policy_key", "version"),
    )
    op.create_index("ix_abac_policy_org_status", "abac_policy", ["organization_id", "status"])

    op.create_table(
        "abac_decision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("principal_type", sa.String(length=30), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("subject_attributes", sa.JSON(), nullable=False),
        sa.Column("resource_attributes", sa.JSON(), nullable=False),
        sa.Column("environment_attributes", sa.JSON(), nullable=False),
        sa.Column("contributing_policy_ids", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("evaluation_time_ms", sa.Float(), nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("correlation_id", sa.String(length=100), nullable=False, index=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_abac_decision_org_created", "abac_decision", ["organization_id", "evaluated_at"]
    )
    op.create_index(
        "ix_abac_decision_principal", "abac_decision", ["principal_id", "evaluated_at"]
    )
