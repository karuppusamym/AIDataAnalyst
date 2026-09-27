"""drop slo_definition and slo_measurement

Revision ID: f3a91c27b5de
Revises: d41a7b8e6c02
Create Date: 2026-09-12 10:30:00.000000

Review 2026-09-11, row R11-D10. The SLO surface was half a feature: three
routes (`POST`/`GET /v1/observability/slo` and the budget read), an ORM pair,
three schemas and a Reliability panel -- with no writer for `slo_measurement`
anywhere in the tree, so `get_slo_budget` could only ever answer NO_DATA.

The row's choice was "wire a writer or retire deliberately", and the writer had
nothing to write. An SLO was bound to no measurable signal: `slo_key` was a
free-text slug (`^[a-z][a-z0-9_-]{1,99}$`) with no registry behind it and no
indicator field on the definition, so a scheduled collector could not know what
any given SLO meant. No SLI or indicator concept existed anywhere in `src/`.
The only real telemetry, the Prometheus exposition on `/metrics`
(`aida.main`, `aida.projection_metrics`), is scraped by nothing in this
repository -- no `compose*.yaml` and no `infra/` manifest deploys a Prometheus.
Wiring a writer therefore meant designing an indicator binding, a scrape path
and the UI to choose one: a new feature, not the completion of this one.

`slo_measurement` has never had a writer, so it is empty in every deployment.
`slo_definition` can hold rows wherever someone used the Reliability screen's
create form; those rows are declared intentions that nothing ever measured, and
dropping them is the deliberate part of the retirement. `downgrade` recreates
both tables and their indexes, not their contents.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a91c27b5de"
# Re-chained on integration 2026-09-12: this revision was authored against
# `d41a7b8e6c02`, and R11-C8's `a7c31f0b95e4` claimed that slot first. Two
# parallel sessions branching from one head is how a migration graph grows a
# second head; the two changes touch disjoint tables, so ordering them is the
# whole fix.
down_revision: str | Sequence[str] | None = "a7c31f0b95e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Child first: slo_measurement.slo_id references slo_definition(id).
    op.drop_index("ix_slo_measurement_organization_id", table_name="slo_measurement")
    op.drop_index("ix_slo_measurement_slo_time", table_name="slo_measurement")
    op.drop_index("ix_slo_measurement_slo_id", table_name="slo_measurement")
    op.drop_table("slo_measurement")

    op.drop_index("ix_slo_definition_organization_id", table_name="slo_definition")
    op.drop_index("ix_slo_definition_org_status", table_name="slo_definition")
    op.drop_table("slo_definition")


def downgrade() -> None:
    op.create_table(
        "slo_definition",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slo_key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
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
            name=op.f("fk_slo_definition_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slo_definition")),
        sa.UniqueConstraint(
            "organization_id", "slo_key", name="uq_slo_definition_organization_id"
        ),
    )
    op.create_index(
        "ix_slo_definition_org_status",
        "slo_definition",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_slo_definition_organization_id",
        "slo_definition",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "slo_measurement",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("budget_remaining", sa.Float(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_slo_measurement_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["slo_id"],
            ["slo_definition.id"],
            name=op.f("fk_slo_measurement_slo_id_slo_definition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slo_measurement")),
    )
    op.create_index("ix_slo_measurement_slo_id", "slo_measurement", ["slo_id"], unique=False)
    op.create_index(
        "ix_slo_measurement_slo_time",
        "slo_measurement",
        ["slo_id", "measured_at"],
        unique=False,
    )
    op.create_index(
        "ix_slo_measurement_organization_id",
        "slo_measurement",
        ["organization_id"],
        unique=False,
    )
