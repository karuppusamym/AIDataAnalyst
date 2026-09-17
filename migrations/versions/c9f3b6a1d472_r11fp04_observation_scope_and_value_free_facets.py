"""R11-FP04: observation scope, and the value-free aggregate facets of a column profile

Revision ID: c9f3b6a1d472
Revises: f4a8c1d7e236
Create Date: 2026-09-16

`table_profile.observation_scope` records how much of the table a profile actually saw, as
the connector itself reported it, instead of leaving it to be inferred downstream from
`sampled_row_count` against `row_count_estimate` -- a proxy that read a `LIMIT`-bounded
BigQuery profile as a full scan and an unbounded Snowflake one as sampled.

The ten `column_profile` columns are the value-free aggregate half of FP-04: a distinct
ratio and the uniqueness rule derived from it (previously re-derived, differently, by
`aida.relationship_validation` and `aida.composite_key_inference`), a cardinality class,
blank and whitespace-only counts, counts per code-defined length bucket, frequency entropy,
and the per-facet availability register. Every one is a statistic about values; none is a
value, a bucket boundary, an exemplar or a mode (ADR-0014/INV-6).

All eleven are nullable with no backfill and no server default. A profile written before
this revision made no claim about any of them, and NULL is how that reads: the readers fall
back to their previous derivation for exactly those rows rather than treating a missing
facet as a zero or as UNKNOWN.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f3b6a1d472"
down_revision: str | Sequence[str] | None = "f4a8c1d7e236"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN_PROFILE_FACETS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("distinct_ratio", sa.Float()),
    ("effectively_unique", sa.Boolean()),
    ("cardinality_class", sa.String(length=30)),
    ("blank_count", sa.BigInteger()),
    ("whitespace_only_count", sa.BigInteger()),
    ("length_bucket_scheme", sa.String(length=40)),
    ("length_bucket_counts", sa.JSON()),
    ("frequency_entropy_bits", sa.Float()),
    ("unavailable_facets", sa.JSON()),
)


def upgrade() -> None:
    op.add_column(
        "table_profile",
        sa.Column("observation_scope", sa.String(length=20), nullable=True),
    )
    for name, column_type in _COLUMN_PROFILE_FACETS:
        op.add_column("column_profile", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMN_PROFILE_FACETS):
        op.drop_column("column_profile", name)
    op.drop_column("table_profile", "observation_scope")
