"""WORM audit archive: two-phase lifecycle, explicit membership, per-org lease.

Three schema consequences of `Docs/review-2026-09-05/REVIEW.md` F01-F03.

**Lifecycle columns on `audit_archive_record`.** The archiver used to return
a success object without writing anything anywhere, and the row it persisted
described an archive that did not exist. `state` makes the difference
representable: PREPARED, UPLOADED, VERIFIED, FAILED. `storage_uri`,
`uploaded_at`, `verified_at` and `retention_acknowledged_at` record what a
destination actually acknowledged; `serialization_version` and
`checksum_algorithm` say which algorithm verifies the stored checksum, so
read-back never has to guess.

Existing rows are backfilled to `LEGACY_UNVERIFIED`, not to VERIFIED. They
were written by the code path that stored nothing, so calling them verified
would preserve the exact false claim this change removes. They are kept as
evidence that an attempt was made and are excluded from the archive-status
endpoint's counts.

**No membership backfill, deliberately.** `audit_archive_membership` is
populated only by the new archiver. Legacy rows get none, so the events they
claimed are unclaimed and the next sweep archives them -- for the first
time, since nothing was ever stored for them. Reconstructing membership from
the legacy `event_range_start`/`event_range_end` window would suppress that,
and would suppress it on the strength of a range that was never a reliable
description of the batch in the first place (F03's equal-timestamp defect).

**`audit_archive_lease`** is one row per organization, so replicas do not all
sweep the same organization at once. A lease row rather than an advisory
lock: the claim has to outlive the connection that took it, and SQLite -- the
test suite's dialect -- has no advisory lock.

Revision ID: a71c5e0d9f34
Revises: e4b8d2f71a95
Create Date: 2026-09-06 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a71c5e0d9f34"
down_revision: str | Sequence[str] | None = "e4b8d2f71a95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_archive_record",
        sa.Column("state", sa.String(24), nullable=False, server_default="LEGACY_UNVERIFIED"),
    )
    op.add_column("audit_archive_record", sa.Column("storage_uri", sa.String(1000)))
    op.add_column(
        "audit_archive_record",
        sa.Column("serialization_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "audit_archive_record",
        sa.Column("checksum_algorithm", sa.String(30), nullable=False, server_default="sha256"),
    )
    op.add_column("audit_archive_record", sa.Column("uploaded_at", sa.DateTime(timezone=True)))
    op.add_column("audit_archive_record", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.add_column(
        "audit_archive_record",
        sa.Column("retention_acknowledged_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "audit_archive_record",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("audit_archive_record", sa.Column("failure_reason", sa.String(1000)))
    op.add_column("audit_archive_record", sa.Column("legal_hold_reason", sa.String(500)))
    op.add_column(
        "audit_archive_record",
        sa.Column("legal_hold_applied_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "audit_archive_record",
        sa.Column("legal_hold_released_at", sa.DateTime(timezone=True)),
    )
    op.add_column("audit_archive_record", sa.Column("event_range_start_id", sa.BigInteger()))
    op.add_column("audit_archive_record", sa.Column("event_range_end_id", sa.BigInteger()))
    op.create_index(
        op.f("ix_audit_archive_org_state"),
        "audit_archive_record",
        ["organization_id", "state"],
    )

    # Constraint names are spelled out with `op.f(...)` to match exactly what
    # `Base.metadata`'s naming convention produces; `test_migration_orm_drift`
    # compares them, and an auto-named constraint here would read as drift.
    op.create_table(
        "audit_archive_membership",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("audit_event_id", sa.BigInteger(), nullable=False),
        sa.Column("archive_record_id", sa.Uuid(), nullable=False),
        sa.Column("archive_id", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_audit_archive_membership_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["archive_record_id"],
            ["audit_archive_record.id"],
            name=op.f("fk_audit_archive_membership_archive_record_id_audit_archive_record"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_archive_membership")),
        sa.UniqueConstraint(
            "organization_id", "audit_event_id", name="uq_audit_archive_membership_event"
        ),
    )
    op.create_index(
        op.f("ix_audit_archive_membership_record"),
        "audit_archive_membership",
        ["archive_record_id"],
    )
    op.create_index(
        op.f("ix_audit_archive_membership_cursor"),
        "audit_archive_membership",
        ["organization_id", "occurred_at"],
    )

    op.create_table(
        "audit_archive_lease",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_audit_archive_lease_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("organization_id", name=op.f("pk_audit_archive_lease")),
    )


def downgrade() -> None:
    op.drop_table("audit_archive_lease")
    op.drop_index(op.f("ix_audit_archive_membership_cursor"), table_name="audit_archive_membership")
    op.drop_index(op.f("ix_audit_archive_membership_record"), table_name="audit_archive_membership")
    op.drop_table("audit_archive_membership")
    op.drop_index(op.f("ix_audit_archive_org_state"), table_name="audit_archive_record")
    for column in (
        "event_range_end_id",
        "event_range_start_id",
        "legal_hold_released_at",
        "legal_hold_applied_at",
        "legal_hold_reason",
        "failure_reason",
        "attempt_count",
        "retention_acknowledged_at",
        "verified_at",
        "uploaded_at",
        "checksum_algorithm",
        "serialization_version",
        "storage_uri",
        "state",
    ):
        op.drop_column("audit_archive_record", column)
