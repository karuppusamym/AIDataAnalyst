"""Embedding store in plain PostgreSQL -- no extension required (ADR-0019)

The hybrid-retrieval design named `pgvector`. That assumption does not survive contact
with a regulated estate: `CREATE EXTENSION` needs a privilege a bank DBA will not grant
a new platform, and several will not install `pgvector` at all.

So embeddings live in an ordinary `bytea` column, and nearest-neighbour search is a port
with four adapters (`aida/vector_store.py`): exact cosine in PostgreSQL (default), the
bank's own in-network vector service over HTTP, `pgvector` where it genuinely exists, and
disabled. Nothing here requires an extension, and the schema does not change if the
backend later does -- the table remains the authoritative copy from which any external
index is rebuilt (INV-1).

Revision ID: b4e2f70a9c15
Revises: a7c3e91d4f28
Create Date: 2026-08-30 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4e2f70a9c15"
down_revision: str | None = "a7c3e91d4f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embedding",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("owner_type", sa.String(40), nullable=False),
        sa.Column("owner_id", sa.String(120), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("index_signature", sa.String(400), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        # bytea, not vector(n): packed float32s, portable to any PostgreSQL.
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        # Stored at write time so the scorer's inner loop does half the work.
        sa.Column("vector_norm", sa.Float(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "owner_type", "owner_id", "chunk_index"),
    )
    op.create_index("ix_embedding_organization_id", "embedding", ["organization_id"])
    op.create_index(
        "ix_embedding_owner", "embedding", ["organization_id", "owner_type", "owner_id"]
    )
    op.create_index("ix_embedding_signature", "embedding", ["organization_id", "index_signature"])


def downgrade() -> None:
    op.drop_table("embedding")
