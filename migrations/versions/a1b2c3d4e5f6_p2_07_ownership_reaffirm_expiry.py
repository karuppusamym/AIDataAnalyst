"""p2-07 ownership assignment re-affirmation, expiry, and leaver flip

Revision ID: a1b2c3d4e5f6
Revises: 0026a6f31c05
Create Date: 2026-09-04 15:00:00.000000

P2-07: closes the audit gap that `OwnershipAssignment` was claimed once and
never re-affirmed, had no expiry-warning job, and had no identity-
merge/delete -> REASSIGNED path (dangling ownership on user delete). Adds
four new nullable columns:

* ``expires_at`` -- horizon by which the owner must re-affirm the assignment
  (``now + settings.ownership_reaffirm_days``, default 180d). Nullable so
  every existing pre-P2-07 row keeps its pre-P2-07 meaning (no expiry until
  it's re-affirmed or freshly assigned under P2-07 code). The expiry-warning
  sweep skips rows where this is NULL.
* ``expiry_warning_emitted_at`` -- idempotency stamp mirroring the P2-08
  ``asset_certification`` column of the same name.
* ``reaffirmed_at`` / ``reaffirmed_by`` -- last time an owner (or admin)
  confirmed the assignment via ``POST /v1/ownership-assignments/{id}/reaffirm``.

Also adds a status-column index used by both the warning sweep and the
identity-lifecycle handler (each queries ``status='ACTIVE'`` and either
``expires_at`` or ``owner_principal``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "0026a6f31c05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ownership_assignment",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ownership_assignment",
        sa.Column(
            "expiry_warning_emitted_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "ownership_assignment",
        sa.Column("reaffirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ownership_assignment",
        sa.Column("reaffirmed_by", sa.String(length=255), nullable=True),
    )
    # Both the warning sweep and the identity-lifecycle handler filter by
    # (status='ACTIVE', expires_at) and (status='ACTIVE', owner_principal)
    # respectively; the composite index below covers both without duplicating
    # the (organization_id, subject_type) index that already exists.
    op.create_index(
        "ix_ownership_assignment_status_expires_at",
        "ownership_assignment",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_ownership_assignment_owner_principal_status",
        "ownership_assignment",
        ["owner_principal", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ownership_assignment_owner_principal_status",
        table_name="ownership_assignment",
    )
    op.drop_index(
        "ix_ownership_assignment_status_expires_at",
        table_name="ownership_assignment",
    )
    op.drop_column("ownership_assignment", "reaffirmed_by")
    op.drop_column("ownership_assignment", "reaffirmed_at")
    op.drop_column("ownership_assignment", "expiry_warning_emitted_at")
    op.drop_column("ownership_assignment", "expires_at")
