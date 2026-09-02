"""at1 metadata playbook

Adds `metadata_playbook` (AT-1): a saved, scheduled bulk-metadata action --
a filter, a CT-1 action (TAG/CLASSIFY/OWN/CERTIFY), and a schedule -- run
automatically by the existing fleet scheduler. See `aida.playbooks` and
`aida.models.MetadataPlaybook` for the full design.

Revision ID: 62341fa9f017
Revises: c8fcafc5856a
Create Date: 2026-09-01 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "62341fa9f017"
down_revision: str | Sequence[str] | None = "c8fcafc5856a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_playbook",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("match_field", sa.String(length=20), nullable=False),
        sa.Column("match_pattern", sa.String(length=255), nullable=False),
        sa.Column("column_name_pattern", sa.String(length=255), nullable=True),
        sa.Column("action_parameters", sa.JSON(), nullable=False),
        sa.Column("schedule_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("auto_apply_max_items", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('TAG', 'CLASSIFY', 'OWN', 'CERTIFY')",
            name=op.f("ck_metadata_playbook_action_is_supported"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_playbook_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_playbook_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_playbook")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_metadata_playbook_organization_id")
        ),
    )
    op.create_index(
        op.f("ix_metadata_playbook_organization_id"),
        "metadata_playbook",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_playbook_datasource_id"),
        "metadata_playbook",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        "ix_metadata_playbook_org_enabled",
        "metadata_playbook",
        ["organization_id", "enabled"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_playbook_org_enabled", table_name="metadata_playbook")
    op.drop_index(op.f("ix_metadata_playbook_datasource_id"), table_name="metadata_playbook")
    op.drop_index(op.f("ix_metadata_playbook_organization_id"), table_name="metadata_playbook")
    op.drop_table("metadata_playbook")
