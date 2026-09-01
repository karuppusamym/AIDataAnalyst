"""identity tenancy -- PRIVATE. SQLAlchemy models in this module's own schema
(`identity`, per `Docs/10-architecture/04-module-decomposition.md` Sec.6).

Not importable from outside this module once the `module-privacy`
contract (tracker ST-02) is enforced.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.models`, which now re-exports these classes for backward
compatibility -- every existing `from aida.models import X` caller keeps
working unchanged. This is a Python-source-location move only: these
classes still declare no `schema=` in `__table_args__` and still live in
the single shared PostgreSQL schema. The actual database schema migration
(refactor plan Sec.5 steps 2.3/2.4) is explicitly deferred to a later,
separate pass.

Owned tables (per Sec.4's register: "organizations, legal entities, LOBs,
projects, principals, role mappings, secret references", read together
with ADR-0018's three-axis tenancy model, which this module's docstrings
below identify as squarely part of this domain):

* `Organization`, `OrganizationIntegrationPolicy` -- tenancy root.
* `LineOfBusiness`, `DataDomain`, `CrossBoundaryGrant` -- pre-ADR-0018 and
  ADR-0017 governance-domain hierarchy, still authoritative until the
  ADR-0018 cutover completes.
* `IsolationBoundary`, `Workspace`, `WorkspaceMembership`,
  `WorkspaceAccessRule`, `AuthorizationShadowRecord`, `SourceBinding` --
  ADR-0018 axis 1 (access: organization -> workspace) and its supporting
  grant/audit records.
* `BusinessNode`, `BusinessAssignment`, `BusinessAssignmentRule`,
  `BusinessNodeClosure`, `BusinessNodeRollup` -- ADR-0018 axis 2
  (classification).
* `Project` -- scoped inside a line of business / data domain.
* `Delegation` -- PG-4 time-bounded delegation of governance-review
  approval authority between principals.
* `RevokedToken` -- durable bearer-token revocation record (authn, per
  Sec.9's `security.py`/`oidc.py` -> "01 identity-tenancy" split).

Explicitly NOT moved here despite living in the same neighborhood in the
old `aida.models`: `AccessPolicy` (policy-governance, module 17 -- it is a
*policy*, not a tenancy structure) and `Embedding` (retrieval, module 12 --
generic vector storage, no tenancy semantics of its own). Both remain in
`aida.models` pending their own modules' extraction passes.

`TimestampMixin` and `utc_now` are imported from `atlas.platform.db`, not
defined here -- they are shared infrastructure used by classes across many
modules (e.g. `aida.envelope_models`), not identity-tenancy-owned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aida.integration_catalog import default_transformation_metadata_integrations
from atlas.platform.db import Base, TimestampMixin, utc_now


class Organization(Base, TimestampMixin):
    __tablename__ = "organization"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class OrganizationIntegrationPolicy(Base, TimestampMixin):
    __tablename__ = "organization_integration_policy"
    __table_args__ = (UniqueConstraint("organization_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transformation_metadata_integrations: Mapped[dict[str, bool]] = mapped_column(
        JSON,
        default=default_transformation_metadata_integrations,
        nullable=False,
    )


class LineOfBusiness(Base, TimestampMixin):
    __tablename__ = "line_of_business"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class DataDomain(Base, TimestampMixin):
    """Governance boundary between line_of_business and project (ADR-0017).

    A steward-owned scope: relationship inference and graph traversal cross
    project/datasource boundaries freely within one domain, and only cross a
    domain boundary through an explicit, audited cross_boundary_grant.
    `parent_domain_id` allows sub-domains to arbitrary depth. Every LOB gets a
    lazily-created `is_default` "Ungoverned" domain so a newly connected
    project or datasource is never left unscoped (see domain_service.py).
    """

    __tablename__ = "data_domain"
    __table_args__ = (UniqueConstraint("line_of_business_id", "code"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    line_of_business_id: Mapped[UUID] = mapped_column(
        ForeignKey("line_of_business.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    parent_domain_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_domain.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class CrossBoundaryGrant(Base, TimestampMixin):
    """Explicit, audited permission to traverse across a data_domain boundary (ADR-0017 SS4).

    Graph traversal and relationship inference never cross a domain boundary on
    their own (INV-5: deny-by-default, never inherited) -- a `target_data_domain`
    only sees into a `source_data_domain` while an ACTIVE grant naming that pair
    exists. Approval flows through the same maker-checker GovernanceReview queue
    every other governed object in this platform uses
    (object_type="CROSS_BOUNDARY_GRANT", see semantic_api.decide_governance_review),
    so a grant starts PENDING_APPROVAL and only becomes ACTIVE once a second
    principal approves it. `edge_kinds` scopes the grant to specific relationship
    kinds (e.g. ["FOREIGN_KEY_INFERRED"]); an empty list grants all kinds. A
    withheld edge at traversal time must be reported as `withheld:"no_grant"`,
    never silently dropped.
    """

    __tablename__ = "cross_boundary_grant"
    __table_args__ = (
        Index(
            "ix_cross_boundary_grant_org_pair",
            "organization_id",
            "source_data_domain_id",
            "target_data_domain_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_data_domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_domain.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    target_data_domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_domain.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    edge_kinds: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_APPROVAL", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- ADR-0018: three-axis tenancy -------------------------------------------
#
# Axis 1 (access): organization -> workspace. The ONLY axis with permission
#   semantics. Short and stable, because a reorganisation must not be a data
#   migration across every governed row.
# Axis 2 (classification): business_node / business_assignment. Versioned,
#   many-to-many, effective-dated. Grants nothing; policy keys on it.
# Axis 3 (technical): datasource -> catalog -> schema -> table -> column.
#
# LineOfBusiness and DataDomain above are the pre-ADR-0018 tenancy levels. They
# remain authoritative until the cutover completes; BusinessNode rows mirroring
# them are created by migration f1a2b3c4d5e6 so both can be read during the
# transition. See Docs/10-architecture/adr/ADR-0018-*.md for the migration steps.


class IsolationBoundary(Base, TimestampMixin):
    """A hard wall that no grant can cross (ADR-0018).

    The escape hatch for genuine Chinese walls -- an advisory desk that must not
    see a trading desk. Deliberately rare and explicit: a bank has a handful, not
    one per line of business, because everything softer is better expressed as an
    access policy. `mode="STRICT"` admits no cross-boundary grant by any
    mechanism, including administrator action; `ADVISORY` records the boundary
    for reporting but lets an approved grant cross it.
    """

    __tablename__ = "isolation_boundary"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="STRICT", nullable=False)
    description: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class Workspace(Base, TimestampMixin):
    """The unit of grant, membership, budget and blast radius (ADR-0018).

    Replaces the LOB/domain segment of the old tenancy path. A workspace owns its
    membership list, the source bindings that decide which datasources it may
    reach and how, and the projects that scope analysis inside it. Tenancy scope
    on governed records becomes `(organization_id, workspace_id)`.

    `isolation_boundary_id` is normally NULL -- most workspaces need no hard wall.
    """

    __tablename__ = "workspace"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    isolation_boundary_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("isolation_boundary.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    monthly_cost_ceiling: Mapped[int | None] = mapped_column(BigInteger)
    # SHADOW | ENFORCE. New and migrated workspaces start in SHADOW, where the
    # attribute-based decision is computed and recorded but never denies. Introducing an
    # authorization system in enforcing mode is how you find out, in production, that it
    # denies something it should not. Flip per workspace once the shadow record shows a
    # week of agreement with what actually happened.
    authorization_mode: Mapped[str] = mapped_column(
        String(20), default="SHADOW", nullable=False
    )


class WorkspaceMembership(Base, TimestampMixin):
    """A principal's role inside one workspace (ADR-0018).

    Roles are additive across memberships; a DENY from policy always wins over a
    grant from a role. Maker != checker (INV-8) holds regardless of role: a
    `workspace_owner` who proposes a change still cannot approve it.
    """

    __tablename__ = "workspace_membership"
    __table_args__ = (UniqueConstraint("workspace_id", "principal_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_kind: Mapped[str] = mapped_column(String(20), default="HUMAN", nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class WorkspaceAccessRule(Base, TimestampMixin):
    """Derives workspace membership from the identity provider's roles.

    Exists because of a problem the ADR-0018 migration created and the rehearsal did not
    catch: it backfills one workspace per project and **zero memberships**, because there
    is nothing to backfill them *from*. There is no persisted principal table anywhere in
    this codebase -- identity and roles arrive as OIDC claims per request and are never
    stored -- so no record exists of who used which project. Wiring `authorize` into a
    read path against 24 memberless workspaces would deny every request in the platform.

    Seeding 24 synthetic owners would be worse: it invents an access grant nobody made.
    Instead a rule maps an IdP role onto a workspace role, which is the same principle the
    design already states for personas -- derived from group claims, never chosen in a UI.
    One rule can cover every migrated workspace, and revoking it revokes the access.

    Scope, narrowest wins: a rule bound to `workspace_id` applies to that workspace; one
    bound to `business_node_id` applies to every workspace whose classification sits at or
    below that node; one bound to neither applies org-wide and should be rare.

    Rules grant. They never deny -- a DENY policy still outranks anything derived here,
    and an explicit `workspace_membership` row is evaluated alongside, not instead.
    """

    __tablename__ = "workspace_access_rule"
    __table_args__ = (
        UniqueConstraint("organization_id", "code"),
        Index("ix_workspace_access_rule_workspace", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), nullable=True
    )
    business_node_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("business_node.id", ondelete="CASCADE"), nullable=True
    )
    # The role as it arrives from the identity provider, after oidc_role_mappings.
    subject_role: Mapped[str] = mapped_column(String(80), nullable=False)
    workspace_role: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthorizationShadowRecord(Base):
    """What the attribute-based decision *would* have been, while it is not enforcing.

    The evidence that makes flipping a workspace to ENFORCE a measurement rather than a
    leap. One row per divergence -- agreements are counted, not stored, because storing
    every allowed read would be a second access log at request volume for no information.

    Value-free (INV-6): reason codes, identifiers and counts. No resource values, no
    question text, and no policy expression.
    """

    __tablename__ = "authorization_shadow_record"
    __table_args__ = (
        Index("ix_auth_shadow_workspace_time", "workspace_id", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(120))
    # The decision the new engine reached, and the reason it gave.
    shadow_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    matched_policy_code: Mapped[str | None] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SourceBinding(Base, TimestampMixin):
    """A workspace's scoped, expiring permission to reach one datasource (ADR-0018).

    The same warehouse serves many workspaces, and two workspaces on one source
    can legitimately see different things -- this is where that is expressed and
    audited. Approval routes to the *source owner*, not a central queue, because
    central queues are where these requests die.

    Bindings expire. That is the mechanism that stops entitlement creep, and it is
    the thing almost every platform omits.
    """

    __tablename__ = "source_binding"
    __table_args__ = (
        UniqueConstraint("workspace_id", "datasource_id"),
        Index("ix_source_binding_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Empty list = every schema in the datasource; otherwise an allowlist.
    schema_scope: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Classifications this workspace may see through this binding. Empty = the
    # organization default policy decides; an explicit list narrows it further.
    permitted_classifications: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    masking_profile: Mapped[str] = mapped_column(String(50), default="DEFAULT", nullable=False)
    purpose: Mapped[str] = mapped_column(String(500), nullable=False)
    max_query_cost: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_APPROVAL", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BusinessNode(Base, TimestampMixin):
    """A node in the classification tree: LOB, sub-LOB, domain, sub-domain, concept.

    Axis 2 of ADR-0018. Grants nothing on its own -- access policies key on it.
    Self-referencing to arbitrary depth, effective-dated so that a reorganisation
    is an update to the tree plus new assignments rather than a migration, and so
    that last quarter's audit record still resolves against last quarter's tree.
    """

    __tablename__ = "business_node"
    __table_args__ = (
        UniqueConstraint("organization_id", "code"),
        Index("ix_business_node_org_kind", "organization_id", "kind"),
        Index("ix_business_node_parent", "parent_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("business_node.id", ondelete="RESTRICT"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    owner_principal: Mapped[str | None] = mapped_column(String(255))
    # Provenance of the node itself, so a migrated LOB is distinguishable from one
    # a steward authored. MIGRATED rows were generated from the pre-ADR-0018
    # line_of_business / data_domain tables.
    origin: Mapped[str] = mapped_column(String(20), default="MANUAL", nullable=False)
    legacy_lob_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("line_of_business.id", ondelete="SET NULL"), nullable=True
    )
    legacy_domain_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_domain.id", ondelete="SET NULL"), nullable=True
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class BusinessAssignment(Base, TimestampMixin):
    """Many-to-many attachment of a business_node to any governed object (ADR-0018).

    `target_type` / `target_id` is a deliberate polymorphic reference rather than a
    foreign key: assignments reach tables, columns, views, metrics, glossary terms,
    data products and knowledge pages, which live in different schemas, and
    ADR-0015 forbids cross-schema foreign keys. Referential integrity is eventual,
    reconciled by the same mechanism as every other cross-module reference.

    An asset can carry several assignments. That is the point: a `customer` table
    belongs to both Retail Banking and Financial Crime, which the pre-ADR-0018
    containment hierarchy could not express.
    """

    __tablename__ = "business_assignment"
    __table_args__ = (
        UniqueConstraint(
            "business_node_id", "target_type", "target_id", "effective_from",
            name="uq_business_assignment_node_target_from",
        ),
        Index("ix_business_assignment_target", "organization_id", "target_type", "target_id"),
        Index("ix_business_assignment_node", "business_node_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_node_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_node.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # MANUAL: a steward said so. RULE: produced by an assignment rule.
    # INFERRED: proposed by analysis, never authoritative until confirmed.
    # MIGRATED: generated from the pre-ADR-0018 tenancy columns.
    assignment_kind: Mapped[str] = mapped_column(String(20), default="MANUAL", nullable=False)
    rule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("business_assignment_rule.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    assigned_by: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class BusinessAssignmentRule(Base, TimestampMixin):
    """A governed rule that proposes assignments (`schema LIKE 'rtl_%' -> Retail Banking`).

    Re-evaluated on catalog drift. Produces *proposals*, never silent
    reassignment -- a rule that quietly moved assets between domains would make
    the classification tree untrustworthy exactly when it matters.
    """

    __tablename__ = "business_assignment_rule"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_node_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_node.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Deterministic match spec, e.g. {"schema_like": "rtl_%", "datasource_id": "..."}.
    # Never a free-form expression -- see the tool-parameter reasoning in module 14.
    match: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    auto_confirm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class BusinessNodeClosure(Base):
    """Precomputed ancestor/descendant pairs for the classification tree (ADR-0018).

    Measured, not assumed. Against 13,548 nodes and 5M assignments on PostgreSQL 16,
    recursive-CTE descendant traversal ran ~3.3 ms and the closure join ~1.5 ms; the
    bigger win is on aggregation, where a subtree roll-up went from ~3.1 s to ~0.9 s.
    Neither number is the reason roll-up is fast now -- see `BusinessNodeRollup` --
    but the closure is what makes that materialisation a single grouped join instead
    of one recursive query per node.

    **This table reflects the tree as it stands now.** It carries no effective dates,
    because a closure that encoded history would need a row per (ancestor, descendant,
    interval) and would grow without bound on a tree that is re-parented. Historical
    `as_of` queries therefore fall back to the recursive CTE, which is correct and
    slower -- the right trade, because history queries are rare and traversal is hot.

    52,044 rows for 13,548 nodes at depth 4: roughly 4x the node count, which is what
    a shallow taxonomy costs. A deep tree would cost more, and the depth cap is the
    thing to watch if the taxonomy ever grows past a handful of levels.
    """

    __tablename__ = "business_node_closure"
    __table_args__ = (
        Index("ix_business_node_closure_descendant", "descendant_id"),
        Index("ix_business_node_closure_ancestor", "ancestor_id"),
    )

    ancestor_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_node.id", ondelete="CASCADE"), primary_key=True
    )
    descendant_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_node.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False)


class BusinessNodeRollup(Base):
    """Materialised "how much sits under this node", refreshed rather than computed.

    Roll-up is the one query in the classification axis that does not scale as a read.
    Measured on PostgreSQL 16 with 13,548 nodes and 5,000,000 assignments:

    | Approach                              | p50      |
    |---------------------------------------|----------|
    | recursive CTE + count(DISTINCT)       | 3,147 ms |
    | closure join + count(DISTINCT)        |   915 ms |
    | **read this table**                   | **0.4 ms** |

    A full recompute of every node takes ~47 s as one grouped statement, which is a
    batch job, not a request. So roll-up is computed on write-ish cadence and read as
    a lookup.

    `computed_at` is part of the contract, not bookkeeping: a coverage number that
    silently drifts is worse than one labelled three hours old. The API returns it so
    staleness is visible rather than hidden -- the same rule the knowledge layer uses
    for compiled pages.

    Exact counts, not approximate: `count(DISTINCT)` is preserved because an asset
    assigned to two sibling domains must not be double-counted, and the obvious
    approximation (HyperLogLog) needs a PostgreSQL extension that a bank will not
    always grant.
    """

    __tablename__ = "business_node_rollup"

    business_node_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_node.id", ondelete="CASCADE"), primary_key=True
    )
    target_type: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    distinct_targets: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Project(Base, TimestampMixin):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    line_of_business_id: Mapped[UUID] = mapped_column(
        ForeignKey("line_of_business.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    data_domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_domain.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)


class Delegation(Base, TimestampMixin):
    """PG-4: time-bounded, audited delegation of governance-review approval
    authority from one principal (the delegator) to another (the delegate) --
    e.g. a steward or reviewer going on leave delegates their decision
    authority to a covering colleague for a bounded window.

    ``delegated_roles`` must be a subset of the roles the delegator actually
    asserted at grant time (``aida.delegation.validate_delegated_roles`` --
    enforced by ``delegation_api.grant_delegation``, not by a database
    constraint, since role membership itself is claims-based, not a stored
    directory) -- a principal can only hand off authority it actually holds,
    never more. Active only within ``[starts_at, expires_at)`` and while
    ``status == "ACTIVE"``; ``aida.delegation.is_delegation_active`` is the
    single query-time projection every enforcement call site uses, mirroring
    ``aida.asset_certification.asset_certification_is_active``'s
    supersede-by-projection pattern -- a delegation past its ``expires_at``
    keeps its row as audited history, it simply stops being honored (never
    deleted, never silently extended).
    """

    __tablename__ = "delegation"
    __table_args__ = (
        Index("ix_delegation_org_delegate", "organization_id", "delegate_principal_id"),
        Index("ix_delegation_org_delegator", "organization_id", "delegator_principal_id"),
        CheckConstraint("expires_at > starts_at", name="ck_delegation_window_ordered"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    delegator_principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    delegate_principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    delegated_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(String(255))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RevokedToken(Base, TimestampMixin):
    """Durable revocation record for a bearer token.

    Keyed by `token_identifier` (see `aida.oidc.token_identifier`) -- the token's own
    `jti` claim when the issuer sets one, else a deterministic fingerprint of
    (subject, issued-at, expiry) so tokens from issuers that omit `jti` can still be
    revoked individually. `token_expires_at` mirrors the *token's* own expiry, not
    this record's: it bounds how long the revocation list must be kept, since a
    token can never be replayed past its own `exp` regardless of this table's
    contents (see `aida.token_revocation.prune_expired_revocations`).
    """

    __tablename__ = "revoked_token"
    __table_args__ = (
        UniqueConstraint("token_identifier"),
        Index("ix_revoked_token_expires_at", "token_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), index=True
    )
    token_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    revoked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
