"""graph store organization setting (C7 / ADR-0020 amendment, Group J)

Revision ID: 8396592b30e0
Revises: 7e6460d905fe
Create Date: 2026-09-02 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8396592b30e0"
down_revision: str | Sequence[str] | None = "7e6460d905fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_store_organization_setting",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("backend", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_graph_store_organization_setting_organization_id_organization"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_store_organization_setting")),
        sa.UniqueConstraint(
            "organization_id",
            name=op.f("uq_graph_store_organization_setting_organization_id"),
        ),
        sa.CheckConstraint(
            "backend IN ('postgres', 'neo4j', 'disabled')",
            name="ck_graph_store_organization_setting_backend",
        ),
    )
    op.create_index(
        op.f("ix_graph_store_organization_setting_organization_id"),
        "graph_store_organization_setting",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_graph_store_organization_setting_organization_id"),
        table_name="graph_store_organization_setting",
    )
    op.drop_table("graph_store_organization_setting")
