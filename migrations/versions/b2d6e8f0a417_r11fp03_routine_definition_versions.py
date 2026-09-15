"""R11-FP03: immutable routine definition versions

Revision ID: b2d6e8f0a417
Revises: 9c3f5a1e7d42
Create Date: 2026-09-15

`metadata_routine_definition_version` keeps every captured definition of a routine instead of only
the current one (`aida.envelope_models.MetadataRoutineDefinitionVersion`): one row on first capture
and one each time the raw-text fingerprint or availability moves, with `change_class` saying
whether only literals changed. Rows are never updated. The stored text is the redacted form, as on
`metadata_routine`.

Backfill: every existing routine gets version 1 from its current row, so history starts at this
migration rather than at the next change, and a first change after deploy is version 2.
`downgrade` drops the table and all history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d6e8f0a417"
down_revision: str | Sequence[str] | None = "9c3f5a1e7d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "metadata_routine_definition_version"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("body_sql_redacted", sa.Text(), nullable=True),
        sa.Column("body_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("unavailable_reason", sa.String(length=500), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("redaction_status", sa.String(length=20), nullable=False),
        sa.Column("screening_status", sa.String(length=20), nullable=False),
        sa.Column("change_class", sa.String(length=20), nullable=True),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "availability IN ('AVAILABLE', 'UNAVAILABLE')",
            name=op.f("ck_metadata_routine_definition_version_availability_state"),
        ),
        sa.CheckConstraint(
            "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL')",
            name=op.f("ck_metadata_routine_definition_version_change_class"),
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name=op.f("ck_metadata_routine_definition_version_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_run.id"],
            name=op.f("fk_metadata_routine_definition_version_analysis_run_id_analysis_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_routine_definition_version_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_routine_definition_version_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f("fk_metadata_routine_definition_version_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_routine_definition_version")),
        sa.UniqueConstraint(
            "routine_id",
            "version_number",
            name=op.f("uq_metadata_routine_definition_version_routine_id"),
        ),
    )
    for column in ("organization_id", "datasource_id", "routine_id", "analysis_run_id"):
        op.create_index(op.f(f"ix_{_TABLE}_{column}"), _TABLE, [column], unique=False)
    op.execute(
        f"""
        INSERT INTO {_TABLE} (
            id, organization_id, datasource_id, routine_id, version_number,
            body_sql_redacted, body_fingerprint, availability, unavailable_reason, truncated,
            redaction_status, screening_status, change_class, analysis_run_id, captured_at
        )
        SELECT
            gen_random_uuid(), organization_id, datasource_id, id, 1,
            body_sql_redacted, body_fingerprint, availability, unavailable_reason, truncated,
            redaction_status, screening_status, NULL, NULL, updated_at
        FROM metadata_routine
        """  # noqa: S608 -- static table names, no input
    )


def downgrade() -> None:
    for column in ("analysis_run_id", "routine_id", "datasource_id", "organization_id"):
        op.drop_index(op.f(f"ix_{_TABLE}_{column}"), table_name=_TABLE)
    op.drop_table(_TABLE)
