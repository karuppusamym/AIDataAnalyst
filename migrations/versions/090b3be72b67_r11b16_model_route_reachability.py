"""R11-B16: record whether an approved route's model is still served.

Three nullable columns on `model_route_configuration`, and nullable is the
point: NULL means "never checked", which is a different fact from "checked and
could not tell" (UNKNOWN) and from "checked and gone" (UNREACHABLE). A
backfilled default would erase that distinction on every existing route and
claim a check that never ran.

Nothing here touches `status`. An approval is a human decision made through
maker-checker, and reachability is recorded beside it so a sweep can never
revoke one.

Revision ID: 090b3be72b67
Revises: b6d24e0f81a7
Create Date: 2026-09-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '090b3be72b67'
down_revision: str | Sequence[str] | None = 'b6d24e0f81a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_route_configuration",
        sa.Column("reachability_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "model_route_configuration",
        sa.Column("reachability_detail", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "model_route_configuration",
        sa.Column("reachability_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_route_configuration", "reachability_checked_at")
    op.drop_column("model_route_configuration", "reachability_detail")
    op.drop_column("model_route_configuration", "reachability_status")

