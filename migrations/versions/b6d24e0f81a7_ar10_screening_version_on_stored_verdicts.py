"""AR-10: record the classifier version beside each stored screening verdict

Revision ID: b6d24e0f81a7
Revises: f3a91c27b5de
Create Date: 2026-09-12 11:15:00.000000

Review 2026-09-11, row R11-C7. `MetadataViewDefinition` and `MetadataRoutine`
store a screening verdict as a status plus its reason codes, and nothing else.
That makes the write-time screening policy unfalsifiable at the row level:
`CLEAN` under `deterministic-prompt-risk-v1+injection-defense-v1` and `CLEAN`
under today's rules are the same two columns, so a verdict an upgrade has left
behind is *indistinguishable from a current one*. The screening design's own
note accepted that an upgrade only reaches stored verdicts at a source's next
metadata scan -- a defensible policy -- while leaving no way to tell which rows
are still waiting for that scan. This column is what makes them findable.

`screening_version` is nullable, and NULL is load-bearing: it is what every row
written before this revision genuinely is, a verdict whose classifier is
unknown. `ingest_screening.is_verdict_current` reads NULL as stale. The
tempting alternative -- backfill the current version, or make the column NOT
NULL with a server default -- would stamp today's version onto verdicts today's
classifier never saw, which relocates the defect this revision exists to remove
rather than fixing it. So there is deliberately **no backfill**: the estate's
existing rows come out of this migration marked stale, because they are.

`downgrade` drops both columns. That loses which classifier judged each row,
and the rows themselves are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d24e0f81a7"
down_revision: str | None = "f3a91c27b5de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metadata_view_definition",
        sa.Column("screening_version", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "metadata_routine",
        sa.Column("screening_version", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("metadata_routine", "screening_version")
    op.drop_column("metadata_view_definition", "screening_version")
