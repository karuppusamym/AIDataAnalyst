"""metadata ingestion envelope 1.1 axes

Adds persistence for the four axes envelope 1.1 introduces (gap/02 row N1):
view definitions, routines with their parameters, source-side object
descriptions, and source-side grants. The tables are declared in
`aida.envelope_models` rather than `aida.models`; they register on the same
`Base.metadata`, so this revision is the schema for that module.

Nothing in this revision touches an existing table, so it is reversible without
data loss: `downgrade()` drops only what `upgrade()` created.

Revision ID: a1c9f4b7e230
Revises: c9d1a83e6b47
Create Date: 2026-08-30 12:00:00

Chained onto `b4e2f70a9c15` when written -- the single head at that moment -- and
re-pointed onto `c9d1a83e6b47` when a concurrent workstream added that revision on
the same parent and produced two heads. This revision depends only on the 1.0
metadata tables, which long predate both, so its parent may be re-pointed again
without any change to its body.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c9f4b7e230"
down_revision: str | Sequence[str] | None = "c9d1a83e6b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_view_definition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("definition_sql", sa.Text(), nullable=True),
        sa.Column("is_materialized", sa.Boolean(), nullable=False),
        sa.Column("is_updatable", sa.Boolean(), nullable=True),
        sa.Column("check_option", sa.String(length=30), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("unavailable_reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "availability IN ('AVAILABLE', 'UNAVAILABLE')",
            name=op.f("ck_metadata_view_definition_availability_state"),
        ),
        sa.CheckConstraint(
            "(availability = 'AVAILABLE') = (definition_sql IS NOT NULL)",
            name=op.f("ck_metadata_view_definition_availability_matches_definition"),
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_view_definition_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_view_definition_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_metadata_view_definition_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_view_definition")),
        sa.UniqueConstraint("table_id", name=op.f("uq_metadata_view_definition_table_id")),
    )
    for column in ("organization_id", "datasource_id", "table_id"):
        op.create_index(
            op.f(f"ix_metadata_view_definition_{column}"),
            "metadata_view_definition",
            [column],
        )
    op.create_index(
        "ix_metadata_view_definition_org_status",
        "metadata_view_definition",
        ["organization_id", "status"],
    )

    op.create_table(
        "metadata_routine",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("signature", sa.String(length=1000), nullable=False),
        sa.Column("routine_type", sa.String(length=30), nullable=False),
        sa.Column("language", sa.String(length=50), nullable=True),
        sa.Column("body_sql", sa.Text(), nullable=True),
        sa.Column("return_type", sa.String(length=255), nullable=True),
        sa.Column("is_deterministic", sa.Boolean(), nullable=True),
        sa.Column("security_mode", sa.String(length=30), nullable=True),
        sa.Column("source_description", sa.Text(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("unavailable_reason", sa.String(length=500), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "availability IN ('AVAILABLE', 'UNAVAILABLE')",
            name=op.f("ck_metadata_routine_availability_state"),
        ),
        sa.CheckConstraint(
            "(availability = 'AVAILABLE') = (body_sql IS NOT NULL)",
            name=op.f("ck_metadata_routine_availability_matches_body"),
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_routine_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_routine_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["metadata_schema.id"],
            name=op.f("fk_metadata_routine_schema_id_metadata_schema"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_routine")),
        sa.UniqueConstraint(
            "schema_id", "name", "signature", name=op.f("uq_metadata_routine_schema_id")
        ),
    )
    for column in ("organization_id", "datasource_id", "schema_id"):
        op.create_index(op.f(f"ix_metadata_routine_{column}"), "metadata_routine", [column])
    op.create_index(
        "ix_metadata_routine_org_status", "metadata_routine", ["organization_id", "status"]
    )

    op.create_table(
        "metadata_routine_parameter",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("ordinal_position", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("physical_type", sa.String(length=255), nullable=False),
        sa.Column("default_expression", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_routine_parameter_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_routine_parameter_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f("fk_metadata_routine_parameter_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_routine_parameter")),
        sa.UniqueConstraint(
            "routine_id",
            "ordinal_position",
            name=op.f("uq_metadata_routine_parameter_routine_id"),
        ),
    )
    for column in ("organization_id", "datasource_id", "routine_id"):
        op.create_index(
            op.f(f"ix_metadata_routine_parameter_{column}"),
            "metadata_routine_parameter",
            [column],
        )
    op.create_index(
        "ix_metadata_routine_parameter_org_status",
        "metadata_routine_parameter",
        ["organization_id", "status"],
    )

    op.create_table(
        "metadata_object_description",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=20), nullable=False),
        sa.Column("catalog_id", sa.Uuid(), nullable=True),
        sa.Column("schema_id", sa.Uuid(), nullable=True),
        sa.Column("column_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "object_type IN ('CATALOG', 'SCHEMA', 'COLUMN')",
            name=op.f("ck_metadata_object_description_object_type_is_describable"),
        ),
        sa.CheckConstraint(
            "(CASE WHEN catalog_id IS NULL THEN 0 ELSE 1 END) "
            "+ (CASE WHEN schema_id IS NULL THEN 0 ELSE 1 END) "
            "+ (CASE WHEN column_id IS NULL THEN 0 ELSE 1 END) = 1",
            name=op.f("ck_metadata_object_description_exactly_one_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["metadata_catalog.id"],
            name=op.f("fk_metadata_object_description_catalog_id_metadata_catalog"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["column_id"],
            ["metadata_column.id"],
            name=op.f("fk_metadata_object_description_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_object_description_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_object_description_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["metadata_schema.id"],
            name=op.f("fk_metadata_object_description_schema_id_metadata_schema"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_object_description")),
        sa.UniqueConstraint(
            "catalog_id", name=op.f("uq_metadata_object_description_catalog_id")
        ),
        sa.UniqueConstraint("column_id", name=op.f("uq_metadata_object_description_column_id")),
        sa.UniqueConstraint("schema_id", name=op.f("uq_metadata_object_description_schema_id")),
    )
    for column in ("organization_id", "datasource_id", "catalog_id", "schema_id", "column_id"):
        op.create_index(
            op.f(f"ix_metadata_object_description_{column}"),
            "metadata_object_description",
            [column],
        )
    op.create_index(
        "ix_metadata_object_description_org_status",
        "metadata_object_description",
        ["organization_id", "status"],
    )

    op.create_table(
        "metadata_source_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("grant_key", sa.String(length=64), nullable=False),
        sa.Column("grantee", sa.String(length=255), nullable=False),
        sa.Column("grantee_type", sa.String(length=30), nullable=False),
        sa.Column("privilege", sa.String(length=50), nullable=False),
        sa.Column("object_type", sa.String(length=30), nullable=False),
        sa.Column("object_name", sa.String(length=255), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=True),
        sa.Column("is_grantable", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_source_grant_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_source_grant_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["metadata_schema.id"],
            name=op.f("fk_metadata_source_grant_schema_id_metadata_schema"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_source_grant")),
        sa.UniqueConstraint(
            "schema_id", "grant_key", name=op.f("uq_metadata_source_grant_schema_id")
        ),
    )
    for column in ("organization_id", "datasource_id", "schema_id"):
        op.create_index(
            op.f(f"ix_metadata_source_grant_{column}"), "metadata_source_grant", [column]
        )
    op.create_index(
        "ix_metadata_source_grant_org_status",
        "metadata_source_grant",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("metadata_source_grant")
    op.drop_table("metadata_object_description")
    op.drop_table("metadata_routine_parameter")
    op.drop_table("metadata_routine")
    op.drop_table("metadata_view_definition")
