"""column tokenization policy (QG-6)

Adds `column_tokenization_policy`: an explicit, per-column steward declaration
that a catalog column is tokenized (reversible, format-preserving) rather than
fully redacted by the query gateway's masking pass. See
`aida.models.ColumnTokenizationPolicy` and `aida.tokenization` for the
provider protocol this wires into.

Revision ID: 2fa45be65bf7
Revises: 4f730e96ee9b
Create Date: 2026-08-31 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2fa45be65bf7"
down_revision: str | Sequence[str] | None = "4f730e96ee9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "column_tokenization_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("value_shape", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_column_tokenization_policy_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_column_tokenization_policy_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["column_id"],
            ["metadata_column.id"],
            name=op.f("fk_column_tokenization_policy_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_column_tokenization_policy")),
        sa.UniqueConstraint("column_id", name="uq_column_tokenization_policy_column"),
    )
    op.create_index(
        op.f("ix_column_tokenization_policy_organization_id"),
        "column_tokenization_policy",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_column_tokenization_policy_datasource_id"),
        "column_tokenization_policy",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_column_tokenization_policy_column_id"),
        "column_tokenization_policy",
        ["column_id"],
        unique=False,
    )
    op.create_index(
        "ix_column_tokenization_policy_org_datasource",
        "column_tokenization_policy",
        ["organization_id", "datasource_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_column_tokenization_policy_org_datasource",
        table_name="column_tokenization_policy",
    )
    op.drop_index(
        op.f("ix_column_tokenization_policy_column_id"),
        table_name="column_tokenization_policy",
    )
    op.drop_index(
        op.f("ix_column_tokenization_policy_datasource_id"),
        table_name="column_tokenization_policy",
    )
    op.drop_index(
        op.f("ix_column_tokenization_policy_organization_id"),
        table_name="column_tokenization_policy",
    )
    op.drop_table("column_tokenization_policy")
