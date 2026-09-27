"""deep procedure lineage edges get ADR-0026's review state

Revision ID: d81f5a2c9e47
Revises: 7c2d94e1b8a3
Create Date: 2026-09-11 18:00:00.000000

ADR-0026 gave the five parser-produced lineage edge tables a review lifecycle
(migration 0026a6f31c05). `deep_procedure_lineage_edge` -- the routine-aware
table the N3 parser writes -- arrived later and never got one, so nothing it
holds could wait for a person. This adds the same six columns and the same
index. The same `server_default="ACTIVE"` makes it safe to apply live: every
existing row keeps its meaning, and a person's parse under the default
`auto_active` mode still lands ACTIVE. The lineage agent (ADR-0029) writes
PROPOSED rows here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d81f5a2c9e47"
down_revision: str | Sequence[str] | None = "7c2d94e1b8a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "deep_procedure_lineage_edge"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "review_status",
            sa.String(length=20),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.add_column(_TABLE, sa.Column("reviewed_by", sa.String(length=255), nullable=True))
    op.add_column(_TABLE, sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("review_reason", sa.String(length=2000), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "previous_edge_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey(f"{_TABLE}.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(_TABLE, sa.Column("created_by", sa.String(length=255), nullable=True))
    op.create_index(f"ix_{_TABLE}_review_status", _TABLE, ["review_status"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_review_status", table_name=_TABLE)
    op.drop_column(_TABLE, "created_by")
    op.drop_column(_TABLE, "previous_edge_id")
    op.drop_column(_TABLE, "review_reason")
    op.drop_column(_TABLE, "reviewed_at")
    op.drop_column(_TABLE, "reviewed_by")
    op.drop_column(_TABLE, "review_status")
