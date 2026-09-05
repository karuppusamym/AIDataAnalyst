"""Reinstating a withdrawn description.

`d9a3f61c2b07` made retirement possible; taking a retirement back was not. The
remedy was "publish the text again", which has no direct path by design -- every
publish goes through review, and there is deliberately no direct-write endpoint.

`request_type` discriminates the two directions on the existing table rather
than adding a second one: both are the same decision shape (this asset, this
exact version, this reason, decided by someone else) and every existing column
serves both. Existing rows are all withdrawals, so the backfill is the default.

Reinstatement republishes as a *new* version; it never flips a WITHDRAWN row
back to APPROVED, which would rewrite history and lose the fact that the
description was ever retired.

Revision ID: e4b8d2f71a95
Revises: 69702d37d798
Create Date: 2026-09-05 00:00:00

Chained after `69702d37d798` rather than after `d9a3f61c2b07` (the migration
that created this table): that one had already been branched from by a parallel
workstream, and every migration here lands in the same shared linear branch --
two heads is the failure `test_migration_orm_drift` reports first, before it can
compare anything.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b8d2f71a95"
down_revision: str | Sequence[str] | None = "69702d37d798"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "description_withdrawal",
        sa.Column(
            "request_type",
            sa.String(20),
            nullable=False,
            # Every row that exists before this migration is a withdrawal.
            server_default="WITHDRAW",
        ),
    )
    op.create_check_constraint(
        "withdrawal_request_type_is_supported",
        "description_withdrawal",
        "request_type IN ('WITHDRAW', 'REINSTATE')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "withdrawal_request_type_is_supported",
        "description_withdrawal",
        type_="check",
    )
    op.drop_column("description_withdrawal", "request_type")
