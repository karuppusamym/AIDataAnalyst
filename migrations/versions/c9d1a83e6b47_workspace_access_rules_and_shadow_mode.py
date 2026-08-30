"""Workspace access rules and shadow-mode authorization (ADR-0018 rollout safety)

Fixes a defect the ADR-0018 migration created and its own rehearsal did not catch.

That migration backfills one workspace per project and **zero memberships**, because
there is nothing to backfill them from: this codebase has no persisted principal table at
all -- identity and roles arrive as OIDC claims per request and are never stored -- so no
record exists of who used which project. The rehearsal asserted 14 things about the
backfill and none of them was "can anyone actually get in".

The consequence: wiring `authorize` into a read path would have returned
`NO_WORKSPACE_MEMBERSHIP` for every request in the platform. A security improvement that
is indistinguishable from an outage.

Two mechanisms, both additive:

* `workspace_access_rule` derives workspace membership from an identity-provider role,
  scoped to a workspace, to everything under a business node, or org-wide. One rule
  covers every migrated workspace without inventing an access grant nobody made, and
  revoking the rule revokes the access. Seeding synthetic owners would have been worse.
* `workspace.authorization_mode` plus `authorization_shadow_record` let a workspace
  compute the full decision, record what it *would* have denied, and enforce nothing.
  Flipping to ENFORCE then becomes a measurement rather than a leap.

Every existing workspace is created in SHADOW, and a single ACTIVE rule per organization
grants the pre-existing steward/analyst roles their equivalent workspace roles -- so
behaviour on the day of this migration is, once again, unchanged.

Revision ID: c9d1a83e6b47
Revises: b4e2f70a9c15

This revision and a1c9f4b7e230 (envelope v1.1) were authored concurrently from the same
parent, producing two heads -- a merge accident that normally only surfaces at deploy
time, and the first real catch by the single-head CI gate added in Phase 0.

Resolving it produced a second, more interesting failure: both authors rebased onto the
other simultaneously, creating a revision *cycle*. The resolution rule that avoids this is
"whoever moved last yields", so this revision stayed on its original parent and
a1c9f4b7e230 chains after it. Worth recording because the failure is invisible to every
check except `alembic heads`, and the cycle form is invisible even to that -- it raises
rather than reporting a count.
Create Date: 2026-08-30 16:30:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "c9d1a83e6b47"
down_revision: str | None = "b4e2f70a9c15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Global role (as it arrives from the IdP) -> workspace role. Mirrors what each global
# role can already do today, so the derived membership grants nothing new.
_SEED_RULES: tuple[tuple[str, str, str], ...] = (
    ("seed-platformadmin", "PlatformAdmin", "workspace_owner"),
    ("seed-organizationadmin", "OrganizationAdmin", "workspace_owner"),
    ("seed-dataadmin", "DataAdmin", "steward"),
    ("seed-steward", "Steward", "steward"),
    ("seed-reviewer", "Reviewer", "reviewer"),
    ("seed-analyst", "Analyst", "analyst"),
    ("seed-viewer", "Viewer", "viewer"),
)


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column(
            "authorization_mode", sa.String(20), nullable=False, server_default="SHADOW"
        ),
    )

    op.create_table(
        "workspace_access_rule",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("business_node_id", sa.Uuid(), nullable=True),
        sa.Column("subject_role", sa.String(80), nullable=False),
        sa.Column("workspace_role", sa.String(40), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["business_node_id"], ["business_node.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "code"),
    )
    op.create_index(
        "ix_workspace_access_rule_organization_id", "workspace_access_rule", ["organization_id"]
    )
    op.create_index(
        "ix_workspace_access_rule_workspace", "workspace_access_rule", ["workspace_id"]
    )

    op.create_table(
        "authorization_shadow_record",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(255), nullable=False),
        sa.Column("principal_kind", sa.String(20), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("resource_type", sa.String(40), nullable=False),
        sa.Column("resource_id", sa.String(120), nullable=True),
        sa.Column("shadow_allowed", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(60), nullable=False),
        sa.Column("matched_policy_code", sa.String(80), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_authorization_shadow_record_organization_id",
        "authorization_shadow_record",
        ["organization_id"],
    )
    op.create_index(
        "ix_auth_shadow_workspace_time",
        "authorization_shadow_record",
        ["workspace_id", "observed_at"],
    )

    bind = op.get_bind()
    now = datetime.now(UTC)
    orgs = bind.execute(sa.text("SELECT id FROM organization")).mappings().all()
    for org in orgs:
        for code, subject_role, workspace_role in _SEED_RULES:
            bind.execute(
                sa.text(
                    "INSERT INTO workspace_access_rule (id, organization_id, code, "
                    "workspace_id, business_node_id, subject_role, workspace_role, status, "
                    "created_by, created_at, updated_at) VALUES (:id, :org, :code, NULL, NULL, "
                    ":subject, :ws_role, 'ACTIVE', :actor, :now, :now)"
                ),
                {
                    "id": uuid4(),
                    "org": org["id"],
                    "code": code,
                    "subject": subject_role,
                    "ws_role": workspace_role,
                    "actor": "migration:c9d1a83e6b47",
                    "now": now,
                },
            )


def downgrade() -> None:
    op.drop_table("authorization_shadow_record")
    op.drop_table("workspace_access_rule")
    op.drop_column("workspace", "authorization_mode")
