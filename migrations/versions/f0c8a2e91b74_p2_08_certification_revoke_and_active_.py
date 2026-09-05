"""p2-08 certification revoke and active tuple uniqueness

Revision ID: f0c8a2e91b74
Revises: 54ad108f9b8a
Create Date: 2026-09-04 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0c8a2e91b74"
down_revision: str | Sequence[str] | None = "54ad108f9b8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # P2-08 (a): manual revoke columns. Nullable because every pre-P2-08 row was
    # certified-then-either-superseded-or-expired via the existing paths and
    # was never revoked; the new
    # `POST /v1/tables/{table_id}/certification/revoke` endpoint is the only
    # writer of these three columns.
    op.add_column(
        "asset_certification",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "asset_certification",
        sa.Column("revoked_by", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "asset_certification",
        sa.Column("revocation_reason", sa.String(length=2000), nullable=True),
    )
    # P2-08 (b): expiry-warning idempotency stamp. Written by
    # `warn_upcoming_certification_expiries` when it emits the notification for
    # a row, so the same cert does not warn twice inside one warning window.
    op.add_column(
        "asset_certification",
        sa.Column(
            "expiry_warning_emitted_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    # P2-08 (c): partial unique index on the ACTIVE tuple.
    #
    # This is the last-mile atomicity guarantee against two concurrent certify
    # calls both landing an ACTIVE row for the same (table, asset_type, column,
    # organization). The existing app-side "select prior ACTIVE, flip to
    # SUPERSEDED, insert new" (see `certify_table_asset`,
    # `catalog_bulk_actions.apply_certify_item`) is a read-modify-write with
    # nothing locking the tuple between the read and the insert -- two
    # connections can both see "no prior active" and both commit an ACTIVE row.
    # Only a database-level uniqueness constraint refuses the second insert.
    #
    # `column_id` participates via COALESCE to the zero-UUID sentinel because
    # PostgreSQL treats NULL as *distinct* inside a unique index, so two
    # concurrent table-level certifies (both `column_id IS NULL`) would
    # otherwise both slip past. The zero-UUID is not a valid ORM-emitted
    # `column_id` (all column ids are `uuid4()`), so this cannot collide with a
    # real column-level certification.
    #
    # SQLite honours the identical partial-index syntax (`WHERE`) too, so the
    # in-memory test database exercises the same constraint as production.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_asset_certification_active_tuple
        ON asset_certification (
            table_id,
            asset_type,
            COALESCE(column_id, '00000000-0000-0000-0000-000000000000'),
            organization_id
        )
        WHERE status = 'ACTIVE'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_asset_certification_active_tuple")
    op.drop_column("asset_certification", "expiry_warning_emitted_at")
    op.drop_column("asset_certification", "revocation_reason")
    op.drop_column("asset_certification", "revoked_by")
    op.drop_column("asset_certification", "revoked_at")
