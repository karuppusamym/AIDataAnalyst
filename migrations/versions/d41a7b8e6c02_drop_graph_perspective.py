"""drop graph_perspective

Revision ID: d41a7b8e6c02
Revises: c93a5f1d47e8
Create Date: 2026-09-11 23:40:00.000000

Review 2026-09-11, row R11-X5. `graph_perspective` (KG-5) held a saved Graph
Explorer view state. Its five routes in `aida.graph_perspectives_api` were the
table's only reader and only writer, and they had no caller: no service module
ever imported the router's handlers, `ui-next` has no Graph Explorer screen to
save a perspective from, and `Docs/20-modules/10-knowledge-graph.md` listed
saved perspectives as "Not implemented". The router, the ORM model, the three
API schemas and the test module were removed in the same change.

Nothing outside `graph_perspectives_api` has ever written this table, so a
deployment can only hold rows in it if a caller reached those routes directly.
`downgrade` recreates the table and its four indexes, not their contents.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d41a7b8e6c02"
down_revision: str | Sequence[str] | None = "c93a5f1d47e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_graph_perspective_org_owner", table_name="graph_perspective")
    op.drop_index("ix_graph_perspective_owner_principal", table_name="graph_perspective")
    op.drop_index("ix_graph_perspective_datasource_id", table_name="graph_perspective")
    op.drop_index("ix_graph_perspective_organization_id", table_name="graph_perspective")
    op.drop_table("graph_perspective")


def downgrade() -> None:
    op.create_table(
        "graph_perspective",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("datasource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("owner_principal", sa.String(length=255), nullable=False),
        sa.Column("allowed_viewer_roles", sa.JSON(), nullable=False),
        sa.Column("view_state", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_graph_perspective_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_graph_perspective_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_perspective")),
    )
    op.create_index(
        "ix_graph_perspective_organization_id",
        "graph_perspective",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_graph_perspective_datasource_id",
        "graph_perspective",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        "ix_graph_perspective_owner_principal",
        "graph_perspective",
        ["owner_principal"],
        unique=False,
    )
    op.create_index(
        "ix_graph_perspective_org_owner",
        "graph_perspective",
        ["organization_id", "owner_principal"],
        unique=False,
    )
