from datetime import UTC, datetime
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base
from aida.integration_catalog import default_transformation_metadata_integrations


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


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


class Embedding(Base, TimestampMixin):
    """A stored embedding, in ordinary PostgreSQL columns (ADR-0019).

    `vector` is `bytea` holding packed float32s, not a `pgvector` column, because a
    regulated PostgreSQL estate frequently forbids extensions and the platform must not
    require one to have semantic search at all. Exact cosine over a policy-narrowed
    candidate set is the default; an external in-network vector service and a future
    `pgvector` adapter sit behind the same port.

    `vector_norm` is stored rather than recomputed per comparison -- the single cheapest
    optimisation available to an exact scorer, halving the inner loop.

    `index_signature` pins (embedding model, model version, dimensions, chunking
    version). Vectors from different signatures are not comparable, and mixing them
    fails as quietly poor search rather than as an error, so the signature is matched on
    every read and a change is a rebuild trigger.

    **What may be embedded:** object names and paths, business names, descriptions,
    synonyms, glossary terms, compiled knowledge blocks, and sections of the customer's
    own uploaded documentation. **Never** source business values (INV-6). This matters
    more than it looks: embedding-inversion research recovers substantial portions of
    source text from vectors alone, so this table inherits the classification of what
    was embedded and is in scope for the same retention and deletion obligations as the
    rest of the control plane. `text_hash` exists so a re-embed can be skipped when
    nothing changed, without keeping a second copy of the text.
    """

    __tablename__ = "embedding"
    __table_args__ = (
        UniqueConstraint("organization_id", "owner_type", "owner_id", "chunk_index"),
        Index("ix_embedding_owner", "organization_id", "owner_type", "owner_id"),
        Index("ix_embedding_signature", "organization_id", "index_signature"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_type: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    index_signature: Mapped[str] = mapped_column(String(400), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    vector_norm: Mapped[float] = mapped_column(Float, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)


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


class AccessPolicy(Base, TimestampMixin):
    """Attribute-based access policy (ADR-0018).

    RBAC alone stops scaling at exactly the point a bank estate becomes
    interesting. A policy keys on what a resource *is* -- its classification, its
    business node, its certification status -- so it covers the column discovered
    next Tuesday with no administrative action.

    Two properties are load-bearing and are enforced in `policy_engine.py`:

    * DENY is a hard ceiling. It cannot be overridden by any role, including
      workspace owner and platform admin.
    * `principal_kind` is a first-class subject attribute, so "humans may see full
      account numbers, agents never do" is one policy rather than an
      inexpressible intention.

    Versioned and immutable per version, so a decision made a year ago can be
    replayed against the policy that was in force.
    """

    __tablename__ = "access_policy"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", "version"),
        Index("ix_access_policy_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    effect: Mapped[str] = mapped_column(String(20), nullable=False)
    # Higher wins among ALLOWs; DENY always wins regardless of priority.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    subject_match: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    resource_match: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    action_match: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    transform: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    origin: Mapped[str] = mapped_column(String(20), default="MANUAL", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


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


class DataSource(Base, TimestampMixin):
    __tablename__ = "datasource"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

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
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    environment: Mapped[str] = mapped_column(String(30), nullable=False)
    network_zone: Mapped[str] = mapped_column(String(100), default="default", nullable=False)
    credential_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="REGISTERED", nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MetadataCatalog(Base, TimestampMixin):
    __tablename__ = "metadata_catalog"
    __table_args__ = (UniqueConstraint("datasource_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataSchema(Base, TimestampMixin):
    __tablename__ = "metadata_schema"
    __table_args__ = (UniqueConstraint("catalog_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    catalog_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_catalog.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataTable(Base, TimestampMixin):
    __tablename__ = "metadata_table"
    __table_args__ = (
        UniqueConstraint("schema_id", "name"),
        Index("ix_metadata_table_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_schema.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_description: Mapped[str | None] = mapped_column(Text)
    # CT-4: set when a RenameCandidate naming this (tombstoned) row is approved and merged --
    # lets anyone still holding this stable ID resolve forward to the object it became.
    superseded_by_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )


class MetadataColumn(Base, TimestampMixin):
    __tablename__ = "metadata_column"
    __table_args__ = (
        UniqueConstraint("table_id", "name"),
        Index("ix_metadata_column_org_class", "organization_id", "classification"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    physical_type: Mapped[str] = mapped_column(String(255), nullable=False)
    nullable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    default_expression: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(30), default="UNCLASSIFIED", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataConstraint(Base, TimestampMixin):
    __tablename__ = "metadata_constraint"
    __table_args__ = (
        UniqueConstraint("table_id", "name"),
        Index("ix_metadata_constraint_org_type", "organization_id", "constraint_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    constraint_type: Mapped[str] = mapped_column(String(30), nullable=False)
    columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    referenced_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    referenced_columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class AnalysisRun(Base, TimestampMixin):
    __tablename__ = "analysis_run"
    __table_args__ = (Index("ix_analysis_run_org_status", "organization_id", "status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    resumed_from_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    mode: Mapped[str] = mapped_column(String(30), default="INCREMENTAL", nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(30), default="MANUAL", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", nullable=False)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    discovered_catalogs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_schemas: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_tables: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_columns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_constraints: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_objects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_objects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deprecated_objects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profiled_tables: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profiled_columns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class ScanPolicy(Base, TimestampMixin):
    __tablename__ = "scan_policy"
    __table_args__ = (
        UniqueConstraint("datasource_id"),
        Index("ix_scan_policy_due", "enabled", "next_run_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(30), default="INCREMENTAL", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    maintenance_start_hour_utc: Mapped[int | None] = mapped_column(Integer)
    maintenance_end_hour_utc: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    # Usage-weighted priority (ADR-0017 SS8): opt-in per policy. `priority` remains the
    # single column the fleet scheduler orders by (due_scan_policies_statement is
    # unchanged) -- when usage_boost_enabled, the scheduler periodically recomputes
    # `priority = base_priority + computed_usage_boost` (clamped to 0-100) instead of
    # adding the boost at query time, so admission ordering and scan-policy ordering stay
    # on the exact same column they always were. `base_priority` is the admin's last
    # explicitly-set value (captured on every upsert) and is never itself overwritten by
    # the boost, so recomputation is always relative to the admin's real choice, never
    # compounding on a previous boost.
    usage_boost_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    computed_usage_boost: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_boost_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TableProfile(Base):
    """Immutable, run-scoped table statistics with no source values persisted."""

    __tablename__ = "table_profile"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "table_id"),
        Index("ix_table_profile_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_version: Mapped[str] = mapped_column(String(50), default="safe-v1", nullable=False)
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    row_count_estimate: Mapped[int | None] = mapped_column(BigInteger)
    sampled_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="COMPLETED", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ColumnProfile(Base):
    """Value-free column statistics used for search, quality hints, and planning."""

    __tablename__ = "column_profile"
    __table_args__ = (UniqueConstraint("table_profile_id", "column_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("table_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    null_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    non_null_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approximate_distinct_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_length: Mapped[int | None] = mapped_column(Integer)
    max_length: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DataQualityPolicy(Base, TimestampMixin):
    """Version-light operational thresholds scoped to a source or one catalog table."""

    __tablename__ = "data_quality_policy"
    __table_args__ = (
        UniqueConstraint("datasource_id", "scope_key"),
        Index("ix_data_quality_policy_org_enabled", "organization_id", "enabled"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), index=True
    )
    scope_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    volume_change_percent: Mapped[float] = mapped_column(Float, default=30.0, nullable=False)
    null_rate_change_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    schema_change_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_scan_max_age_minutes: Mapped[int] = mapped_column(
        Integer, default=1440, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class DataQualityObservation(Base):
    """Immutable value-free comparison between a profile and its historical baseline."""

    __tablename__ = "data_quality_observation"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "table_id"),
        Index("ix_quality_observation_source_created", "datasource_id", "created_at"),
        Index("ix_quality_observation_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    baseline_profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("table_profile.id", ondelete="SET NULL"), index=True
    )
    policy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_quality_policy.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    anomaly_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DataQualityIncident(Base, TimestampMixin):
    """Durable anomaly lifecycle; one record is reopened when the same control regresses."""

    __tablename__ = "data_quality_incident"
    __table_args__ = (
        UniqueConstraint("fingerprint"),
        Index("ix_quality_incident_source_status", "datasource_id", "status"),
        Index("ix_quality_incident_org_severity", "organization_id", "severity"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_quality_policy.id", ondelete="SET NULL"), index=True
    )
    latest_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_quality_observation.id", ondelete="SET NULL"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    anomaly_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_by: Mapped[str | None] = mapped_column(String(255))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_reason: Mapped[str | None] = mapped_column(String(1000))


class QueryExecution(Base, TimestampMixin):
    __tablename__ = "query_execution"
    __table_args__ = (
        Index("ix_query_execution_org_created", "organization_id", "created_at"),
        Index("ix_query_execution_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RECEIVED", nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_sql: Mapped[str | None] = mapped_column(Text)
    referenced_tables: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    referenced_columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    column_lineage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    policy_version: Mapped[str] = mapped_column(
        String(100), default="development-v1", nullable=False
    )
    semantic_version: Mapped[str | None] = mapped_column(String(100))
    plan_cost: Mapped[float | None] = mapped_column(Float)
    warehouse_query_id: Mapped[str | None] = mapped_column(String(255))
    row_count: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))


class AgentRun(Base, TimestampMixin):
    """Auditable orchestration envelope; raw user questions are intentionally not persisted."""

    __tablename__ = "agent_run"
    __table_args__ = (
        Index("ix_agent_run_org_created", "organization_id", "created_at"),
        Index("ix_agent_run_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RECEIVED", nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_source: Mapped[str] = mapped_column(String(50), nullable=False)
    model_route: Mapped[str | None] = mapped_column(String(255))
    semantic_version: Mapped[str | None] = mapped_column(String(100))
    policy_version: Mapped[str] = mapped_column(
        String(100), default="development-v1", nullable=False
    )
    query_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("query_execution.id", ondelete="SET NULL"), index=True
    )
    step_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    retrieval_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    plan_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    recommended_tool_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governed_tool_version.id", ondelete="SET NULL"), index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(1000))


class AgentEvaluationRun(Base, TimestampMixin):
    __tablename__ = "agent_evaluation_run"
    __table_args__ = (Index("ix_agent_evaluation_org_created", "organization_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    scenario_count: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pass_rate: Mapped[float] = mapped_column(Float, nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)


class ModelRouteConfiguration(Base, TimestampMixin):
    """Governed, non-secret model endpoint definition; approval never registers an adapter."""

    __tablename__ = "model_route_configuration"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "route_key",
            "version",
            name="uq_model_route_configuration_organization_id_route_key_version",
        ),
        Index("ix_model_route_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    route_key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_reference: Mapped[str | None] = mapped_column(String(1000))
    data_residency: Mapped[str] = mapped_column(String(100), nullable=False)
    retention_policy: Mapped[str] = mapped_column(String(50), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SemanticModelVersion(Base, TimestampMixin):
    __tablename__ = "semantic_model_version"
    __table_args__ = (
        UniqueConstraint("project_id", "version"),
        Index("ix_semantic_model_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    change_summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    based_on_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("semantic_model_version.id", ondelete="SET NULL"), index=True
    )


class SemanticMetric(Base, TimestampMixin):
    __tablename__ = "semantic_metric"
    __table_args__ = (UniqueConstraint("project_id", "slug"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(100), nullable=False)


class SemanticMetricVersion(Base, TimestampMixin):
    __tablename__ = "semantic_metric_version"
    __table_args__ = (
        UniqueConstraint("metric_id", "version"),
        UniqueConstraint(
            "semantic_model_version_id",
            "metric_id",
            name="uq_semantic_metric_version_model_metric",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    semantic_model_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("semantic_model_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_id: Mapped[UUID] = mapped_column(
        ForeignKey("semantic_metric.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    aggregation: Mapped[str] = mapped_column(String(30), nullable=False)
    grain: Mapped[str] = mapped_column(String(255), nullable=False)
    source_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    measure_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="RESTRICT"), index=True
    )
    default_time_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="RESTRICT"), index=True
    )
    allowed_dimension_column_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class GovernanceReview(Base, TimestampMixin):
    __tablename__ = "governance_review"
    __table_args__ = (Index("ix_governance_review_org_status", "organization_id", "status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    object_type: Mapped[str] = mapped_column(String(100), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    requested_action: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decision_reason: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GovernedTool(Base, TimestampMixin):
    __tablename__ = "governed_tool"
    __table_args__ = (UniqueConstraint("project_id", "slug"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(100), nullable=False)


class GovernedToolVersion(Base, TimestampMixin):
    __tablename__ = "governed_tool_version"
    __table_args__ = (UniqueConstraint("tool_id", "version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tool_id: Mapped[UUID] = mapped_column(
        ForeignKey("governed_tool.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    semantic_model_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("semantic_model_version.id", ondelete="RESTRICT"), index=True
    )
    sql_template: Mapped[str] = mapped_column(Text, nullable=False)
    referenced_tables: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    parameter_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    allowed_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolExecution(Base, TimestampMixin):
    __tablename__ = "tool_execution"
    __table_args__ = (Index("ix_tool_execution_org_created", "organization_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tool_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("governed_tool_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    query_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("query_execution.id", ondelete="SET NULL"), index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parameter_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RECEIVED", nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))


class QueryMemoryEvidence(Base, TimestampMixin):
    """Value-free evidence for future retrieval; never an automatic execution path."""

    __tablename__ = "query_memory_evidence"
    __table_args__ = (
        UniqueConstraint("agent_run_id"),
        Index("ix_query_memory_lookup", "organization_id", "datasource_id", "question_hash"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    query_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("query_execution.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="ELIGIBLE", nullable=False)
    positive_feedback_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    negative_feedback_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class QueryFeedback(Base, TimestampMixin):
    __tablename__ = "query_feedback"
    __table_args__ = (UniqueConstraint("agent_run_id", "principal_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rating: Mapped[str] = mapped_column(String(30), nullable=False)
    comment_hash: Mapped[str | None] = mapped_column(String(64))


class RelationshipCandidate(Base, TimestampMixin):
    __tablename__ = "relationship_candidate"
    __table_args__ = (
        UniqueConstraint(
            "source_column_id", "target_column_id", name="uq_relationship_candidate_columns"
        ),
        Index("ix_relationship_candidate_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # datasource_id names the SOURCE side's datasource; target_datasource_id names the
    # target's. Equal for a same-source candidate (the only kind before ADR-0017 phase 5),
    # different for a cross-source candidate. Same-domain cross-source pairs are free
    # (ADR-0017 SS4/SS8); a row where the two datasources belong to different data_domains
    # can only have been created by discover_cross_source_relationship_candidates after
    # domain_service.check_cross_boundary_grant confirmed an ACTIVE grant, and is only ever
    # rendered back into a unified lineage graph the same way -- gated per read, not just
    # per write, so a later-expired or revoked grant stops the edge from rendering too.
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TableFamilyCandidate(Base, TimestampMixin):
    """RL-1: evidence-backed table-family / temporal-intelligence candidate.

    A single row records one detected grouping -- a snapshot series, a
    history/audit pair, a delta/CDC pair, or a single SCD Type 2 table -- and
    follows the exact maker-checker review shape established by
    ``RelationshipCandidate`` above (PENDING/APPROVED/REJECTED, created_by /
    reviewed_by / reviewed_at). ``member_table_ids`` holds every
    ``MetadataTable`` id that belongs to the family (exactly one for SCD,
    normally two or more otherwise); ``base_table_id`` is the inferred
    "current/live" table when one can be resolved (never set for SNAPSHOT).
    """

    __tablename__ = "table_family_candidate"
    __table_args__ = (
        Index("ix_table_family_candidate_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_schema.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_type: Mapped[str] = mapped_column(String(20), nullable=False)
    member_table_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    base_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), index=True
    )
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RenameCandidate(Base, TimestampMixin):
    """CT-4: a tombstoned object proposed as a rename of a just-created one.

    Detected automatically inside the same scan run that tombstones the old object and
    creates the new one -- see `aida.workflows.activities.detect_rename_candidates`.
    Approval is a steward decision (maker-checker) and is the only path that reassigns
    the old object's downstream links (see `aida.identity_merge`); rejecting a candidate
    leaves the delete-then-create outcome exactly as it was (module 04 SS6).
    """

    __tablename__ = "rename_candidate"
    __table_args__ = (
        UniqueConstraint("old_table_id", "new_table_id", name="uq_rename_candidate_pair"),
        Index("ix_rename_candidate_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_schema.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    new_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CompositeKeyCandidate(Base, TimestampMixin):
    """PR-1: an evidence-backed, review-gated candidate composite (or single) key.

    Mirrors ``RelationshipCandidate``'s maker-checker shape. ``column_ids`` is
    the ordered list of ``MetadataColumn`` ids that make up the candidate key,
    stored as stringified UUIDs in a JSON list -- the same "list of ids on one
    row" convention already used by e.g. ``ContextProductVersion.table_ids``.
    ``evidence`` carries the full per-column profiling stats (null/non-null/
    approximate-distinct counts) and the ``TableProfile`` context they were
    computed against, so a reviewer can see why this was proposed without
    re-querying anything -- see ``aida.composite_key_inference`` for how it is
    produced and why ``confidence`` is capped well below what a corroborated
    ``RelationshipCandidate`` might reach.
    """

    __tablename__ = "composite_key_candidate"
    __table_args__ = (
        Index("ix_composite_key_candidate_org_status", "organization_id", "status"),
        Index("ix_composite_key_candidate_table", "table_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrossSourceResolutionCandidate(Base, TimestampMixin):
    """CT-6: proposes that two tables in different datasources are the same logical asset.

    The catalog-identity analogue of `RelationshipCandidate`'s cross-source pairing
    (module 06 RL-5): deterministic, metadata-only matching on name/qualified-name
    similarity and column shape -- never row values, per ADR-0014. Discovery is scoped
    and grant-gated exactly like `discover_cross_source_relationship_candidates` --
    free within one `data_domain`, requiring an ACTIVE `CrossBoundaryGrant` to pair
    across a domain boundary (ADR-0017 SS4). Approval only confirms the link; unlike a
    rename candidate it never reassigns either table's downstream references, because
    both tables remain distinct catalog objects in distinct estates.
    """

    __tablename__ = "cross_source_resolution_candidate"
    __table_args__ = (
        UniqueConstraint(
            "source_table_id", "target_table_id", name="uq_cross_source_resolution_pair"
        ),
        Index("ix_cross_source_resolution_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))




class RelationshipCandidateGroundTruthLabel(Base, TimestampMixin):
    """RL-7 (optional, additive): a stronger-than-steward-decision label for one
    `RelationshipCandidate`, for confidence-calibration purposes only.

    A steward's APPROVE/REJECT on the candidate itself is real, legitimate
    first-form ground truth (a human looked at the evidence and decided), and
    calibration reads it directly by default. This table exists only for the
    case where a *later* signal is stronger than that original decision --
    e.g. a labelled banking corpus, or a query-execution confirmation that the
    join is actually used -- without disturbing the original decision record
    on `RelationshipCandidate` itself (maker-checker history, negative
    knowledge) or requiring every calibration reader to know about this table.
    At most one row per candidate: a second label supersedes, it does not
    accumulate a competing opinion.

    Nothing in this platform populates this table yet (no labelled banking
    corpus exists in this environment -- see module 06 RL-7). It is schema
    only, ready for whichever ingestion path is built once such a corpus, or a
    usage-confirmation signal, exists.
    """

    __tablename__ = "relationship_candidate_ground_truth_label"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", name="uq_relationship_candidate_ground_truth_label"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Named `candidate_id`, not `relationship_candidate_id` -- combined with this
    # table's already-long name, the longer column name pushes the default
    # SQLAlchemy index/constraint names (`ix_<table>_<column>`,
    # `fk_<table>_<column>_<referred_table>`) past Postgres's 63-byte
    # NAMEDATALEN limit.
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("relationship_candidate.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    rationale: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class SemanticInferenceRun(Base, TimestampMixin):
    """Bounded metadata-only business inference run."""

    __tablename__ = "semantic_inference_run"
    __table_args__ = (Index("ix_semantic_inference_org_created", "organization_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="RUNNING", nullable=False)
    engine_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_route: Mapped[str | None] = mapped_column(String(255))
    table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    proposal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_enriched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rule_only_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(String(1000))


class BusinessDomain(Base, TimestampMixin):
    __tablename__ = "business_domain"
    __table_args__ = (UniqueConstraint("organization_id", "domain_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    domain_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="APPROVED", nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BusinessEntity(Base, TimestampMixin):
    __tablename__ = "business_entity"
    __table_args__ = (UniqueConstraint("domain_id", "entity_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_domain.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    entity_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="APPROVED", nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MetadataEnrichmentProposal(Base, TimestampMixin):
    __tablename__ = "metadata_enrichment_proposal"
    __table_args__ = (
        UniqueConstraint("inference_run_id", "table_id"),
        Index("ix_metadata_enrichment_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inference_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("semantic_inference_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    governance_review_id: Mapped[UUID] = mapped_column(
        ForeignKey("governance_review.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    proposal_type: Mapped[str] = mapped_column(
        String(50), default="TABLE_BUSINESS_SEMANTICS", nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), default="PENDING_REVIEW", nullable=False)
    engine_type: Mapped[str] = mapped_column(String(30), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_tool_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governed_tool_version.id", ondelete="SET NULL"), index=True
    )


class MetadataBusinessAnnotation(Base, TimestampMixin):
    __tablename__ = "metadata_business_annotation"
    __table_args__ = (
        UniqueConstraint("table_id", name="uq_metadata_business_annotation_table_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False
    )
    domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_domain.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_entity.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_enrichment_proposal.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    business_description: Mapped[str] = mapped_column(Text, nullable=False)
    table_role: Mapped[str] = mapped_column(String(50), nullable=False)
    grain_statement: Mapped[str] = mapped_column(String(1000), nullable=False)
    synonyms: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    suggested_questions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GlossaryCategory(Base, TimestampMixin):
    __tablename__ = "glossary_category"
    __table_args__ = (UniqueConstraint("organization_id", "category_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("glossary_category.id", ondelete="RESTRICT"), index=True
    )
    category_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class GlossaryTerm(Base, TimestampMixin):
    __tablename__ = "glossary_term"
    __table_args__ = (UniqueConstraint("organization_id", "term_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    term_key: Mapped[str] = mapped_column(String(100), nullable=False)
    category_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("glossary_category.id", ondelete="SET NULL"), index=True
    )
    lifecycle_status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_by: Mapped[str | None] = mapped_column(String(255))
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deprecation_reason: Mapped[str | None] = mapped_column(String(2000))


class GlossaryTermVersion(Base, TimestampMixin):
    __tablename__ = "glossary_term_version"
    __table_args__ = (
        UniqueConstraint("term_id", "version"),
        Index("ix_glossary_term_version_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    term_id: Mapped[UUID] = mapped_column(
        ForeignKey("glossary_term.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    synonyms: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    owner_principal: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetDocumentation(Base, TimestampMixin):
    __tablename__ = "asset_documentation"
    __table_args__ = (UniqueConstraint("table_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )


class AssetDocumentationVersion(Base, TimestampMixin):
    __tablename__ = "asset_documentation_version"
    __table_args__ = (
        UniqueConstraint("documentation_id", "version"),
        Index("ix_asset_documentation_version_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    documentation_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_documentation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    readme: Mapped[str] = mapped_column(Text, nullable=False)
    owner_principal: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetTermLink(Base, TimestampMixin):
    __tablename__ = "asset_term_link"
    __table_args__ = (UniqueConstraint("table_id", "term_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    term_id: Mapped[UUID] = mapped_column(
        ForeignKey("glossary_term.id", ondelete="CASCADE"), nullable=False, index=True
    )
    linked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    link_type: Mapped[str] = mapped_column(String(30), default="MANUAL", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    source_annotation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_business_annotation.id", ondelete="SET NULL"), index=True
    )


class OwnershipAssignment(Base, TimestampMixin):
    __tablename__ = "ownership_assignment"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "subject_type",
            "subject_id",
            "owner_type",
            "owner_principal",
            name="uq_ownership_assignment_subject_owner",
        ),
        Index("ix_ownership_assignment_org_subject", "organization_id", "subject_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    owner_type: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    assignment_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    source_rule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ownership_rule.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    assigned_by: Mapped[str] = mapped_column(String(255), nullable=False)


class OwnershipRule(Base, TimestampMixin):
    __tablename__ = "ownership_rule"
    __table_args__ = (UniqueConstraint("organization_id", "rule_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    rule_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    match_field: Mapped[str] = mapped_column(String(30), nullable=False)
    match_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class AssetCertification(Base, TimestampMixin):
    """CT-5: certification of a catalog asset, with expiry enforced at query time.

    Originally table-only (GL-5's reviewed bulk table certification). ``asset_type``
    and ``column_id`` make certification first-class for columns too -- module 04's
    scale note names column as the dominant catalog entity (30x the table count) --
    while ``table_id`` stays populated for both, so "every certification under this
    table" is always a single indexed lookup. A row's ``status`` staying "ACTIVE"
    past its ``expires_at`` is expected (certification history is retained evidence,
    never mutated by a clock); ``aida.asset_certification.asset_certification_is_active``
    is the query-time projection that actually enforces expiry, mirroring
    ``aida.tool_certification.certification_is_active`` for tool version certification.
    """

    __tablename__ = "asset_certification"
    __table_args__ = (
        Index("ix_asset_certification_org_status", "organization_id", "status"),
        CheckConstraint(
            "asset_type IN ('TABLE', 'COLUMN')", name="ck_asset_certification_asset_type"
        ),
        CheckConstraint(
            "(asset_type = 'TABLE' AND column_id IS NULL) OR "
            "(asset_type = 'COLUMN' AND column_id IS NOT NULL)",
            name="ck_asset_certification_column_consistency",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), index=True
    )
    asset_type: Mapped[str] = mapped_column(String(20), default="TABLE", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    certified_by: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GlossaryConflict(Base, TimestampMixin):
    __tablename__ = "glossary_conflict"
    __table_args__ = (Index("ix_glossary_conflict_org_status", "organization_id", "status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    term_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("glossary_term.id", ondelete="CASCADE"), index=True
    )
    conflict_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)
    position_a: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    position_b: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    assigned_owner: Mapped[str | None] = mapped_column(String(255))
    raised_by: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_resolution: Mapped[str | None] = mapped_column(String(30))
    proposed_definition: Mapped[str | None] = mapped_column(Text)
    resolution_rationale: Mapped[str | None] = mapped_column(String(2000))
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BulkStewardshipOperation(Base, TimestampMixin):
    __tablename__ = "bulk_stewardship_operation"
    __table_args__ = (Index("ix_bulk_stewardship_org_status", "organization_id", "status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="REVIEW_REQUIRED", nullable=False)
    governance_review_id: Mapped[UUID] = mapped_column(
        ForeignKey("governance_review.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    applied_by: Mapped[str | None] = mapped_column(String(255))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class GlossaryLinkProposal(Base, TimestampMixin):
    __tablename__ = "glossary_link_proposal"
    __table_args__ = (
        UniqueConstraint(
            "table_id",
            "term_id",
            "source_annotation_id",
            name="uq_glossary_link_proposal_evidence",
        ),
        Index("ix_glossary_link_proposal_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    term_id: Mapped[UUID] = mapped_column(
        ForeignKey("glossary_term.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_annotation_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_business_annotation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL"), unique=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CoverageSnapshot(Base, TimestampMixin):
    __tablename__ = "coverage_snapshot"
    __table_args__ = (
        Index("ix_coverage_snapshot_org_created", "organization_id", "created_at"),
        Index(
            "ix_coverage_snapshot_scope",
            "organization_id",
            "domain_id",
            "line_of_business_id",
            "datasource_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), index=True
    )
    domain_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("business_domain.id", ondelete="CASCADE"), index=True
    )
    line_of_business_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("line_of_business.id", ondelete="CASCADE"), index=True
    )
    table_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    computed_by: Mapped[str] = mapped_column(String(255), nullable=False)


class UnownedAssetEscalation(Base, TimestampMixin):
    """GL-6: tracks a table's unowned-asset backlog entry through routing/escalation.

    One row per table currently or previously flagged by the stewardship-coverage
    "owned" dimension as unowned. Routing/escalation reuses DQ-1's generic
    notification engine (``aida.notification_routing``) against the same
    ``notification_rule`` table quality incidents route through -- this record
    persists the outcome of that reuse (candidate owner, matched rule, dedup key,
    delivery status) since ``notification_event`` is FK-scoped to
    ``data_quality_incident`` and cannot carry a non-incident subject.
    """

    __tablename__ = "unowned_asset_escalation"
    __table_args__ = (
        UniqueConstraint("table_id"),
        Index("ix_unowned_asset_escalation_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    first_detected_unowned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    candidate_owner: Mapped[str | None] = mapped_column(String(255))
    notification_rule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("notification_rule.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[str | None] = mapped_column(String(30))
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(64))
    routed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpenLineageRunEvent(Base, TimestampMixin):
    __tablename__ = "openlineage_run_event"
    __table_args__ = (
        UniqueConstraint("datasource_id", "event_fingerprint"),
        Index("ix_openlineage_event_source_created", "datasource_id", "created_at"),
        Index("ix_openlineage_event_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    producer: Mapped[str] = mapped_column(String(1000), nullable=False)
    schema_url: Mapped[str | None] = mapped_column(String(1000))
    job_namespace: Mapped[str] = mapped_column(String(500), nullable=False)
    job_name: Mapped[str] = mapped_column(String(500), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="IMPORTED", nullable=False)
    input_dataset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    output_dataset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    table_edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    column_edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_dataset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_by: Mapped[str] = mapped_column(String(255), nullable=False)


class OpenLineageDataset(Base, TimestampMixin):
    __tablename__ = "openlineage_dataset"
    __table_args__ = (
        UniqueConstraint("run_event_id", "direction", "namespace", "name"),
        Index("ix_openlineage_dataset_run_direction", "run_event_id", "direction"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("openlineage_run_event.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    namespace: Mapped[str] = mapped_column(String(500), nullable=False)
    name: Mapped[str] = mapped_column(String(1000), nullable=False)
    matched_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    schema_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class OpenLineageTableEdge(Base, TimestampMixin):
    __tablename__ = "openlineage_table_edge"
    __table_args__ = (
        UniqueConstraint(
            "run_event_id",
            "input_dataset_namespace",
            "input_dataset_name",
            "output_dataset_namespace",
            "output_dataset_name",
            name="uq_openlineage_table_edge_run_input_output",
        ),
        Index("ix_openlineage_table_edge_run", "run_event_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("openlineage_run_event.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_dataset_namespace: Mapped[str] = mapped_column(String(500), nullable=False)
    input_dataset_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    input_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    output_dataset_namespace: Mapped[str] = mapped_column(String(500), nullable=False)
    output_dataset_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    output_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    edge_kind: Mapped[str] = mapped_column(String(30), default="ETL", nullable=False)


class OpenLineageColumnEdge(Base, TimestampMixin):
    __tablename__ = "openlineage_column_edge"
    __table_args__ = (
        UniqueConstraint(
            "run_event_id",
            "input_dataset_namespace",
            "input_dataset_name",
            "input_column_name",
            "output_dataset_namespace",
            "output_dataset_name",
            "output_column_name",
            name="uq_openlineage_column_edge_run_input_output",
        ),
        Index("ix_openlineage_column_edge_run", "run_event_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("openlineage_run_event.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_dataset_namespace: Mapped[str] = mapped_column(String(500), nullable=False)
    input_dataset_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    input_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    input_column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    output_dataset_namespace: Mapped[str] = mapped_column(String(500), nullable=False)
    output_dataset_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    output_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    output_column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    transformation_type: Mapped[str | None] = mapped_column(String(100))
    transformation_subtype: Mapped[str | None] = mapped_column(String(100))
    edge_kind: Mapped[str] = mapped_column(String(30), default="ETL", nullable=False)


class DbtProject(Base, TimestampMixin):
    """A governed dbt project registration bound to one warehouse datasource."""

    __tablename__ = "dbt_project"
    __table_args__ = (
        UniqueConstraint("organization_id", "project_key"),
        Index("ix_dbt_project_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_url: Mapped[str | None] = mapped_column(String(1000))
    target_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class DbtArtifactImport(Base, TimestampMixin):
    """Immutable manifest snapshot; the raw artifact is deliberately not persisted."""

    __tablename__ = "dbt_artifact_import"
    __table_args__ = (
        UniqueConstraint("dbt_project_id", "manifest_fingerprint"),
        Index("ix_dbt_artifact_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dbt_project_id: Mapped[UUID] = mapped_column(
        ForeignKey("dbt_project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    dbt_schema_version: Mapped[str] = mapped_column(String(255), nullable=False)
    dbt_version: Mapped[str | None] = mapped_column(String(50))
    invocation_id: Mapped[str | None] = mapped_column(String(255))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="IMPORTED", nullable=False)
    resource_count: Mapped[int] = mapped_column(Integer, nullable=False)
    model_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    test_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lineage_edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_resource_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unmatched_resource_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_by: Mapped[str] = mapped_column(String(255), nullable=False)


class DbtResource(Base, TimestampMixin):
    """Value-safe dbt node/source metadata extracted from one immutable manifest."""

    __tablename__ = "dbt_resource"
    __table_args__ = (
        UniqueConstraint("artifact_import_id", "unique_id"),
        Index("ix_dbt_resource_import_type", "artifact_import_id", "resource_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_import_id: Mapped[UUID] = mapped_column(
        ForeignKey("dbt_artifact_import.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unique_id: Mapped[str] = mapped_column(String(500), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)
    package_name: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    database_name: Mapped[str | None] = mapped_column(String(255))
    schema_name: Mapped[str | None] = mapped_column(String(255))
    relation_name: Mapped[str | None] = mapped_column(String(1000))
    materialization: Mapped[str | None] = mapped_column(String(100))
    original_file_path: Mapped[str | None] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    compiled_sql_hash: Mapped[str | None] = mapped_column(String(64))
    compiled_sql_redacted: Mapped[str | None] = mapped_column(Text)
    sql_parse_status: Mapped[str] = mapped_column(String(30), nullable=False)
    column_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    column_descriptions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    column_types: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    depends_on_unique_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    matched_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    test_status: Mapped[str | None] = mapped_column(String(30))
    test_failures: Mapped[int | None] = mapped_column(Integer)
    test_execution_time: Mapped[float | None] = mapped_column(Float)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DbtLineageEdge(Base, TimestampMixin):
    """A dependency between two dbt resources in one manifest snapshot.

    `edge_type="DEPENDS_ON"` rows are table-level (one per manifest
    `depends_on` pair) and have no column granularity -- `source_column` /
    `target_column` are the empty string on those rows, never NULL, so the
    widened unique constraint below stays meaningful (Postgres treats NULLs
    as distinct from one another, which would defeat de-duplication by the
    full column set). `edge_type="COLUMN_DEPENDS_ON"` rows (LN-5) add
    column-level detail extracted from `compiled_sql_redacted` where the
    manifest provides parseable SQL; `transformation_type` / `confidence`
    are only meaningful on those rows and are NULL on table-level ones.
    """

    __tablename__ = "dbt_lineage_edge"
    __table_args__ = (
        UniqueConstraint(
            "artifact_import_id",
            "source_resource_id",
            "target_resource_id",
            "source_column",
            "target_column",
            name="uq_dbt_lineage_edge_import_source_target_column",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_import_id: Mapped[UUID] = mapped_column(
        ForeignKey("dbt_artifact_import.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_resource_id: Mapped[UUID] = mapped_column(
        ForeignKey("dbt_resource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_resource_id: Mapped[UUID] = mapped_column(
        ForeignKey("dbt_resource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edge_type: Mapped[str] = mapped_column(String(30), default="DEPENDS_ON", nullable=False)
    source_column: Mapped[str | None] = mapped_column(String(255), default="", server_default="")
    target_column: Mapped[str | None] = mapped_column(String(255), default="", server_default="")
    transformation_type: Mapped[str | None] = mapped_column(String(30))
    confidence: Mapped[str | None] = mapped_column(String(30))


class MetadataIngestionJob(Base, TimestampMixin):
    """Idempotent evidence for a canonical metadata push or stream delivery."""

    __tablename__ = "metadata_ingestion_job"
    __table_args__ = (
        UniqueConstraint("datasource_id", "idempotency_key"),
        Index("ix_metadata_ingestion_org_status", "organization_id", "status"),
        Index("ix_metadata_ingestion_source_created", "datasource_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    envelope_version: Mapped[str] = mapped_column(String(20), nullable=False)
    producer: Mapped[str] = mapped_column(String(200), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    object_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    change_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetadataIngestionBatch(Base, TimestampMixin):
    """Durable manifest for a resumable, chunked metadata snapshot."""

    __tablename__ = "metadata_ingestion_batch"
    __table_args__ = (
        UniqueConstraint("datasource_id", "batch_key"),
        Index("ix_ingestion_batch_source_created", "datasource_id", "created_at"),
        Index("ix_ingestion_batch_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    batch_key: Mapped[str] = mapped_column(String(200), nullable=False)
    envelope_version: Mapped[str] = mapped_column(String(20), nullable=False)
    producer: Mapped[str] = mapped_column(String(200), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    received_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    object_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    change_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))


class MetadataIngestionChunk(Base, TimestampMixin):
    """Checksum-addressed chunk; the validated payload is erased after successful processing."""

    __tablename__ = "metadata_ingestion_chunk"
    __table_args__ = (
        UniqueConstraint("batch_id", "chunk_number", name="uq_ingestion_chunk_batch_number"),
        UniqueConstraint("batch_id", "chunk_key", name="uq_ingestion_chunk_batch_key"),
        Index("ix_ingestion_chunk_batch_status", "batch_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_ingestion_batch.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_number: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_key: Mapped[str] = mapped_column(String(200), nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    object_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    change_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RECEIVED", nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorCertificationRun(Base, TimestampMixin):
    """Immutable, attributable connector conformance evidence for one source."""

    __tablename__ = "connector_certification_run"
    __table_args__ = (
        Index("ix_connector_cert_source_created", "datasource_id", "created_at"),
        Index("ix_connector_cert_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(50), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextProduct(Base, TimestampMixin):
    """Stable identity for a governed package of context exposed to AI consumers."""

    __tablename__ = "context_product"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "product_key",
            name="uq_context_product_organization_id_product_key",
        ),
        Index("ix_context_product_project_status", "project_id", "lifecycle_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_key: Mapped[str] = mapped_column(String(100), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class ContextProductVersion(Base, TimestampMixin):
    """Immutable once submitted; a pinned, value-free context product definition."""

    __tablename__ = "context_product_version"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "version",
            name="uq_context_product_version_product_id_version",
        ),
        Index("ix_context_product_version_org_status", "organization_id", "status"),
        Index(
            "uq_context_product_version_one_published",
            "product_id",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        CheckConstraint("version > 0", name="ck_context_product_version_positive"),
        CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPERSEDED', "
            "'REJECTED', 'DEPRECATION_REVIEW', 'DEPRECATED')",
            name="ck_context_product_version_status",
        ),
        CheckConstraint(
            "owner_type IN ('INDIVIDUAL', 'GROUP')",
            name="ck_context_product_version_owner_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(20), default="INDIVIDUAL", nullable=False)
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    table_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    semantic_model_version_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    glossary_term_version_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    eligible_tool_version_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allowed_consumer_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    lineage_depth: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    quality_requirements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    policy_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    based_on_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="SET NULL"), index=True
    )


class ContextProductRoleBinding(Base):
    """Indexed authorization binding for a Context Product version."""

    __tablename__ = "context_product_role_binding"
    __table_args__ = (
        UniqueConstraint(
            "context_product_version_id",
            "role_name",
            name="uq_context_product_role_binding_version_role",
        ),
        Index(
            "ix_context_product_role_binding_org_role",
            "organization_id",
            "role_name",
            "context_product_version_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    context_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_name: Mapped[str] = mapped_column(String(100), nullable=False)


class ContextProductConsumptionEdge(Base):
    """Immutable consumer-to-version lineage edge emitted for every successful read."""

    __tablename__ = "context_product_consumption_edge"
    __table_args__ = (
        Index(
            "ix_context_product_consumption_version_time",
            "context_product_version_id",
            "consumed_at",
        ),
        Index(
            "ix_context_product_consumption_org_principal_time",
            "organization_id",
            "principal_id",
            "consumed_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    context_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    product_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_decision: Mapped[str] = mapped_column(String(30), nullable=False)
    quality_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class McpConsumptionEvidence(Base):
    """Immutable value-free evidence for every successful MCP operation."""

    __tablename__ = "mcp_consumption_evidence"
    __table_args__ = (
        Index("ix_mcp_consumption_org_time", "organization_id", "consumed_at"),
        Index("ix_mcp_consumption_principal_time", "principal_id", "consumed_at"),
        CheckConstraint(
            "operation_kind IN ('CONTROL', 'RESOURCE', 'PROMPT', 'TOOL')",
            name="ck_mcp_consumption_operation_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    method: Mapped[str] = mapped_column(String(100), nullable=False)
    target_reference: Mapped[str | None] = mapped_column(String(500))
    business_purpose: Mapped[str | None] = mapped_column(String(200))
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    policy_decision: Mapped[str] = mapped_column(String(30), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DataProduct(Base, TimestampMixin):
    """Stable identity for a governed, marketplace-visible data product."""

    __tablename__ = "data_product"
    __table_args__ = (
        UniqueConstraint("organization_id", "product_key", name="uq_data_product_org_product_key"),
        Index("ix_data_product_project_lifecycle", "project_id", "lifecycle_status"),
        CheckConstraint(
            "lifecycle_status IN ('CANDIDATE', 'ACTIVE', 'RETIRED')",
            name="ck_data_product_lifecycle",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_key: Mapped[str] = mapped_column(String(100), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(30), default="CANDIDATE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class DataProductVersion(Base, TimestampMixin):
    __tablename__ = "data_product_version"
    __table_args__ = (
        UniqueConstraint("product_id", "version", name="uq_data_product_version_product_version"),
        Index("ix_data_product_version_org_status", "organization_id", "status"),
        Index(
            "uq_data_product_version_one_published",
            "product_id",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        CheckConstraint("version > 0", name="ck_data_product_version_positive"),
        CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPERSEDED', "
            "'REJECTED', 'RETIRED')",
            name="ck_data_product_version_status",
        ),
        CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100)",
            name="ck_data_product_quality_score",
        ),
        CheckConstraint(
            "lineage_coverage >= 0 AND lineage_coverage <= 100",
            name="ck_data_product_lineage_coverage",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_product.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    domain_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    usage_terms: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(String(30), nullable=False)
    certification_status: Mapped[str] = mapped_column(
        String(30), default="UNCERTIFIED", nullable=False
    )
    quality_score: Mapped[int | None] = mapped_column(Integer)
    lineage_coverage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    context_product_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="SET NULL"), index=True
    )
    discoverable_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    consumer_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataProductPort(Base):
    __tablename__ = "data_product_port"
    __table_args__ = (
        UniqueConstraint(
            "data_product_version_id",
            "port_key",
            name="uq_data_product_port_version_key",
        ),
        Index("ix_data_product_port_org_asset", "organization_id", "asset_type", "asset_id"),
        CheckConstraint("direction IN ('INPUT', 'OUTPUT')", name="ck_data_product_port_direction"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    data_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_product_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    port_key: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(255), nullable=False)


class DataProductRoleBinding(Base):
    __tablename__ = "data_product_role_binding"
    __table_args__ = (
        UniqueConstraint(
            "data_product_version_id",
            "role_kind",
            "role_name",
            name="uq_data_product_role_binding_version_kind_role",
        ),
        Index(
            "ix_data_product_role_binding_lookup",
            "organization_id",
            "role_kind",
            "role_name",
            "data_product_version_id",
        ),
        CheckConstraint(
            "role_kind IN ('DISCOVER', 'CONSUME')", name="ck_data_product_role_binding_kind"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    data_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_product_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    role_name: Mapped[str] = mapped_column(String(100), nullable=False)


class DataContractVersion(Base, TimestampMixin):
    __tablename__ = "data_contract_version"
    __table_args__ = (
        UniqueConstraint("product_id", "version", name="uq_data_contract_version_product_version"),
        Index("ix_data_contract_version_org_status", "organization_id", "status"),
        Index(
            "uq_data_contract_version_one_published",
            "product_id",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        CheckConstraint("version > 0", name="ck_data_contract_version_positive"),
        CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPERSEDED', 'REJECTED')",
            name="ck_data_contract_version_status",
        ),
        CheckConstraint(
            "compatibility_status IN ('INITIAL', 'COMPATIBLE', 'BREAKING')",
            name="ck_data_contract_compatibility_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_product.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    compatibility_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    compatibility_status: Mapped[str] = mapped_column(String(30), nullable=False)
    compatibility_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    schema_definition: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    quality_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    freshness_sla_minutes: Mapped[int | None] = mapped_column(Integer)
    availability_sla_percent: Mapped[float | None] = mapped_column(Float)
    producer_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    consumer_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataProductAccessRequest(Base, TimestampMixin):
    __tablename__ = "data_product_access_request"
    __table_args__ = (
        Index("ix_data_product_access_org_status", "organization_id", "status"),
        Index(
            "ix_data_product_access_requester_product",
            "organization_id",
            "requested_by",
            "data_product_version_id",
        ),
        Index(
            "uq_data_product_access_one_pending",
            "data_product_version_id",
            "requested_by",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'REVOKED', 'EXPIRED')",
            name="ck_data_product_access_status",
        ),
        CheckConstraint(
            "fulfillment_status IN ('NOT_REQUESTED', 'PENDING', 'PROVISIONED', 'FAILED', "
            "'REVOKED')",
            name="ck_data_product_access_fulfillment_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    data_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_product_version.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(2000), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    governance_review_id: Mapped[UUID] = mapped_column(
        ForeignKey("governance_review.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decision_reason: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_status: Mapped[str] = mapped_column(
        String(30), default="NOT_REQUESTED", nullable=False
    )
    fulfillment_provider: Mapped[str | None] = mapped_column(String(100))
    fulfillment_reference: Mapped[str | None] = mapped_column(String(500))
    fulfillment_error: Mapped[str | None] = mapped_column(String(1000))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiAsset(Base, TimestampMixin):
    __tablename__ = "ai_asset"
    __table_args__ = (
        UniqueConstraint("organization_id", "asset_key", name="uq_ai_asset_org_key"),
        Index(
            "ix_ai_asset_org_kind_lifecycle", "organization_id", "asset_kind", "lifecycle_status"
        ),
        CheckConstraint("asset_kind IN ('AI_USE_CASE', 'MODEL', 'AGENT')", name="ck_ai_asset_kind"),
        CheckConstraint("lifecycle_status IN ('ACTIVE', 'RETIRED')", name="ck_ai_asset_lifecycle"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    asset_key: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class AiAssetVersion(Base, TimestampMixin):
    __tablename__ = "ai_asset_version"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_ai_asset_version_asset_version"),
        Index("ix_ai_asset_version_org_status", "organization_id", "status"),
        Index(
            "uq_ai_asset_version_one_approved",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'APPROVED'"),
        ),
        CheckConstraint("version > 0", name="ck_ai_asset_version_positive"),
        CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'APPROVED', 'SUPERSEDED', "
            "'REJECTED', 'RETIRED')",
            name="ck_ai_asset_version_status",
        ),
        CheckConstraint(
            "risk_tier IN ('LOW', 'MEDIUM', 'HIGH', 'PROHIBITED')", name="ck_ai_asset_risk_tier"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    intended_use: Mapped[str] = mapped_column(Text, nullable=False)
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(30), nullable=False)
    documentation_url: Mapped[str | None] = mapped_column(String(1000))
    context_product_version_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    model_route_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    policy_control_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evaluation_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    runtime_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiAssessment(Base, TimestampMixin):
    __tablename__ = "ai_assessment"
    __table_args__ = (
        Index("ix_ai_assessment_version_created", "ai_asset_version_id", "created_at"),
        CheckConstraint("score >= 0 AND score <= 100", name="ck_ai_assessment_score"),
        CheckConstraint(
            "status IN ('PASS', 'NEEDS_REMEDIATION', 'FAIL')", name="ck_ai_assessment_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ai_asset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_asset_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    framework: Mapped[str] = mapped_column(String(100), nullable=False)
    framework_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    control_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    assessed_by: Mapped[str] = mapped_column(String(255), nullable=False)


class AiTrustSnapshot(Base):
    """Immutable explainable trust history for an AI asset version."""

    __tablename__ = "ai_trust_snapshot"
    __table_args__ = (
        Index("ix_ai_trust_snapshot_version_time", "ai_asset_version_id", "computed_at"),
        CheckConstraint("score >= 0 AND score <= 100", name="ck_ai_trust_snapshot_score"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ai_asset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_asset_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    grade: Mapped[str] = mapped_column(String(30), nullable=False)
    factors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    blockers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AiRemediation(Base, TimestampMixin):
    __tablename__ = "ai_remediation"
    __table_args__ = (
        Index("ix_ai_remediation_version_status", "ai_asset_version_id", "status"),
        CheckConstraint(
            "status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'ACCEPTED_RISK')",
            name="ck_ai_remediation_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ai_asset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_asset_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    finding_key: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)
    resolution_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    __table_args__ = (
        Index("ix_outbox_pending", "status", "occurred_at"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), index=True
    )
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(150), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(String(1000))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchIndex(Base, TimestampMixin):
    """Full-text search index configuration for catalog metadata."""

    __tablename__ = "search_index"
    __table_args__ = (
        UniqueConstraint("organization_id", "index_key"),
        Index("ix_search_index_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    index_key: Mapped[str] = mapped_column(String(100), nullable=False)
    index_type: Mapped[str] = mapped_column(String(30), default="GIN", nullable=False)
    source_table: Mapped[str] = mapped_column(String(100), nullable=False)
    text_columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    language: Mapped[str] = mapped_column(String(30), default="english", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VectorEmbedding(Base, TimestampMixin):
    """Vector embeddings for catalog metadata, stored as JSON float arrays.

    Uses a JSON column for the embedding vector to avoid a hard dependency on
    pgvector at import time.  The ``ix_vector_embedding_org_type`` index covers
    the org-scoped type lookups the hybrid retrieval pipeline issues; a future
    migration can add a pgvector ivfflat/hnsw index on the ``embedding`` column
    once the extension is provisioned.
    """

    __tablename__ = "vector_embedding"
    __table_args__ = (
        UniqueConstraint("organization_id", "object_type", "object_id"),
        Index("ix_vector_embedding_org_type", "organization_id", "object_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), index=True
    )
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)


class AbacPolicyRecord(Base, TimestampMixin):
    """Versioned ABAC policy rules for attribute-based access control."""

    __tablename__ = "abac_policy"
    __table_args__ = (
        UniqueConstraint("organization_id", "policy_key", "version"),
        Index("ix_abac_policy_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    policy_key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    effect: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    resource_conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    environment_conditions: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class AbacDecisionRecord(Base):
    """Immutable audit log of ABAC evaluation decisions."""

    __tablename__ = "abac_decision"
    __table_args__ = (
        Index("ix_abac_decision_org_created", "organization_id", "evaluated_at"),
        Index("ix_abac_decision_principal", "principal_id", "evaluated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    subject_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resource_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    environment_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    contributing_policy_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evaluation_time_ms: Mapped[float] = mapped_column(Float, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AiDecisionRecord(Base):
    """First-class AI decision edge for the lineage graph."""

    __tablename__ = "ai_decision_record"
    __table_args__ = (
        Index("ix_ai_decision_run", "run_id", "decision_type"),
        Index("ix_ai_decision_asset", "target_node", "decided_at"),
        Index("ix_ai_decision_org_created", "organization_id", "decided_at"),
        Index("ix_ai_decision_refusals", "organization_id", "decision_type",
              postgresql_where=text("decision_type = 'REFUSAL'")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    decision_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_node: Mapped[str] = mapped_column(String(500), nullable=False)
    target_node: Mapped[str] = mapped_column(String(500), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    control_version: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ViewLineageEdge(Base, TimestampMixin):
    """Column-level lineage edge extracted from a SQL view definition."""

    __tablename__ = "view_lineage_edge"
    __table_args__ = (
        Index("ix_view_lineage_edge_org_target", "organization_id", "target_table_id"),
        Index("ix_view_lineage_edge_datasource", "datasource_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table: Mapped[str] = mapped_column(String(500), nullable=False)
    source_column: Mapped[str] = mapped_column(String(255), nullable=False)
    target_table: Mapped[str] = mapped_column(String(500), nullable=False)
    target_column: Mapped[str] = mapped_column(String(255), nullable=False)
    source_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    source_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    target_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    target_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    transformation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ProcedureLineageEdge(Base, TimestampMixin):
    """Column-level lineage edge extracted from a stored procedure body."""

    __tablename__ = "procedure_lineage_edge"
    __table_args__ = (
        Index("ix_procedure_lineage_edge_org_target", "organization_id", "target_table_id"),
        Index("ix_procedure_lineage_edge_datasource", "datasource_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table: Mapped[str] = mapped_column(String(500), nullable=False)
    source_column: Mapped[str] = mapped_column(String(255), nullable=False)
    target_table: Mapped[str] = mapped_column(String(500), nullable=False)
    target_column: Mapped[str] = mapped_column(String(255), nullable=False)
    source_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    source_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    target_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    target_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    transformation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class StudioChangeSet(Base, TimestampMixin):
    """A collection of proposed changes to semantic model objects."""

    __tablename__ = "studio_change_set"
    __table_args__ = (
        Index("ix_studio_change_set_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    base_version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    conflict_status: Mapped[str] = mapped_column(String(30), default="CLEAN", nullable=False)


class StudioChangeItem(Base, TimestampMixin):
    """One item within a Studio change set."""

    __tablename__ = "studio_change_item"
    __table_args__ = (
        Index("ix_studio_change_item_change_set", "change_set_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    change_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("studio_change_set.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    test_status: Mapped[str] = mapped_column(String(30), default="UNTESTED", nullable=False)


class StudioTestRun(Base, TimestampMixin):
    """Test run evidence for a Studio change set."""

    __tablename__ = "studio_test_run"
    __table_args__ = (
        Index("ix_studio_test_run_change_set", "change_set_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    change_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("studio_change_set.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ConsumptionRecord(Base):
    """General-purpose consumption lineage edge: who consumed what, when, and how."""

    __tablename__ = "consumption_record"
    __table_args__ = (
        Index(
            "ix_consumption_record_resource",
            "organization_id",
            "resource_type",
            "resource_id",
            "consumed_at",
        ),
        Index(
            "ix_consumption_record_consumer",
            "organization_id",
            "consumer_id",
            "consumed_at",
        ),
        CheckConstraint(
            "channel IN ('MCP', 'REST', 'GRAPHQL', 'EVENT', 'INTERNAL')",
            name="ck_consumption_record_channel",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    consumer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    consumer_type: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    policy_decision: Mapped[str] = mapped_column(String(30), nullable=False)
    business_purpose: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class NotificationRuleRecord(Base, TimestampMixin):
    """Org-scoped notification routing rule for quality incidents."""

    __tablename__ = "notification_rule"
    __table_args__ = (
        Index("ix_notification_rule_org_enabled", "organization_id", "enabled"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    escalation_after_minutes: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class NotificationEventRecord(Base, TimestampMixin):
    """Outbound notification event tracking delivery and acknowledgement."""

    __tablename__ = "notification_event"
    __table_args__ = (
        Index("ix_notification_event_org_status", "organization_id", "status"),
        Index("ix_notification_event_incident", "incident_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_quality_incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_id: Mapped[UUID] = mapped_column(
        ForeignKey("notification_rule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(255))


class FreshnessWatermarkConfig(Base, TimestampMixin):
    """Watermark-based freshness configuration for a table. Requires maker-checker approval."""

    __tablename__ = "freshness_watermark_config"
    __table_args__ = (UniqueConstraint("table_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    watermark_column: Mapped[str] = mapped_column(String(255), nullable=False)
    classification: Mapped[str] = mapped_column(String(30), default="INTERNAL", nullable=False)
    threshold_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_APPROVAL", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class FreshnessObservation(Base):
    """Observed watermark value for a table at a point in time."""

    __tablename__ = "freshness_observation"
    __table_args__ = (
        Index("ix_freshness_observation_table_time", "table_id", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    watermark_value: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SloDefinition(Base, TimestampMixin):
    """Service-level objective definition, org-scoped."""

    __tablename__ = "slo_definition"
    __table_args__ = (
        UniqueConstraint("organization_id", "slo_key"),
        Index("ix_slo_definition_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slo_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class SloMeasurement(Base):
    """Point-in-time SLO measurement."""

    __tablename__ = "slo_measurement"
    __table_args__ = (
        Index("ix_slo_measurement_slo_time", "slo_id", "measured_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slo_id: Mapped[UUID] = mapped_column(
        ForeignKey("slo_definition.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    budget_remaining: Mapped[float] = mapped_column(Float, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AuditArchiveRecord(Base, TimestampMixin):
    """Immutable record of an audit archive batch."""

    __tablename__ = "audit_archive_record"
    __table_args__ = (
        Index("ix_audit_archive_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    archive_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    event_range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(30), nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_org_occurred", "organization_id", "occurred_at"),
        Index("ix_audit_correlation", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(150), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


# ---------------------------------------------------------------------------
# Runtime Data Contract Enforcement (Phase E - EE.1)
# ---------------------------------------------------------------------------


class ContractViolationRecord(Base, TimestampMixin):
    """Immutable record of a runtime data contract violation."""

    __tablename__ = "contract_violation"
    __table_args__ = (
        Index("ix_contract_violation_org_contract", "organization_id", "contract_id"),
        Index("ix_contract_violation_org_type", "organization_id", "violation_type"),
        Index("ix_contract_violation_detected", "organization_id", "detected_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contract_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_contract_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    violation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(255))


class ContractSlaRecord(Base, TimestampMixin):
    """Periodic SLA compliance record for a data contract."""

    __tablename__ = "contract_sla_record"
    __table_args__ = (
        UniqueConstraint("contract_id", "period_start", name="uq_contract_sla_period"),
        Index("ix_contract_sla_org_contract", "organization_id", "contract_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contract_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_contract_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    uptime_percent: Mapped[float] = mapped_column(Float, nullable=False)
    violations_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    breach_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ---------------------------------------------------------------------------
# Compliance Pack Generation (Phase E - EE.4 / OB-5)
# ---------------------------------------------------------------------------


class CompliancePackRecord(Base, TimestampMixin):
    """WORM-archived compliance pack generated from runtime evidence."""

    __tablename__ = "compliance_pack"
    __table_args__ = (
        Index("ix_compliance_pack_org_framework", "organization_id", "framework"),
        Index("ix_compliance_pack_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    framework: Mapped[str] = mapped_column(String(50), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="GENERATED", nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


# ---------------------------------------------------------------------------
# Negative Knowledge Surface (Phase E - EE.3)
# ---------------------------------------------------------------------------


class NegativeAssertionRecord(Base, TimestampMixin):
    """Queryable negative knowledge: what the system decided is not true."""

    __tablename__ = "negative_assertion"
    __table_args__ = (
        Index("ix_negative_assertion_org_subject", "organization_id", "subject_id"),
        Index("ix_negative_assertion_org_type", "organization_id", "assertion_type"),
        Index(
            "ix_negative_assertion_suppression",
            "organization_id",
            "suppression_active",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    assertion_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    predicate: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    rejected_by: Mapped[str] = mapped_column(String(255), nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    suppression_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    material_change_hash: Mapped[str | None] = mapped_column(String(64))
    suppression_lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppression_lifted_by: Mapped[str | None] = mapped_column(String(255))
    lift_reason: Mapped[str | None] = mapped_column(String(2000))


# ---------------------------------------------------------------------------
# Multi-Step Tool Plans (Phase E - EE.6 / AG-4)
# ---------------------------------------------------------------------------


class ToolPlanRecord(Base, TimestampMixin):
    """Governed multi-step tool execution plan."""

    __tablename__ = "tool_plan"
    __table_args__ = (
        Index("ix_tool_plan_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class ToolPlanStepRecord(Base, TimestampMixin):
    """Individual step within a governed tool plan."""

    __tablename__ = "tool_plan_step"
    __table_args__ = (
        UniqueConstraint("plan_id", "sequence", name="uq_tool_plan_step_sequence"),
        Index("ix_tool_plan_step_plan", "plan_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_plan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(50), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    dependencies: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    expected_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))


class ToolPlanExecutionRecord(Base, TimestampMixin):
    """Execution envelope for a tool plan run."""

    __tablename__ = "tool_plan_execution"
    __table_args__ = (
        Index("ix_tool_plan_execution_plan", "plan_id"),
        Index("ix_tool_plan_execution_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_plan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_consumed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RUNNING", nullable=False)
    executed_by: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# TL-1: tool certification corpus and workflow (module 14, tool registry).
#
# Mirrors ConnectorCertificationRun (module 09 connector "100-point" cert) and
# AssetCertification (module 08 glossary GL-5 bulk certification with expiry):
# a corpus of deterministic test cases is executed against a governed tool
# version's real invocation path (aida.tool_rendering.render_tool_sql -- the
# AST literal-binding step module 14 owns per its "not responsibilities"
# boundary with 16 query-gateway) and countersigned maker != checker before it
# becomes an active certification. Runs are immutable and never deleted or
# rewritten by recertification: "current" certification is a query-time
# projection over non-expired CERTIFIED runs, exactly like AssetCertification.
# ---------------------------------------------------------------------------


class ToolCertificationCase(Base, TimestampMixin):
    """One deterministic case in a governed tool's certification corpus."""

    __tablename__ = "tool_certification_case"
    __table_args__ = (
        UniqueConstraint("tool_id", "case_key", name="uq_tool_certification_case_key"),
        Index("ix_tool_certification_case_tool_status", "tool_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tool_id: Mapped[UUID] = mapped_column(
        ForeignKey("governed_tool.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_key: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    expectation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class ToolCertificationRun(Base, TimestampMixin):
    """Immutable, attributable certification evidence for one tool version.

    ``status`` moves PENDING_REVIEW -> CERTIFIED/REJECTED once a checker
    decides, or straight to CERTIFICATION_FAILED when the corpus itself did
    not fully pass (a failed corpus can never be countersigned into a
    certification -- this is evidence-driven, not a rubber stamp).
    Recertification is simply a new row: history is preserved forever.
    """

    __tablename__ = "tool_certification_run"
    __table_args__ = (
        Index("ix_tool_certification_run_tool_created", "tool_id", "created_at"),
        Index("ix_tool_certification_run_org_status", "organization_id", "status"),
        Index("ix_tool_certification_run_version_status", "tool_version_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tool_id: Mapped[UUID] = mapped_column(
        ForeignKey("governed_tool.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("governed_tool_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    suite_version: Mapped[str] = mapped_column(String(50), nullable=False)
    corpus_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_REVIEW", nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    executed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    certified_by: Mapped[str | None] = mapped_column(String(255))
    decision_reason: Mapped[str | None] = mapped_column(String(2000))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# BI lineage (LN-4, module 09) — Tableau / Power BI / Looker report -> metric
# -> column edges. Mirrors the OpenLineage and dbt ingestion shape above: an
# immutable, value-free artifact snapshot plus its extracted nodes and edges.
# Only Tableau has a parser today (see aida.bi_lineage); the other tools are
# accepted at the connection/import layer as a pluggable extension point.
# ---------------------------------------------------------------------------


class BiConnection(Base, TimestampMixin):
    """A governed registration of one BI tool site/workspace bound to a warehouse datasource."""

    __tablename__ = "bi_connection"
    __table_args__ = (
        UniqueConstraint("organization_id", "connection_key"),
        Index("ix_bi_connection_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bi_tool: Mapped[str] = mapped_column(String(30), nullable=False)
    connection_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    site_or_workspace: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class BiArtifactImport(Base, TimestampMixin):
    """Immutable BI metadata artifact snapshot; the raw artifact is deliberately not persisted."""

    __tablename__ = "bi_artifact_import"
    __table_args__ = (
        UniqueConstraint("connection_id", "artifact_fingerprint"),
        Index("ix_bi_artifact_import_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_connection.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    bi_tool: Mapped[str] = mapped_column(String(30), nullable=False)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="IMPORTED", nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report_metric_edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_column_edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_column_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unmatched_column_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_by: Mapped[str] = mapped_column(String(255), nullable=False)


class BiReportNode(Base, TimestampMixin):
    """A workbook, dashboard, or sheet/report extracted from one BI artifact import."""

    __tablename__ = "bi_report_node"
    __table_args__ = (
        UniqueConstraint("artifact_import_id", "external_id"),
        Index("ix_bi_report_node_import_type", "artifact_import_id", "report_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_import_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_artifact_import.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_report_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bi_report_node.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    report_type: Mapped[str] = mapped_column(String(30), nullable=False)
    project_name: Mapped[str | None] = mapped_column(String(255))


class BiMetricNode(Base, TimestampMixin):
    """A field/metric (calculated, column, group, ...) extracted from one BI artifact import."""

    __tablename__ = "bi_metric_node"
    __table_args__ = (
        UniqueConstraint("artifact_import_id", "external_id"),
        Index("ix_bi_metric_node_import_type", "artifact_import_id", "field_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_import_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_artifact_import.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False)
    datasource_name: Mapped[str | None] = mapped_column(String(255))
    # The raw calculation formula is never persisted — see aida.bi_lineage._formula_hash.
    formula_hash: Mapped[str | None] = mapped_column(String(64))
    formula_present: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class BiReportMetricEdge(Base, TimestampMixin):
    """Report -> metric edge: this workbook/sheet/dashboard uses this field."""

    __tablename__ = "bi_report_metric_edge"
    __table_args__ = (
        UniqueConstraint(
            "artifact_import_id",
            "report_id",
            "metric_id",
            name="uq_bi_report_metric_edge_import_report_metric",
        ),
        Index("ix_bi_report_metric_edge_import", "artifact_import_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_import_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_artifact_import.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_report_node.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_metric_node.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edge_kind: Mapped[str] = mapped_column(String(30), default="BI", nullable=False)


class BiMetricColumnEdge(Base, TimestampMixin):
    """Metric -> column edge: this field derives from this underlying source column."""

    __tablename__ = "bi_metric_column_edge"
    __table_args__ = (
        UniqueConstraint(
            "artifact_import_id",
            "metric_id",
            "source_database_name",
            "source_schema_name",
            "source_table_name",
            "source_column_name",
            name="uq_bi_metric_column_edge_import_metric_source",
        ),
        Index("ix_bi_metric_column_edge_import", "artifact_import_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_import_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_artifact_import.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_id: Mapped[UUID] = mapped_column(
        ForeignKey("bi_metric_node.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_database_name: Mapped[str | None] = mapped_column(String(255))
    source_schema_name: Mapped[str | None] = mapped_column(String(255))
    source_table_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    matched_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    matched_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    edge_kind: Mapped[str] = mapped_column(String(30), default="BI", nullable=False)


# ---------------------------------------------------------------------------
# CT-1: Catalog bulk actions (tag, classify, own, certify)
# ---------------------------------------------------------------------------


class AssetTag(Base, TimestampMixin):
    """A steward-applied label on a table asset (module 04 domain: asset_tag)."""

    __tablename__ = "asset_tag"
    __table_args__ = (
        UniqueConstraint("table_id", "tag_key"),
        Index("ix_asset_tag_org_key", "organization_id", "tag_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_key: Mapped[str] = mapped_column(String(100), nullable=False)
    tag_value: Mapped[str | None] = mapped_column(String(500))
    applied_by: Mapped[str] = mapped_column(String(255), nullable=False)


class CatalogBulkActionRun(Base, TimestampMixin):
    """Durable partial-success record for a CT-1 catalog bulk action.

    One row per bulk request (tag/classify/own/certify), carrying the
    per-subject outcome so a caller can retrieve which items succeeded and
    which failed (and why) after the fact, not just in the synchronous
    response.
    """

    __tablename__ = "catalog_bulk_action_run"
    __table_args__ = (
        Index(
            "ix_catalog_bulk_action_run_org_action", "organization_id", "action", "created_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    succeeded_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)


# ---------------------------------------------------------------------------
# SM-2: Glossary term binding to semantic objects
# ---------------------------------------------------------------------------


class TermSemanticBinding(Base, TimestampMixin):
    """Reviewable link between a glossary term (module 08) and a semantic
    object (module 07 -- today only a published `SemanticMetric`; a future
    governed-dimension type from SM-1 binds the same way without a schema
    change).

    Mirrors `CrossBoundaryGrant`'s maker-checker shape rather than GL-8's
    evidence-inference shape (`GlossaryLinkProposal`): a binding is a direct
    steward assertion, not something inferred from approved annotations, so
    it is created `PENDING_APPROVAL` and only becomes `ACTIVE` once an
    independent reviewer decides it through the shared governance review
    queue (`semantic_api.decide_governance_review`,
    object_type="TERM_SEMANTIC_BINDING"). Only an `ACTIVE` binding
    participates in retrieval (`retrieval.hybrid_retrieve`).

    `semantic_object_type` is deliberately open so it does not need a schema
    change when a second semantic-object kind exists; `semantic_object_id`
    therefore carries no FK constraint of its own -- the same polymorphic
    subject-reference pattern `OwnershipAssignment.subject_id` already uses
    elsewhere in this module, just typed as `UUID` here because every
    semantic object today has a UUID primary key and callers join on it
    directly (see `retrieval.hybrid_retrieve`).
    """

    __tablename__ = "term_semantic_binding"
    __table_args__ = (
        UniqueConstraint(
            "term_id",
            "semantic_object_type",
            "semantic_object_id",
            name="uq_term_semantic_binding_term_object",
        ),
        Index("ix_term_semantic_binding_org_status", "organization_id", "status"),
        Index(
            "ix_term_semantic_binding_object",
            "semantic_object_type",
            "semantic_object_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    term_id: Mapped[UUID] = mapped_column(
        ForeignKey("glossary_term.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semantic_object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    semantic_object_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_APPROVAL", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL"), unique=True
    )
