"""at7b context product consumer binding

Adds `context_product_consumer_binding` (AT-7b): pins one named consumer to
one specific Context Product version, for staged rollout under explicit
operator control -- some consumers stay on a prior version deliberately
while others move, rather than a blind percentage/weight split (declined by
the tracker). One binding per (product, consumer): a `PUT`-shaped upsert
moves an existing binding rather than appending a new row.

Revision ID: eaf120430212
Revises: 75838f5c1cea
Create Date: 2026-09-01 11:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eaf120430212"
down_revision: str | Sequence[str] | None = "75838f5c1cea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_product_consumer_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_principal_id", sa.String(length=255), nullable=False),
        sa.Column("bound_version_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_context_product_consumer_binding_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["context_product.id"],
            name=op.f("fk_context_product_consumer_binding_product_id_context_product"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bound_version_id"],
            ["context_product_version.id"],
            name=op.f(
                "fk_context_product_consumer_binding_bound_version_id_context_product_version"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_product_consumer_binding")),
        sa.UniqueConstraint(
            "product_id",
            "consumer_principal_id",
            name=op.f("uq_context_product_consumer_binding_product_consumer"),
        ),
    )
    op.create_index(
        op.f("ix_context_product_consumer_binding_organization_id"),
        "context_product_consumer_binding",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_consumer_binding_product_id"),
        "context_product_consumer_binding",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_consumer_binding_bound_version_id"),
        "context_product_consumer_binding",
        ["bound_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_context_product_consumer_binding_org_product",
        "context_product_consumer_binding",
        ["organization_id", "product_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_product_consumer_binding_org_product",
        table_name="context_product_consumer_binding",
    )
    op.drop_index(
        op.f("ix_context_product_consumer_binding_bound_version_id"),
        table_name="context_product_consumer_binding",
    )
    op.drop_index(
        op.f("ix_context_product_consumer_binding_product_id"),
        table_name="context_product_consumer_binding",
    )
    op.drop_index(
        op.f("ix_context_product_consumer_binding_organization_id"),
        table_name="context_product_consumer_binding",
    )
    op.drop_table("context_product_consumer_binding")
