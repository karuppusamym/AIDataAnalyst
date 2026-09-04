"""p3-09 certification structured evidence

Adds `AssetCertification.evidence` (nullable JSON). Every pre-P3-09 row is
left with `evidence IS NULL` -- the audit trail rule that no historical row
is mutated by a schema migration applies here the same as it does for
P2-08's revoke/expiry-warning columns. New writes populate the column via
`aida.certification_evidence.compute_certification_evidence`; legacy rows
may be filled in best-effort by `backfill_certification_evidence_v1` (opt-in,
config-gated OFF by default -- see `Settings.certification_evidence_backfill_on_startup`).

Revision ID: d5b2e4f7a9c1
Revises: a1b2c3d4e5f6
Create Date: 2026-09-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5b2e4f7a9c1"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # P3-09: structured evidence blob captured at certify time. Shape
    # validated app-side by `aida.schemas.CertificationEvidence`. JSON
    # (portable between Postgres and SQLite) rather than JSONB, matching the
    # existing `DataQualityIncident.evidence` / `DataQualityObservation.evidence`
    # columns already in the schema; the store is small (five id lists and
    # a short notes field per certification) so no GIN index is needed. Stays
    # nullable indefinitely -- legacy pre-P3-09 rows keep `evidence IS NULL`
    # and are never NOT-NULL-tightened by a follow-up migration, because
    # certification history is retained audit evidence and is never mutated
    # by a background schema change.
    op.add_column(
        "asset_certification",
        sa.Column("evidence", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("asset_certification", "evidence")
