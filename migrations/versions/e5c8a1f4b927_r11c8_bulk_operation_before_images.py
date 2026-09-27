"""R11-C8: record what an overwriting bulk operation replaced

Revision ID: e5c8a1f4b927
Revises: 090b3be72b67
Create Date: 2026-09-12

`applied_subject_ids` (a7c31f0b95e4) made the *additive* operation types
reversible: LINK_TERM and CERTIFY_ASSET add rows that did not exist, so undoing
them needs only the ids of what they added. The overwriting types could not be
undone, because nothing recorded what they overwrote -- TAG replaces a tag's
value, CLASSIFY replaces a column's classification -- and reversing them by
deleting the tag or clearing the classification would be a second wrong change
dressed as a correction.

`applied_before_images` is that record: for each applied subject, what it held
before the operation wrote to it. It is filled for TAG and CLASSIFY. The other
overwriting types -- ASSIGN_OWNERSHIP, DEPRECATE_TERM, REASSIGN_LEAVER -- still
have no compensating action and are still refused by name.

Backfill is deliberately not attempted, for the reason the ledger migration
gave: an empty object on an existing row means "not recorded", never "nothing
was overwritten", and `request_bulk_operation_reversal` refuses such a row
rather than restoring a guess. Operations applied before this migration are
therefore not reversible through this path.

`downgrade` drops the column, and with it the only record of what any
operation applied in the meantime overwrote, so those operations become
permanently unreversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5c8a1f4b927"
down_revision: str | Sequence[str] | None = "090b3be72b67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bulk_stewardship_operation",
        sa.Column(
            "applied_before_images",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bulk_stewardship_operation", "applied_before_images")
