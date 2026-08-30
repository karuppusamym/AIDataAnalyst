"""data domain (ADR-0017 phase 1)

Adds the data_domain governance level between line_of_business and project,
completing ADR-0005's tenancy hierarchy at the level ADR-0017 scopes cross-
project/cross-source graph traversal and relationship inference to. Every
existing line_of_business gets one lazily-materialized default ("Ungoverned")
domain, and every existing project/datasource is backfilled onto it, so
nothing is left unscoped by this migration (ADR-0017 SS1, SS10).

Revision ID: d3f7a5c8e1b4
Revises: c8a4d3e91f02
Create Date: 2026-08-29 21:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f7a5c8e1b4"
down_revision: str | None = "c8a4d3e91f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_domain",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("line_of_business_id", sa.Uuid(), nullable=False),
        sa.Column("parent_domain_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_data_domain_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["line_of_business_id"],
            ["line_of_business.id"],
            name=op.f("fk_data_domain_line_of_business_id_line_of_business"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_domain_id"],
            ["data_domain.id"],
            name=op.f("fk_data_domain_parent_domain_id_data_domain"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_domain")),
        sa.UniqueConstraint(
            "line_of_business_id", "code", name=op.f("uq_data_domain_line_of_business_id")
        ),
    )
    op.create_index(
        op.f("ix_data_domain_organization_id"), "data_domain", ["organization_id"], unique=False
    )
    op.create_index(
        op.f("ix_data_domain_line_of_business_id"),
        "data_domain",
        ["line_of_business_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_data_domain_parent_domain_id"), "data_domain", ["parent_domain_id"], unique=False
    )

    # Columns start nullable so existing rows can be backfilled before the NOT NULL
    # constraint lands — a bare add-and-require-in-one-step would fail on any
    # non-empty deployment.
    op.add_column("project", sa.Column("data_domain_id", sa.Uuid(), nullable=True))
    op.add_column("datasource", sa.Column("data_domain_id", sa.Uuid(), nullable=True))

    # One default "Ungoverned" domain per existing line_of_business — the same
    # lazy-creation this migration performs eagerly for pre-existing rows is also
    # done on demand for LOBs created after this migration (domain_service.py).
    op.execute(
        sa.text(
            """
            INSERT INTO data_domain
                (id, organization_id, line_of_business_id, name, code, is_default,
                 status, created_at, updated_at)
            SELECT gen_random_uuid(), lob.organization_id, lob.id, 'Ungoverned',
                   'UNGOVERNED', true, 'ACTIVE', now(), now()
            FROM line_of_business AS lob
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE project AS p
            SET data_domain_id = dd.id
            FROM data_domain AS dd
            WHERE dd.line_of_business_id = p.line_of_business_id
              AND dd.is_default = true
              AND p.data_domain_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE datasource AS d
            SET data_domain_id = dd.id
            FROM data_domain AS dd
            WHERE dd.line_of_business_id = d.line_of_business_id
              AND dd.is_default = true
              AND d.data_domain_id IS NULL
            """
        )
    )

    op.alter_column("project", "data_domain_id", nullable=False)
    op.alter_column("datasource", "data_domain_id", nullable=False)

    op.create_foreign_key(
        op.f("fk_project_data_domain_id_data_domain"),
        "project",
        "data_domain",
        ["data_domain_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_datasource_data_domain_id_data_domain"),
        "datasource",
        "data_domain",
        ["data_domain_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_project_data_domain_id"), "project", ["data_domain_id"], unique=False
    )
    op.create_index(
        op.f("ix_datasource_data_domain_id"), "datasource", ["data_domain_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_datasource_data_domain_id"), table_name="datasource")
    op.drop_index(op.f("ix_project_data_domain_id"), table_name="project")
    op.drop_constraint(
        op.f("fk_datasource_data_domain_id_data_domain"), "datasource", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_project_data_domain_id_data_domain"), "project", type_="foreignkey"
    )
    op.drop_column("datasource", "data_domain_id")
    op.drop_column("project", "data_domain_id")

    op.drop_index(op.f("ix_data_domain_parent_domain_id"), table_name="data_domain")
    op.drop_index(op.f("ix_data_domain_line_of_business_id"), table_name="data_domain")
    op.drop_index(op.f("ix_data_domain_organization_id"), table_name="data_domain")
    op.drop_table("data_domain")
