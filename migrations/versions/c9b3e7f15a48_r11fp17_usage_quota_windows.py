"""R11-FP17: per-tenant and per-source daily usage/quota windows

Revision ID: c9b3e7f15a48
Revises: a3f71c58d94b
Create Date: 2026-09-16

Two accumulator tables, `tenant_usage_window` and `source_usage_window`, one row
per scope per metered dimension per UTC day. They are what `aida.usage_quotas`
moves with a conditional UPDATE carrying the cap in its own `WHERE`, so a quota
cannot be broken by two concurrent callers that both read the day's total before
either writes -- the same mechanism, and the same reason, as
`agent_budget_window` (AG-10 / AR-05).

Two tables rather than one with a nullable `datasource_id`: PostgreSQL treats
NULLs as distinct in a unique constraint, so a single table would not actually
enforce one row per scope per day, and a duplicated window is a quota that
counts half the traffic.

`used` is `BigInteger` where `agent_budget_window.reserved_tokens` is
`Integer`. A whole organization's daily model tokens across every agent and
every source can pass a signed 32-bit integer in a large estate, and a quota
accumulator that overflows fails *open*.

No backfill and no data migration. Every `*_daily_quota_*` setting ships as
`None` (no quota declared), so `consume_quota` issues no statement at all on a
default deployment and these tables stay empty until an estate either declares
a quota or the per-source cost recording in `aida.cost_metrics` runs.
`organization_id` is `RESTRICT` (a tenant with usage history cannot be deleted
out from under it) while `datasource_id` is `CASCADE` (a removed source's
history goes with it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9b3e7f15a48"
down_revision: str | Sequence[str] | None = "a3f71c58d94b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_usage_window",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(length=40), nullable=False),
        sa.Column("window_date", sa.Date(), nullable=False),
        sa.Column("used", sa.BigInteger(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_tenant_usage_window_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_usage_window")),
        sa.UniqueConstraint(
            "organization_id",
            "dimension",
            "window_date",
            name="uq_tenant_usage_window_scope",
        ),
        sa.CheckConstraint("used >= 0", name="ck_tenant_usage_window_non_negative"),
    )
    op.create_index(
        op.f("ix_tenant_usage_window_organization_id"),
        "tenant_usage_window",
        ["organization_id"],
    )
    op.create_index(
        "ix_tenant_usage_window_org_date",
        "tenant_usage_window",
        ["organization_id", "window_date"],
    )

    op.create_table(
        "source_usage_window",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(length=40), nullable=False),
        sa.Column("window_date", sa.Date(), nullable=False),
        sa.Column("used", sa.BigInteger(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_source_usage_window_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_source_usage_window_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_usage_window")),
        sa.UniqueConstraint(
            "organization_id",
            "datasource_id",
            "dimension",
            "window_date",
            name="uq_source_usage_window_scope",
        ),
        sa.CheckConstraint("used >= 0", name="ck_source_usage_window_non_negative"),
    )
    op.create_index(
        op.f("ix_source_usage_window_organization_id"),
        "source_usage_window",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_source_usage_window_datasource_id"),
        "source_usage_window",
        ["datasource_id"],
    )
    op.create_index(
        "ix_source_usage_window_source_date",
        "source_usage_window",
        ["datasource_id", "window_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_usage_window_source_date", table_name="source_usage_window")
    op.drop_index(
        op.f("ix_source_usage_window_datasource_id"), table_name="source_usage_window"
    )
    op.drop_index(
        op.f("ix_source_usage_window_organization_id"), table_name="source_usage_window"
    )
    op.drop_table("source_usage_window")
    op.drop_index("ix_tenant_usage_window_org_date", table_name="tenant_usage_window")
    op.drop_index(
        op.f("ix_tenant_usage_window_organization_id"), table_name="tenant_usage_window"
    )
    op.drop_table("tenant_usage_window")
