"""R11-B9: map the Auditor role onto the auditor workspace role

Revision ID: b7e3d1f9a254
Revises: e6d2b9f4a1c7
Create Date: 2026-09-14

`c9d1a83e6b47` seeded one organization-wide access rule per identity-provider
role, each onto the workspace role matching "what each global role can already
do today, so the derived membership grants nothing new". `Auditor` got no row,
because no workspace role matched it: the only one that could extract the audit
ledger, `workspace_owner`, also reads data and approves changes. So a workspace
that enforces refused an auditor `NO_WORKSPACE_MEMBERSHIP` on the audit export
their role is admitted to -- the ADR-0018 rollout would have taken away a
capability the role already had.

R11-B9 added the `auditor` workspace role (`READ_METADATA`, `EXPORT`). This
seeds the missing rule by the same principle -- `Auditor` onto `auditor`,
organization-wide -- for every organization that has no rule with this code
yet. It grants nothing the role could not already do through `require_roles`,
and nothing that reads data. Revoking the rule revokes the access.

As with `c9d1a83e6b47`, an organization created after this migration receives
no seeded rules; nothing seeds them at runtime.

`downgrade` deletes only the rows this migration inserted.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3d1f9a254"
down_revision: str | Sequence[str] | None = "e6d2b9f4a1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "seed-auditor"
_ACTOR = "migration:b7e3d1f9a254"


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    organizations = (
        bind.execute(
            sa.text(
                "SELECT id FROM organization WHERE id NOT IN "
                "(SELECT organization_id FROM workspace_access_rule WHERE code = :code)"
            ),
            {"code": _CODE},
        )
        .mappings()
        .all()
    )
    for organization in organizations:
        bind.execute(
            sa.text(
                "INSERT INTO workspace_access_rule (id, organization_id, code, "
                "workspace_id, business_node_id, subject_role, workspace_role, status, "
                "created_by, created_at, updated_at) VALUES (:id, :org, :code, NULL, NULL, "
                "'Auditor', 'auditor', 'ACTIVE', :actor, :now, :now)"
            ),
            {
                "id": uuid4(),
                "org": organization["id"],
                "code": _CODE,
                "actor": _ACTOR,
                "now": now,
            },
        )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM workspace_access_rule WHERE code = :code AND created_by = :actor"),
        {"code": _CODE, "actor": _ACTOR},
    )
