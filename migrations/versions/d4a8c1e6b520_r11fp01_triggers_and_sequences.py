"""R11-FP01: triggers and sequences as their own native object kinds

Revision ID: d4a8c1e6b520
Revises: c9b3e7f15a48
Create Date: 2026-09-17

Two tables for two native kinds that no adapter discovered and no model could
hold. `ObjectKind` ran TABLE / VIEW / MATERIALIZED_VIEW / PROCEDURE / FUNCTION /
PACKAGE, and a trigger fitted none of them: it is not called but *fires*, on a
named table, for a named event, at a named time. A sequence fitted none of them
either: it holds no rows, has no columns, and is read by somebody else's default
expression.

**Why two tables rather than columns on the existing 1.1 axes.** Review
2026-09-16's own rule is that a native kind keeps its native identity even
where it shares a graph category ("an Oracle package, PostgreSQL materialized
view and SQL Server indexed view must retain their native identity"). A trigger
folded onto `metadata_routine` would have to report its firing table as a
parameter or lose it; a sequence folded onto `metadata_table` would be offered
for profiling. See `aida.envelope_models.MetadataTrigger` /
`MetadataSequence` for the per-column argument.

**`metadata_trigger` carries the body apparatus `metadata_routine` carries, and
for the same reason.** A trigger body is SQL, SQL carries source values in its
literals (INV-6), and it reaches model context by the paths a procedure body
does -- so the stored text is the literal-redacted form, `body_fingerprint` is a
digest of the original, and the verdict columns are the write-time screening.
The two CHECK constraints are the pair `metadata_view_definition` and
`metadata_routine` already carry: `availability` is a closed two-value
vocabulary, and it must agree with whether there is text, so "the source refused
the body" can never be stored as "the trigger has an empty body".

`metadata_trigger` is keyed `(schema_id, table_name, name)`. PostgreSQL scopes a
trigger name to its *table*, so two tables in one schema may both own
`audit_trg`; Oracle and SQL Server scope it to the schema. The tighter engine
decides the key -- keyed `(schema_id, name)`, the two PostgreSQL triggers would
collide and each FULL rescan would soft-delete one of them, which is the
overload defect `metadata_routine.signature` exists to prevent.

**`metadata_sequence` has no `availability` pair and no current-position
column.** A sequence has no defining text to be refused: its declaration is its
metadata, the way a base relation's columns are the fact. And its current
position (`pg_sequences.last_value`, `ALL_SEQUENCES.LAST_NUMBER`,
`sys.sequences.current_value`) is source data -- the value the next insert
writes into a customer's row -- so it is not read, not carried on the envelope
and has no column here (INV-6). Every numeric declaration parameter is
`String(64)`: Oracle permits a 28-digit `MAXVALUE` and PostgreSQL a `bigint`
one, no integer column is wide enough for both, and a declaration is compared
and displayed rather than used in arithmetic.

`ix_metadata_trigger_firing_table` is the one index here that is not a foreign
key or a status filter. It answers the question the axis exists for -- "what
fires when this table changes?" -- which is a per-source lookup by table name,
and without it that read is a sequential scan of every trigger in the estate.

No backfill and no data migration: both tables start empty, and they stay empty
on an existing deployment until a rediscovery run writes into them.

`organization_id` is `RESTRICT` and `datasource_id`/`schema_id` are `CASCADE`,
exactly as on every other envelope axis (INV-5): a tenant with metadata cannot
be deleted out from under it, and a removed source or schema takes its objects
with it rather than leaving rows pointing at nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a8c1e6b520"
down_revision: str | Sequence[str] | None = "c9b3e7f15a48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_trigger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("table_name", sa.String(length=255), nullable=False),
        sa.Column("table_schema_name", sa.String(length=255), nullable=True),
        sa.Column("timing", sa.String(length=20), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("orientation", sa.String(length=20), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=True),
        sa.Column("action_routine", sa.String(length=511), nullable=True),
        sa.Column("body_sql_redacted", sa.Text(), nullable=True),
        sa.Column("body_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("redaction_status", sa.String(length=20), nullable=False),
        sa.Column("screening_status", sa.String(length=20), nullable=False),
        sa.Column("screening_reason_codes", sa.JSON(), nullable=False),
        sa.Column("screening_version", sa.String(length=100), nullable=True),
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
            name=op.f("ck_metadata_trigger_availability_state"),
        ),
        sa.CheckConstraint(
            "(availability = 'AVAILABLE') = (body_sql_redacted IS NOT NULL)",
            name=op.f("ck_metadata_trigger_availability_matches_body"),
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_trigger_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_trigger_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["metadata_schema.id"],
            name=op.f("fk_metadata_trigger_schema_id_metadata_schema"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_trigger")),
        sa.UniqueConstraint(
            "schema_id",
            "table_name",
            "name",
            name=op.f("uq_metadata_trigger_schema_id"),
        ),
    )
    for column in ("organization_id", "datasource_id", "schema_id"):
        op.create_index(
            op.f(f"ix_metadata_trigger_{column}"), "metadata_trigger", [column]
        )
    op.create_index(
        "ix_metadata_trigger_org_status",
        "metadata_trigger",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_metadata_trigger_firing_table",
        "metadata_trigger",
        ["datasource_id", "table_name"],
    )

    op.create_table(
        "metadata_sequence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("data_type", sa.String(length=255), nullable=True),
        sa.Column("start_with", sa.String(length=64), nullable=True),
        sa.Column("increment_by", sa.String(length=64), nullable=True),
        sa.Column("minimum_bound", sa.String(length=64), nullable=True),
        sa.Column("maximum_bound", sa.String(length=64), nullable=True),
        sa.Column("cache_size", sa.String(length=64), nullable=True),
        sa.Column("cycles", sa.Boolean(), nullable=True),
        sa.Column("owned_by_table", sa.String(length=255), nullable=True),
        sa.Column("owned_by_column", sa.String(length=255), nullable=True),
        sa.Column("source_description", sa.Text(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_sequence_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_sequence_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["metadata_schema.id"],
            name=op.f("fk_metadata_sequence_schema_id_metadata_schema"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_sequence")),
        sa.UniqueConstraint(
            "schema_id", "name", name=op.f("uq_metadata_sequence_schema_id")
        ),
    )
    for column in ("organization_id", "datasource_id", "schema_id"):
        op.create_index(
            op.f(f"ix_metadata_sequence_{column}"), "metadata_sequence", [column]
        )
    op.create_index(
        "ix_metadata_sequence_org_status",
        "metadata_sequence",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_sequence_org_status", table_name="metadata_sequence")
    for column in ("schema_id", "datasource_id", "organization_id"):
        op.drop_index(op.f(f"ix_metadata_sequence_{column}"), table_name="metadata_sequence")
    op.drop_table("metadata_sequence")
    op.drop_index("ix_metadata_trigger_firing_table", table_name="metadata_trigger")
    op.drop_index("ix_metadata_trigger_org_status", table_name="metadata_trigger")
    for column in ("schema_id", "datasource_id", "organization_id"):
        op.drop_index(op.f(f"ix_metadata_trigger_{column}"), table_name="metadata_trigger")
    op.drop_table("metadata_trigger")
