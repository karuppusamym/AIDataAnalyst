"""kill switch state (MG-2)

Adds `kill_switch_state`, the current-state table backing the model-gateway
kill switch (module 15, `Docs/20-modules/15-model-gateway.md` §7). One
mutable row per (organization_id, route_key) -- `route_key="*"` is the
organization-wide scope (`aida.model_gateway.GLOBAL_KILL_SWITCH_SCOPE`), any
other value scopes the switch to that one route. Immutable engagement history
is carried by `audit_event`/`outbox_event` via `aida.ai_governance_api`'s
`engage_kill_switch` / `release_kill_switch` endpoints, not by this table.

Revision ID: d09d6e42028d
Revises: 4f730e96ee9b
Create Date: 2026-08-31 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d09d6e42028d"
down_revision: str | Sequence[str] | None = "4f730e96ee9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kill_switch_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("route_key", sa.String(length=100), nullable=False),
        sa.Column("engaged", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=True),
        sa.Column("engaged_by", sa.String(length=255), nullable=True),
        sa.Column("engaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.String(length=255), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_kill_switch_state_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kill_switch_state")),
        sa.UniqueConstraint(
            "organization_id",
            "route_key",
            name=op.f("uq_kill_switch_state_organization_id_route_key"),
        ),
    )
    op.create_index(
        op.f("ix_kill_switch_state_organization_id"),
        "kill_switch_state",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_kill_switch_state_org_engaged",
        "kill_switch_state",
        ["organization_id", "engaged"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_kill_switch_state_org_engaged", table_name="kill_switch_state")
    op.drop_index(
        op.f("ix_kill_switch_state_organization_id"), table_name="kill_switch_state"
    )
    op.drop_table("kill_switch_state")
