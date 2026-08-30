"""ADR-0018: three-axis tenancy (access / classification / technical)

Additive and reversible. Nothing is dropped, and no existing column changes
meaning. `line_of_business`, `data_domain` and the tenancy columns on `project`
and `datasource` stay authoritative until the cutover completes; this migration
creates the new axes alongside them and backfills them so both can be read
during the transition (ADR-0018, "Migration", steps 1-3).

What it creates
---------------
Access axis:        isolation_boundary, workspace, workspace_membership, source_binding
Classification:     business_node, business_assignment_rule, business_assignment
Policy:             access_policy

What it backfills
-----------------
* One ACTIVE `workspace` per existing `project`, keeping the project's slug so
  URLs and operator muscle memory survive.
* One `business_node` per `line_of_business` (kind=LOB) and per `data_domain`
  (kind=DOMAIN / SUB_DOMAIN), preserving the domain parent chain and pointing
  back at the row each was generated from via legacy_lob_id / legacy_domain_id.
  Codes are namespaced (`LOB:x`, `DOM:lob:domain`) because business_node.code is
  unique per organization while data_domain.code was only unique per LOB.
* `business_assignment` rows (kind=MIGRATED) attaching every project and
  datasource to its LOB node and its domain node.
* One ACTIVE `source_binding` per datasource, bound to the workspace generated
  from its project and grandfathered as approved -- existing access must not
  break at the moment the binding model is introduced.
* Default `access_policy` rows that reproduce today's RBAC outcomes exactly, so
  behaviour on the day of this migration is identical. The one policy that would
  *change* behaviour -- denying agents access to sensitive classifications -- is
  seeded as DRAFT so it is visible and reviewable but inert.

Revision ID: f1a2b3c4d5e6
Revises: e6d5b8c6bcef
Create Date: 2026-08-30 12:00:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e6d5b8c6bcef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TS = sa.DateTime(timezone=True)


def _timestamps() -> tuple[sa.Column[datetime], sa.Column[datetime]]:
    return (
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        "isolation_boundary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False, server_default="STRICT"),
        sa.Column("description", sa.String(1000), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "code"),
    )
    op.create_index(
        "ix_isolation_boundary_organization_id", "isolation_boundary", ["organization_id"]
    )

    op.create_table(
        "workspace",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("isolation_boundary_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("purpose", sa.String(1000), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("monthly_cost_ceiling", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["isolation_boundary_id"], ["isolation_boundary.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("organization_id", "slug"),
    )
    op.create_index("ix_workspace_organization_id", "workspace", ["organization_id"])
    op.create_index("ix_workspace_isolation_boundary_id", "workspace", ["isolation_boundary_id"])

    op.create_table(
        "workspace_membership",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(255), nullable=False),
        sa.Column("principal_kind", sa.String(20), nullable=False, server_default="HUMAN"),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("granted_by", sa.String(255), nullable=False),
        sa.Column("expires_at", _TS, nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("workspace_id", "principal_id"),
    )
    op.create_index(
        "ix_workspace_membership_organization_id", "workspace_membership", ["organization_id"]
    )
    op.create_index(
        "ix_workspace_membership_workspace_id", "workspace_membership", ["workspace_id"]
    )

    op.create_table(
        "source_binding",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("schema_scope", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("permitted_classifications", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("masking_profile", sa.String(50), nullable=False, server_default="DEFAULT"),
        sa.Column("purpose", sa.String(500), nullable=False),
        sa.Column("max_query_cost", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING_APPROVAL"),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("approved_at", _TS, nullable=True),
        sa.Column("expires_at", _TS, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("workspace_id", "datasource_id"),
    )
    op.create_index("ix_source_binding_organization_id", "source_binding", ["organization_id"])
    op.create_index("ix_source_binding_workspace_id", "source_binding", ["workspace_id"])
    op.create_index("ix_source_binding_datasource_id", "source_binding", ["datasource_id"])
    op.create_index(
        "ix_source_binding_org_status", "source_binding", ["organization_id", "status"]
    )

    op.create_table(
        "business_node",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False, server_default=""),
        sa.Column("owner_principal", sa.String(255), nullable=True),
        sa.Column("origin", sa.String(20), nullable=False, server_default="MANUAL"),
        sa.Column("legacy_lob_id", sa.Uuid(), nullable=True),
        sa.Column("legacy_domain_id", sa.Uuid(), nullable=True),
        sa.Column("effective_from", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("effective_to", _TS, nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_id"], ["business_node.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["legacy_lob_id"], ["line_of_business.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["legacy_domain_id"], ["data_domain.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("organization_id", "code"),
    )
    op.create_index("ix_business_node_organization_id", "business_node", ["organization_id"])
    op.create_index("ix_business_node_org_kind", "business_node", ["organization_id", "kind"])
    op.create_index("ix_business_node_parent", "business_node", ["parent_id"])

    op.create_table(
        "business_assignment_rule",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("business_node_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("match", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("auto_confirm", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.String(255), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["business_node_id"], ["business_node.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "code"),
    )
    op.create_index(
        "ix_business_assignment_rule_organization_id",
        "business_assignment_rule",
        ["organization_id"],
    )
    op.create_index(
        "ix_business_assignment_rule_business_node_id",
        "business_assignment_rule",
        ["business_node_id"],
    )

    op.create_table(
        "business_assignment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("business_node_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(120), nullable=False),
        sa.Column("assignment_kind", sa.String(20), nullable=False, server_default="MANUAL"),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("assigned_by", sa.String(255), nullable=False),
        sa.Column("confirmed_by", sa.String(255), nullable=True),
        sa.Column("effective_from", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("effective_to", _TS, nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["business_node_id"], ["business_node.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["business_assignment_rule.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "business_node_id",
            "target_type",
            "target_id",
            "effective_from",
            name="uq_business_assignment_node_target_from",
        ),
    )
    op.create_index(
        "ix_business_assignment_organization_id", "business_assignment", ["organization_id"]
    )
    op.create_index(
        "ix_business_assignment_target",
        "business_assignment",
        ["organization_id", "target_type", "target_id"],
    )
    op.create_index("ix_business_assignment_node", "business_assignment", ["business_node_id"])

    op.create_table(
        "access_policy",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False, server_default=""),
        sa.Column("effect", sa.String(20), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("subject_match", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("resource_match", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("action_match", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("transform", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("condition", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("origin", sa.String(20), nullable=False, server_default="MANUAL"),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.String(255), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "code", "version"),
    )
    op.create_index("ix_access_policy_organization_id", "access_policy", ["organization_id"])
    op.create_index("ix_access_policy_org_status", "access_policy", ["organization_id", "status"])

    _backfill()


def _backfill() -> None:
    """Populate the new axes from the existing tenancy tables.

    Written with explicit SQL rather than the ORM so the migration keeps working
    if the models move on (ADR-0015's usual practice in this repo).
    """
    bind = op.get_bind()
    now = datetime.now(UTC)
    actor = "migration:f1a2b3c4d5e6"

    lobs = bind.execute(
        sa.text("SELECT id, organization_id, name, code FROM line_of_business")
    ).mappings().all()
    domains = bind.execute(
        sa.text(
            "SELECT id, organization_id, line_of_business_id, parent_domain_id, name, code, "
            "is_default FROM data_domain"
        )
    ).mappings().all()
    projects = bind.execute(
        sa.text(
            "SELECT id, organization_id, line_of_business_id, data_domain_id, name, slug, status "
            "FROM project"
        )
    ).mappings().all()
    datasources = bind.execute(
        sa.text(
            "SELECT id, organization_id, line_of_business_id, data_domain_id, project_id, name "
            "FROM datasource"
        )
    ).mappings().all()

    node_insert = sa.text(
        "INSERT INTO business_node (id, organization_id, parent_id, kind, name, code, "
        "description, origin, legacy_lob_id, legacy_domain_id, effective_from, status, "
        "created_at, updated_at) VALUES (:id, :org, :parent, :kind, :name, :code, :desc, "
        "'MIGRATED', :lob, :domain, :now, 'ACTIVE', :now, :now)"
    )
    assignment_insert = sa.text(
        "INSERT INTO business_assignment (id, organization_id, business_node_id, target_type, "
        "target_id, assignment_kind, assigned_by, effective_from, status, created_at, updated_at) "
        "VALUES (:id, :org, :node, :ttype, :tid, 'MIGRATED', :actor, :now, 'ACTIVE', :now, :now)"
    )

    used_codes: set[tuple[UUID, str]] = set()

    def unique_code(org_id: UUID, candidate: str) -> str:
        code = candidate[:80]
        suffix = 1
        while (org_id, code) in used_codes:
            suffix += 1
            tail = f"~{suffix}"
            code = candidate[: 80 - len(tail)] + tail
        used_codes.add((org_id, code))
        return code

    # --- LOB nodes ---
    lob_node: dict[UUID, UUID] = {}
    lob_code: dict[UUID, str] = {}
    for lob in lobs:
        node_id = uuid4()
        lob_node[lob["id"]] = node_id
        lob_code[lob["id"]] = lob["code"]
        bind.execute(
            node_insert,
            {
                "id": node_id,
                "org": lob["organization_id"],
                "parent": None,
                "kind": "LOB",
                "name": lob["name"],
                "code": unique_code(lob["organization_id"], f"LOB:{lob['code']}"),
                "desc": "Generated from line_of_business by ADR-0018 migration.",
                "lob": lob["id"],
                "domain": None,
                "now": now,
            },
        )

    # --- Domain nodes, parents resolved after every node exists so that a
    # sub-domain whose parent appears later in the result set still links up. ---
    domain_node: dict[UUID, UUID] = {}
    for domain in domains:
        node_id = uuid4()
        domain_node[domain["id"]] = node_id
        parent_lob_code = lob_code.get(domain["line_of_business_id"], "LOB")
        bind.execute(
            node_insert,
            {
                "id": node_id,
                "org": domain["organization_id"],
                "parent": None,
                "kind": "SUB_DOMAIN" if domain["parent_domain_id"] else "DOMAIN",
                "name": domain["name"],
                "code": unique_code(
                    domain["organization_id"],
                    f"DOM:{parent_lob_code[:24]}:{domain['code'][:48]}",
                ),
                "desc": "Generated from data_domain by ADR-0018 migration.",
                "lob": None,
                "domain": domain["id"],
                "now": now,
            },
        )
    for domain in domains:
        parent_id = (
            domain_node.get(domain["parent_domain_id"])
            if domain["parent_domain_id"]
            else lob_node.get(domain["line_of_business_id"])
        )
        if parent_id is not None:
            bind.execute(
                sa.text("UPDATE business_node SET parent_id = :parent WHERE id = :id"),
                {"parent": parent_id, "id": domain_node[domain["id"]]},
            )

    def assign(org_id: UUID, node_id: UUID | None, target_type: str, target_id: UUID) -> None:
        if node_id is None:
            return
        bind.execute(
            assignment_insert,
            {
                "id": uuid4(),
                "org": org_id,
                "node": node_id,
                "ttype": target_type,
                "tid": str(target_id),
                "actor": actor,
                "now": now,
            },
        )

    # --- Workspaces (one per project) + assignments ---
    project_workspace: dict[UUID, UUID] = {}
    for project in projects:
        workspace_id = uuid4()
        project_workspace[project["id"]] = workspace_id
        bind.execute(
            sa.text(
                "INSERT INTO workspace (id, organization_id, isolation_boundary_id, name, slug, "
                "purpose, status, created_at, updated_at) VALUES (:id, :org, NULL, :name, :slug, "
                ":purpose, :status, :now, :now)"
            ),
            {
                "id": workspace_id,
                "org": project["organization_id"],
                "name": project["name"],
                "slug": project["slug"],
                "purpose": "Generated from project by the ADR-0018 migration.",
                "status": project["status"],
                "now": now,
            },
        )
        org_id = project["organization_id"]
        assign(org_id, lob_node.get(project["line_of_business_id"]), "PROJECT", project["id"])
        assign(org_id, domain_node.get(project["data_domain_id"]), "PROJECT", project["id"])

    # --- Source bindings, grandfathered as already approved ---
    for datasource in datasources:
        workspace_id = project_workspace.get(datasource["project_id"])
        if workspace_id is None:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO source_binding (id, organization_id, workspace_id, datasource_id, "
                "schema_scope, permitted_classifications, masking_profile, purpose, status, "
                "requested_by, approved_by, approved_at, created_at, updated_at) VALUES "
                "(:id, :org, :ws, :ds, '[]', '[]', 'DEFAULT', :purpose, 'ACTIVE', :actor, "
                ":actor, :now, :now, :now)"
            ),
            {
                "id": uuid4(),
                "org": datasource["organization_id"],
                "ws": workspace_id,
                "ds": datasource["id"],
                "purpose": (
                    "Grandfathered by the ADR-0018 migration from the existing project link."
                ),
                "actor": actor,
                "now": now,
            },
        )
        assign(
            datasource["organization_id"],
            lob_node.get(datasource["line_of_business_id"]),
            "DATASOURCE",
            datasource["id"],
        )
        assign(
            datasource["organization_id"],
            domain_node.get(datasource["data_domain_id"]),
            "DATASOURCE",
            datasource["id"],
        )

    _seed_policies(bind, now, actor)


def _seed_policies(bind: sa.Connection, now: datetime, actor: str) -> None:
    """Seed one policy set per organization that reproduces today's RBAC exactly.

    ADR-0018 requires behaviour to be identical on the day of the migration, so
    every seeded ACTIVE policy is an ALLOW that mirrors an existing role check.
    The single policy that would change behaviour -- denying agents sensitive
    data -- is seeded DRAFT: visible, reviewable, and inert until someone
    deliberately activates it.
    """
    orgs = bind.execute(sa.text("SELECT id FROM organization")).mappings().all()
    policy_insert = sa.text(
        "INSERT INTO access_policy (id, organization_id, code, version, name, description, "
        "effect, priority, subject_match, resource_match, action_match, transform, condition, "
        "origin, status, created_by, created_at, updated_at) VALUES (:id, :org, :code, 1, :name, "
        ":desc, :effect, :priority, :subject, '{}', :actions, '{}', '{}', 'SEEDED', :status, "
        ":actor, :now, :now)"
    )
    readers = (
        '{"roles": ["Viewer", "Analyst", "Steward", "Reviewer", "DataAdmin", '
        '"OrganizationAdmin", "PlatformAdmin"]}'
    )
    operators = (
        '{"roles": ["Analyst", "Steward", "Reviewer", "DataAdmin", '
        '"OrganizationAdmin", "PlatformAdmin"]}'
    )
    proposers = (
        '{"roles": ["Analyst", "Steward", "DataAdmin", "OrganizationAdmin", "PlatformAdmin"]}'
    )
    approvers = '{"roles": ["Reviewer", "OrganizationAdmin", "PlatformAdmin"]}'
    seeds = [
        ("rbac-read-metadata", "RBAC parity: read metadata", "ALLOW", 100, readers,
         '["READ_METADATA"]', "ACTIVE",
         "Mirrors the role check that governs catalog and metadata reads today."),
        ("rbac-read-data", "RBAC parity: read data", "ALLOW", 100, operators,
         '["READ_DATA", "EXECUTE_TOOL", "CONSUME_CONTEXT"]', "ACTIVE",
         "Mirrors the role check that governs governed query and tool execution today."),
        ("rbac-propose", "RBAC parity: propose", "ALLOW", 100, proposers,
         '["PROPOSE"]', "ACTIVE",
         "Mirrors the role check that governs proposing governed changes today."),
        ("rbac-approve", "RBAC parity: approve", "ALLOW", 100, approvers,
         '["APPROVE"]', "ACTIVE",
         "Mirrors the role check that governs approvals today. Maker != checker "
         "(INV-8) is enforced separately and is not a policy decision."),
        ("agent-denied-sensitive-data", "Agents may not read sensitive classifications", "DENY",
         1000, '{"principal_kind": "AGENT"}', '["READ_DATA"]', "DRAFT",
         "Seeded DRAFT, deliberately inert. Activating it makes 'humans may see full account "
         "numbers, agents never do' a single enforced policy -- the control most often asked "
         "for once agents reach production. Left inactive here because ADR-0018 requires "
         "migration day to change no behaviour."),
    ]
    for org in orgs:
        for code, name, effect, priority, subject, actions, status, desc in seeds:
            params: dict[str, object] = {
                "id": uuid4(),
                "org": org["id"],
                "code": code,
                "name": name,
                "desc": desc,
                "effect": effect,
                "priority": priority,
                "subject": subject,
                "actions": actions,
                "status": status,
                "actor": actor,
                "now": now,
            }
            bind.execute(policy_insert, params)
            if code == "agent-denied-sensitive-data":
                bind.execute(
                    sa.text(
                        "UPDATE access_policy SET resource_match = :rm "
                        "WHERE organization_id = :org AND code = :code"
                    ),
                    {
                        "rm": (
                            '{"classifications": '
                            '["PII", "PHI", "PCI", "SECRET", "CONFIDENTIAL"]}'
                        ),
                        "org": org["id"],
                        "code": code,
                    },
                )


def downgrade() -> None:
    op.drop_table("access_policy")
    op.drop_table("business_assignment")
    op.drop_table("business_assignment_rule")
    op.drop_table("business_node")
    op.drop_table("source_binding")
    op.drop_table("workspace_membership")
    op.drop_table("workspace")
    op.drop_table("isolation_boundary")
