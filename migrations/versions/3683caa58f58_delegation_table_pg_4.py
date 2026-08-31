"""delegation table (PG-4)

PG-4 ("Delegation and reassignment"): a principal delegates one or more of
its own governance roles to another principal for a bounded time window
(e.g. a steward going on leave delegates their governance-review decision
authority to a covering colleague). `aida.models.Delegation` is the
persisted grant; `aida.delegation.is_delegation_active` is the query-time
projection that decides whether it is currently honored --
`ck_delegation_window_ordered` only guards row-level sanity (the window
can't be inverted), it does not itself encode "currently active".

Revision ID: 3683caa58f58
Revises: bb909675ad3c
Create Date: 2026-08-31 06:59:58.510349
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3683caa58f58"
down_revision: str | Sequence[str] | None = "bb909675ad3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delegation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("delegator_principal_id", sa.String(length=255), nullable=False),
        sa.Column("delegate_principal_id", sa.String(length=255), nullable=False),
        sa.Column("delegated_roles", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("revoked_by", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expires_at > starts_at", name=op.f("ck_delegation_window_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_delegation_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delegation")),
    )
    op.create_index(
        op.f("ix_delegation_organization_id"),
        "delegation",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_delegation_org_delegate",
        "delegation",
        ["organization_id", "delegate_principal_id"],
        unique=False,
    )
    op.create_index(
        "ix_delegation_org_delegator",
        "delegation",
        ["organization_id", "delegator_principal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_delegation_org_delegator", table_name="delegation")
    op.drop_index("ix_delegation_org_delegate", table_name="delegation")
    op.drop_index(op.f("ix_delegation_organization_id"), table_name="delegation")
    op.drop_table("delegation")
