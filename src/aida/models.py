from datetime import datetime  # noqa: I001
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

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.identity_tenancy.models` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.models import Organization` (etc.) caller keeps working
# unchanged. `TimestampMixin`/`utc_now` moved to `atlas.platform.db` in the
# same pass -- they are shared infrastructure, not identity-tenancy-owned,
# and other classes in *this* file (below) still use them.
from atlas.modules.identity_tenancy.models import (
    AuthorizationShadowRecord as AuthorizationShadowRecord,
    BusinessAssignment as BusinessAssignment,
    BusinessAssignmentRule as BusinessAssignmentRule,
    BusinessNode as BusinessNode,
    BusinessNodeClosure as BusinessNodeClosure,
    BusinessNodeRollup as BusinessNodeRollup,
    CrossBoundaryGrant as CrossBoundaryGrant,
    DataDomain as DataDomain,
    Delegation as Delegation,
    IsolationBoundary as IsolationBoundary,
    LineOfBusiness as LineOfBusiness,
    Organization as Organization,
    OrganizationIntegrationPolicy as OrganizationIntegrationPolicy,
    Project as Project,
    RevokedToken as RevokedToken,
    SourceBinding as SourceBinding,
    Workspace as Workspace,
    WorkspaceAccessRule as WorkspaceAccessRule,
    WorkspaceMembership as WorkspaceMembership,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.connectivity.models` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.models import DataSource` (etc.) caller keeps working unchanged.
from atlas.modules.connectivity.models import (
    ConnectorCertificationRun as ConnectorCertificationRun,
    DataSource as DataSource,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.catalog.models` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.models import MetadataTable` (etc.) caller keeps working
# unchanged.
from atlas.modules.catalog.models import (
    MetadataCatalog as MetadataCatalog,
    MetadataColumn as MetadataColumn,
    MetadataConstraint as MetadataConstraint,
    MetadataIndex as MetadataIndex,
    MetadataPartition as MetadataPartition,
    MetadataSchema as MetadataSchema,
    MetadataTable as MetadataTable,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.ingestion.models` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.models import MetadataIngestionJob` (etc.) caller keeps
# working unchanged.
from atlas.modules.ingestion.models import (
    MetadataIngestionBatch as MetadataIngestionBatch,
    MetadataIngestionChunk as MetadataIngestionChunk,
    MetadataIngestionJob as MetadataIngestionJob,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.observability_audit.models` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.models import AuditEvent` (etc.) caller keeps working
# unchanged.
from atlas.modules.observability_audit.models import (
    AccessReviewReportRecord as AccessReviewReportRecord,
    AuditArchiveRecord as AuditArchiveRecord,
    AuditEvent as AuditEvent,
    CompliancePackRecord as CompliancePackRecord,
    OutboxEvent as OutboxEvent,
    SloDefinition as SloDefinition,
    SloMeasurement as SloMeasurement,
)
from atlas.platform.db import TimestampMixin as TimestampMixin, utc_now as utc_now


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


class ClassificationEvidence(Base):
    """Append-only provenance ledger for column classification decisions.

    Every rule-based classification and every authoritative-feed override is
    recorded here (never mutated), with ``is_current`` marking the row that
    matches ``MetadataColumn.classification`` right now — so "why is this
    column classified this way, and was it inferred or externally asserted"
    is always answerable without guessing from the column row alone.
    """

    __tablename__ = "classification_evidence"
    __table_args__ = (
        Index("ix_classification_evidence_column_current", "column_id", "is_current"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    classification: Mapped[str] = mapped_column(String(30), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    matched_signal: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ColumnDerivedClassification(Base):
    """AT-11: a column's *derived* classification -- one propagated to it along
    data lineage from a more-sensitive upstream column -- kept in its own table,
    strictly separate from the *asserted* classification that lives on
    ``MetadataColumn.classification`` (a steward decision, or an authoritative
    external feed; see ``aida.classification_feed``).

    The separation is the whole point of the row: for us a classification is an
    ABAC enforcement input, not a display label, so a value the graph *inferred*
    must never silently become a value a policy *enforces on*. A derived value
    only becomes asserted by going through the shared maker-checker review queue
    (a ``GovernanceReview`` of object type ``COLUMN_CLASSIFICATION_PROMOTION`` --
    see ``aida.classification_propagation``); nothing else may copy
    ``classification`` onto the ``MetadataColumn``.

    Evidence is first-class and queryable: ``edge_chain`` is the ordered list of
    lineage edges the classification travelled (origin -> this column), and
    ``graph_version`` is the fingerprint of the lineage graph the propagation ran
    over, so "why is this column derived-PII, and along which edges" is always
    answerable. Propagation is raise-only and follows only authoritative edge
    kinds (never inferred ``INFLUENCES`` edges) -- both enforced in
    ``aida.classification_propagation``, not here.

    ``is_current`` marks the row that reflects the latest propagation pass for a
    column, mirroring ``ClassificationEvidence``'s append-only ledger shape.
    """

    __tablename__ = "column_derived_classification"
    __table_args__ = (
        Index(
            "ix_column_derived_classification_column_current", "column_id", "is_current"
        ),
        CheckConstraint(
            "status IN ('DERIVED', 'PROMOTION_PENDING', 'PROMOTED', 'PROMOTION_REJECTED')",
            name="ck_column_derived_classification_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    classification: Mapped[str] = mapped_column(String(30), nullable=False)
    # The upstream column whose asserted classification propagated here. SET NULL
    # rather than CASCADE: losing the origin column must not silently delete the
    # evidence that a downstream column was raised because of it.
    origin_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    origin_classification: Mapped[str] = mapped_column(String(30), nullable=False)
    # Ordered edges the classification travelled (origin -> this column), each a
    # value-free descriptor: {source_id, target_id, kind, edge_ref}.
    edge_chain: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    graph_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DERIVED", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The COLUMN_CLASSIFICATION_PROMOTION GovernanceReview that promotes (or
    # rejected) this derived value. Plain id, no FK: the review lives in a
    # different module's table and the coupling is deliberately loose.
    review_id: Mapped[UUID | None] = mapped_column(index=True)
    promoted_by: Mapped[str | None] = mapped_column(String(255))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


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
    discovered_indexes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_partitions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_objects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_objects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deprecated_objects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profiled_tables: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profiled_columns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class AnalysisTask(Base, TimestampMixin):
    """Persisted per-task evidence for one node of the analysis-run DAG.

    Temporal tracks attempt count, heartbeats, and retry backoff for each
    activity invocation, but that state lives only inside the Temporal
    cluster. This table is the operator-facing mirror of it — written by
    ``aida.task_tracking`` at the start, on heartbeat, and at the end of every
    task — so ``GET /v1/analysis-runs/{id}/tasks[/…]`` can show attempt
    count, last heartbeat, and failure reason for a stuck or failing run
    without reaching into Temporal directly (module 05 §6/§10, PR-4).
    """

    __tablename__ = "analysis_task"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "task_key", name="uq_analysis_task_run_key"),
        Index("ix_analysis_task_run_status", "analysis_run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    task_key: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)


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


class ProfilingExceptionPolicy(Base, TimestampMixin):
    """PR-2: the maker-checker gate for value-bearing profiling (ADR-0014 exception).

    Module 05 §8: ranges and top values are never computed by default -- only
    a policy-approved, classification-specific exception with its own
    retention contract may unlock it, scoped to exactly one
    ``(organization_id, classification, datasource_id)`` triple. Mirrors
    ``GovernanceReview``'s maker-checker shape (a different principal must
    decide than the one who requested) but keeps its own denormalized
    ``status``/``requested_by``/``decided_by`` fields rather than filing into
    the shared ``governance_review`` queue: that queue's decision endpoint is
    already a large per-object-type dispatcher (semantic models, tool
    versions, model routes, ...), and this policy's shape -- scoped to a
    classification tuple, carrying its own retention contract, gating a
    connector capability rather than flipping one row's status -- does not
    fit its existing branches without either distorting them or growing that
    dispatcher further. A single active (``PENDING`` or ``APPROVED``) policy
    per scope is enforced at request time in ``api.py``, not by a DB
    constraint, so a ``REJECTED``/``REVOKED`` policy never blocks a fresh
    request for the same scope.
    """

    __tablename__ = "profiling_exception_policy"
    __table_args__ = (
        Index(
            "ix_profiling_exception_policy_scope",
            "organization_id",
            "datasource_id",
            "classification",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    classification: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    # Pinned onto every `ColumnValueProfileArtifact` this policy authorizes at
    # the moment each one is captured -- changing this column on an existing
    # policy only affects artifacts captured after the change, never rewrites
    # the retention already committed to an earlier artifact.
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    request_reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decision_reason: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(255))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(2000))


class PolicyNativeSyncRequest(Base, TimestampMixin):
    """QG-2: the maker-checker gate for applying source-native row/column policy DDL.

    `aida.policy_native_sync.build_native_sync_plan` generates the DDL (dry-run,
    no gate needed -- nothing changes on the source from generation alone); this
    table is what a *governed apply* of that DDL against a live source looks like.
    Mirrors `ProfilingExceptionPolicy`'s shape for the same reason its own
    docstring gives: a different principal must decide than the one who requested
    (maker != checker), but the object being decided -- a set of generated DDL
    statements scoped to one table, gating a live write to an external source
    rather than flipping one row's status -- does not fit the shared
    `governance_review` queue's existing per-object-type dispatcher
    (`semantic_api._apply_governance_review_decision`) without distorting it.

    `statements` is the exact, already-generated DDL this decision is about --
    frozen at request time, not regenerated at apply time, so a checker approves
    precisely what they read and an apply can never drift from what was reviewed
    even if the underlying policy set changes between request and decision.
    """

    __tablename__ = "policy_native_sync_request"
    __table_args__ = (
        Index(
            "ix_policy_native_sync_request_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "ix_policy_native_sync_request_scope",
            "organization_id",
            "datasource_id",
            "schema_name",
            "table_name",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(255), nullable=False)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The generated `NativeStatement.as_dict()` list -- frozen at request time.
    statements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    row_policy_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    column_policy_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unsupported: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    request_reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decision_reason: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only on an APPLY_FAILED transition -- the exception class, never the raw
    # driver error text, which could carry source-side identifiers or values
    # (INV-6). The statements themselves stay auditable via `statements` above.
    apply_error: Mapped[str | None] = mapped_column(String(500))


class ColumnValueProfileArtifact(Base):
    """PR-2: the value-bearing artifact a `ProfilingExceptionPolicy` unlocks.

    Deliberately a *separate* table from the value-free `ColumnProfile` (never
    joined into it by default): everything here is real source data (an
    actual min/max and top-N actual values), it exists only for columns whose
    classification had an APPROVED, unrevoked policy at capture time, and it
    carries its own pinned `expires_at` so the background purge sweep
    (`profiling_exceptions.purge_expired_value_profile_artifacts`) can enforce
    the retention contract without touching the value-free profile at all.
    """

    __tablename__ = "column_value_profile_artifact"
    __table_args__ = (
        UniqueConstraint("column_profile_id"),
        Index("ix_column_value_profile_artifact_org_expires", "organization_id", "expires_at"),
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
    column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("column_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[UUID] = mapped_column(
        ForeignKey("profiling_exception_policy.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    classification: Mapped[str] = mapped_column(String(30), nullable=False)
    min_value: Mapped[str | None] = mapped_column(Text)
    max_value: Mapped[str | None] = mapped_column(Text)
    top_values: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    # DQ-8: origin discriminator. "INTERNAL" for Atlas's own deterministic
    # detectors (volume/null-rate/schema, custom rule packs, dbt bridge);
    # "EXTERNAL" for incidents reconciled from a third-party detector signal
    # (Monte Carlo, Anomalo, ...) via the external-signal ingest endpoint, so
    # externally-sourced and internally-computed signals are never conflated.
    source: Mapped[str] = mapped_column(String(30), default="INTERNAL", nullable=False)
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


class QualityRulePack(Base, TimestampMixin):
    """DQ-4: a named, schedulable group of custom threshold rules.

    Runs on its own cadence (``interval_minutes``), independent of the
    profiling scan that drives ``DataQualityObservation``/``evaluate_analysis_run``
    — the point of DQ-4's exit condition, "rules run outside scans".
    """

    __tablename__ = "quality_rule_pack"
    __table_args__ = (
        UniqueConstraint("datasource_id", "name"),
        Index("ix_quality_rule_pack_org_enabled", "organization_id", "enabled"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class QualityRule(Base, TimestampMixin):
    """A single deterministic, value-free threshold check within a rule pack.

    Evaluated against the most recent stored profile snapshot for its table
    (``TableProfile``/``ColumnProfile``) — never live source data — so a rule
    pack sweep stays value-free (INV-6) and needs no query-gateway execution.
    """

    __tablename__ = "quality_rule"
    __table_args__ = (
        CheckConstraint(
            "rule_type IN ('TABLE_ROW_COUNT_MIN', 'TABLE_ROW_COUNT_MAX', 'COLUMN_NULL_RATE_MAX')",
            name="ck_quality_rule_type",
        ),
        Index("ix_quality_rule_pack_enabled", "rule_pack_id", "enabled"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    rule_pack_id: Mapped[UUID] = mapped_column(
        ForeignKey("quality_rule_pack.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(30), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


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
    # AT-6: one entry per grounding fragment assembled into model context (the
    # retrieval hits selected in `agent_orchestrator.GovernedAgentOrchestrator.run`,
    # i.e. the same set `retrieval_evidence` above describes) --
    # {"object_type", "object_id", "fragment_digest", "annotation_version_id"}.
    # `fragment_digest` is a SHA-256 of the fragment's actual grounding content
    # (never the content itself -- value-free, matching `retrieval_evidence`).
    # `annotation_version_id` is set only for a `BUSINESS_ANNOTATION` fragment
    # and points at the exact `MetadataBusinessAnnotationVersion` row hashed, so
    # the run replays against that content even after a later approval
    # supersedes it. See `agent_run_replay.py`.
    grounding_fragment_digests: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    plan_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    recommended_tool_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governed_tool_version.id", ondelete="SET NULL"), index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(1000))
    # AG-10: which registered agent version (an `AGENT`-kind `AiAsset`'s
    # `AiAssetVersion`, carrying an `AgentContract`) this run executed as.
    # Nullable: a run invoked directly by a human principal with no agent
    # identity stays unlinked, and `aida.agent_roster` keeps reporting those
    # organization-wide. Set only by `GovernedAgentOrchestrator.run` when the
    # caller names an agent version, after that version's contract has been
    # loaded and its kill switch / envelope checked.
    ai_asset_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ai_asset_version.id", ondelete="SET NULL"), index=True
    )
    # AG-10 budget attribution. *Estimated* tokens, by the same
    # 4-bytes-per-token heuristic `ProviderNeutralModelGateway` enforces the
    # approved input cap against -- no provider adapter reports real usage, so
    # a column named `tokens_used` would be a precision claim the platform
    # cannot make. Summed across every attempt in the run (a failed fallback
    # attempt sent the same payload and so cost the same input estimate).
    # NULL means no model call happened -- a query-memory hit or a refusal
    # before generation -- which is different from a call that used zero.
    estimated_input_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_output_tokens: Mapped[int | None] = mapped_column(Integer)


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


class KillSwitchState(Base, TimestampMixin):
    """Module 15 kill switch (MG-2, ADR-0009 §7 of `20-modules/15-model-gateway.md`).

    Current engaged/released state, one mutable row per (organization, scope) --
    the same "current-state row, immutable history lives in AuditEvent/OutboxEvent"
    shape as `OrganizationIntegrationPolicy`, not an event-sourced table of its own.
    `route_key` holds the literal sentinel `"*"` (see
    `model_gateway.GLOBAL_KILL_SWITCH_SCOPE`) for an organization-wide switch that
    halts every route, or a specific route_key to halt only that route. Checked by
    `model_gateway.kill_switch_blocking_state` on every `structured_completion`
    call -- the single choke point through which all generation requests pass --
    so engaging it fails closed on the very next request, not eventually.
    """

    __tablename__ = "kill_switch_state"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "route_key", name="uq_kill_switch_state_organization_id_route_key"
        ),
        Index("ix_kill_switch_state_org_engaged", "organization_id", "engaged"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    route_key: Mapped[str] = mapped_column(String(100), nullable=False, default="*")
    engaged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000))
    engaged_by: Mapped[str | None] = mapped_column(String(255))
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[str | None] = mapped_column(String(255))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    __table_args__ = (
        Index("ix_governance_review_org_status", "organization_id", "status"),
        # NT-1's relay predicate: pending, not yet considered, oldest first.
        # Without it the sweep scans every review ever raised on every pass.
        Index(
            "ix_governance_review_notify_backlog",
            "status",
            "review_requested_notified_at",
            "created_at",
        ),
    )

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
    # --- ADR-0027 pre-review columns ------------------------------------
    # Written by `reviewer_agent.pre_review_pending`, read by the agent
    # inbox and by `auto_decide_tier0_tier1`. All nullable: a review that
    # has never been pre-reviewed is the pre-ADR-0027 shape exactly, and
    # every read site treats NULL as "no recommendation".
    risk_tier: Mapped[str | None] = mapped_column(String(2))
    pre_review_recommendation: Mapped[str | None] = mapped_column(String(20))
    pre_review_confidence: Mapped[float | None] = mapped_column(Float)
    pre_review_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pre_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pre_reviewed_by: Mapped[str | None] = mapped_column(String(255))
    # NT-1 watermark. Set by `governance_review_relay` once this review has
    # been considered for a REVIEW_REQUESTED notification -- whether it was
    # actually sent or skipped as too old to be news. It exists because the
    # review-creation path has 27 call sites and no single funnel, so the
    # notification is driven by a sweep over this column rather than by a
    # hook none of those sites share. Same shape as
    # `AssetCertification.expiry_warning_emitted_at`. NULL means "not yet
    # considered", which is what makes the sweep idempotent.
    review_requested_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


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


class RelationshipCandidateGroup(Base, TimestampMixin):
    """A composite (multi-column) FK-like candidate; see RelationshipCandidateGroupMember.

    ``RelationshipCandidate`` above is single-column only -- its unique
    constraint is keyed on exactly one source/target column pair, and the
    knowledge graph and impact-analysis code that reads it assumes the same.
    Rather than restructure that working, already-consumed shape, composite
    candidates get their own parent/member pair (RL-3): the parent carries
    the same maker-checker decision fields as ``RelationshipCandidate``, and
    an ordered set of column pairs lives in the member table below.
    """

    __tablename__ = "relationship_candidate_group"
    __table_args__ = (
        UniqueConstraint(
            "datasource_id",
            "member_fingerprint",
            name="uq_relationship_candidate_group_fingerprint",
        ),
        Index("ix_relationship_candidate_group_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RelationshipCandidateGroupMember(Base):
    """One ordered column pair belonging to a composite relationship candidate."""

    __tablename__ = "relationship_candidate_group_member"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "ordinal", name="uq_relationship_candidate_group_member_ordinal"
        ),
        UniqueConstraint(
            "group_id",
            "source_column_id",
            "target_column_id",
            name="uq_relationship_candidate_group_member_columns",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("relationship_candidate_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )


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
    # AU-8: no separate `index=True` here -- __table_args__ already declares
    # ix_composite_key_candidate_table on this column; a second, differently
    # named index over the same single column was drift with no migration.
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False
    )
    table_profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("table_profile.id", ondelete="SET NULL"), index=True
    )
    column_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    column_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    column_count: Mapped[int] = mapped_column(Integer, nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_distinctness_ratio: Mapped[float] = mapped_column(Float, nullable=False)
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


class CanonicalTableMapping(Base, TimestampMixin):
    """RL-2: steward override of which table-family member is canonical.

    ``TableFamilyCandidate`` (RL-1, shipped upstream) already carries
    ``base_table_id`` -- the algorithm's own "current/live" pick -- but that
    field is explicitly never set for a SNAPSHOT family (see that model's
    docstring): a run of dated full copies has no single member the
    algorithm can call canonical. This table is purely additive to
    ``TableFamilyCandidate``: it exists only to record an explicit steward
    decision, which is required to name a canonical member for a SNAPSHOT
    family and optional (but always wins) for any other family type. A row
    here only ever exists for a family a steward has actually decided; there
    is no row for the common "algorithm's pick stands, unreviewed" case.
    ``resolve_canonical`` (``aida.relationship_intelligence``) is the read
    path: this override if one exists, else ``base_table_id``, else
    ``None``.
    """

    __tablename__ = "canonical_table_mapping"
    __table_args__ = (
        UniqueConstraint(
            "family_candidate_id", name="uq_canonical_table_mapping_family_candidate"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    family_candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("table_family_candidate.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonical_table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resolved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    is_steward_override: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


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


# ---------------------------------------------------------------------------
# KG-5: saved Knowledge Graph / Graph Explorer perspectives
# ---------------------------------------------------------------------------


class GraphPerspective(Base, TimestampMixin):
    """A named, reusable snapshot of a caller's Graph Explorer view state.

    This is a thin persistence layer, not a governed object: there is no
    maker-checker review (unlike ``RelationshipCandidate``/
    ``CompositeKeyCandidate`` above) and no domain event is emitted for it --
    it is a personal/shared productivity artifact, the same tier as a saved
    search or a dashboard layout, not a lineage/quality/policy fact.

    ``view_state`` is an opaque, caller-defined JSON object: whatever shape
    the frontend Graph Explorer (``ui/scripts/graph-engine.js``) needs to
    reconstruct a view -- centered node, expansion depth, edge-kind filters,
    layout name, pan/zoom -- e.g.::

        {
          "centerNodeId": "b3f1...",
          "depth": 2,
          "edgeKinds": ["DECLARED_FOREIGN_KEY", "SUGGESTED_RELATIONSHIP"],
          "layout": "dagre",
          "zoom": 1.35,
          "pan": {"x": -120.0, "y": 40.0}
        }

    The server never parses or interprets it beyond "valid JSON object,
    bounded in size" (``schemas.GRAPH_PERSPECTIVE_MAX_VIEW_STATE_BYTES``) --
    it only stores/retrieves/authorizes it.

    Sharing reuses this codebase's one established sharing mechanism --
    role-based visibility via a JSON list of role names, the same shape as
    e.g. ``GovernedToolVersion.allowed_roles`` -- rather than inventing a
    user-to-user ACL system: an empty/absent ``allowed_viewer_roles`` means
    private to ``owner_principal`` only; a non-empty list additionally
    grants read access to any caller whose roles intersect it. Only the
    owner may update or delete a perspective; shared viewers are read-only.
    ``datasource_id`` is nullable because a perspective may describe a
    single datasource's subgraph or an org-wide cross-source view.
    """

    __tablename__ = "graph_perspective"
    __table_args__ = (
        Index("ix_graph_perspective_org_owner", "organization_id", "owner_principal"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    allowed_viewer_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    view_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


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
    """Identity/pointer row for a table's business annotation.

    AT-6: content (`business_name`, `business_description`, ...) used to live
    directly on this row and was mutated in place on every re-approval
    (`semantic_inference.apply_enrichment_proposal`'s old `else:` branch), which
    made it impossible to know what content an `AgentRun` was actually grounded
    on once a later approval overwrote it -- see
    `Docs/review-2026-08/atlan-context/00-decisions.md` §1. All authored content
    now lives on the append-only `MetadataBusinessAnnotationVersion` below,
    following the same parent-identity / versioned-content split as
    `AssetDocumentation`/`AssetDocumentationVersion` and
    `GlossaryTerm`/`GlossaryTermVersion`. This row keeps only the current
    domain/entity classification pointer and identity -- resolve content
    through the current (`status="APPROVED"`) version, or through a specific
    `MetadataBusinessAnnotationVersion.id` for replay of a past `AgentRun`.
    """

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


class MetadataBusinessAnnotationVersion(Base, TimestampMixin):
    """Append-only content history for a `MetadataBusinessAnnotation` (AT-6).

    One row per approved re-annotation. The previously `APPROVED` row (if any)
    is flipped to `SUPERSEDED` in the same transaction that inserts the new
    `APPROVED` row -- see `business_annotation_versions.write_annotation_version`
    -- never mutated for content. An `AgentRun.grounding_fragment_digests`
    entry for a `BUSINESS_ANNOTATION` retrieval hit records this row's id, so a
    run can be replayed against exactly this content even after a later
    approval supersedes it.
    """

    __tablename__ = "metadata_business_annotation_version"
    __table_args__ = (
        UniqueConstraint("annotation_id", "version"),
        Index(
            "ix_metadata_business_annotation_version_org_status",
            "organization_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    annotation_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_business_annotation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="APPROVED", nullable=False)
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
    # --------------------------------------------------------------------- #
    # Group K / AT-9: scope-aware term uniqueness.
    #
    # Uniqueness used to be a bare `(organization_id, term_key)` pair -- a
    # bank cannot govern one definition of "exposure" across Retail Banking
    # and Risk, so that shape structurally forbade the thing AT-9 exists to
    # allow: two independently governed definitions of the same term, each
    # scoped to a different node on the business graph (`business_node`,
    # ADR-0018's classification axis; N9's recursive CTE walk / closure
    # table), with a nullable enterprise-wide default when `business_node_id`
    # is NULL. Most-specific-wins resolution over that scope, and refusal
    # when two node-scoped definitions are equally applicable and neither is
    # an ancestor of the other, live in `semantic_inference.resolve_scoped_glossary_term`.
    #
    # Postgres unique constraints treat NULL as distinct per row, so a plain
    # 3-column `UniqueConstraint` would let multiple enterprise-default rows
    # (`business_node_id IS NULL`) coexist for the same term_key -- silently
    # reopening the ambiguity this row exists to close. Two constraints
    # instead: the composite one below covers every node-scoped pair, and a
    # partial-unique index (mirroring `ContextProductVersion`'s
    # `uq_context_product_version_one_published`, `postgresql_where` +
    # `sqlite_where` so the in-memory SQLite test harness enforces the same
    # rule) caps the enterprise default at exactly one row per term_key.
    # --------------------------------------------------------------------- #
    __tablename__ = "glossary_term"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "term_key",
            "business_node_id",
            name="uq_glossary_term_org_key_node",
        ),
        Index(
            "uq_glossary_term_org_key_enterprise_default",
            "organization_id",
            "term_key",
            unique=True,
            postgresql_where=text("business_node_id IS NULL"),
            sqlite_where=text("business_node_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    term_key: Mapped[str] = mapped_column(String(100), nullable=False)
    category_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("glossary_category.id", ondelete="SET NULL"), index=True
    )
    # Group K / AT-9: the business-graph node this definition is scoped to,
    # NULL for the enterprise-wide default. See the class docstring block
    # above for the uniqueness shape this participates in.
    business_node_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("business_node.id", ondelete="SET NULL"), nullable=True, index=True
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


class ColumnDocumentation(Base, TimestampMixin):
    """Identity/pointer row for one column's business description of record.

    The column-level counterpart to `AssetDocumentation` above, and the store
    `DocumentClaim`'s docstring named as missing: before this, an APPROVED
    column `DESCRIBES` claim's terminal state was the claim row itself, so a
    steward could approve a column description and no reader anywhere could
    then resolve it. `MetadataColumn.source_description` is a *different*
    thing and stays where it is -- that is the source system's own comment,
    overwritten by rediscovery; this is authored, reviewed content that
    rediscovery must never touch.

    Content lives on the append-only `ColumnDocumentationVersion` below, not
    here, following the same parent-identity / versioned-content split as
    `AssetDocumentation`/`AssetDocumentationVersion` and
    `MetadataBusinessAnnotation`/`MetadataBusinessAnnotationVersion`: an
    `AgentRun` grounded on a column description has to be replayable against
    exactly the content it saw, which in-place mutation would destroy.

    `table_id` is denormalized from `MetadataColumn.table_id` so the
    table-scoped and datasource-scoped reads (the column pane, the workbook
    export) can filter without a second join through `metadata_column`;
    `column_id` remains the unique key.
    """

    __tablename__ = "column_documentation"
    __table_args__ = (UniqueConstraint("column_id", name="uq_column_documentation_column_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False
    )


class ColumnDocumentationVersion(Base, TimestampMixin):
    """Append-only content history for a `ColumnDocumentation`.

    One row per approved description. The previously `APPROVED` row (if any)
    is flipped to `SUPERSEDED` in the same transaction that inserts the new
    `APPROVED` row -- see `column_documentation.publish_column_description` --
    never mutated for content.

    `source_claim_id` records which `DocumentClaim` a version was published
    from, so the description a reader resolves can always be traced back
    through the claim to the exact uploaded source text (`DocumentSection`)
    that asserted it. It is nullable because later authoring routes (a
    workbook re-import, a direct steward edit) will publish versions that did
    not come from a document claim.
    """

    __tablename__ = "column_documentation_version"
    __table_args__ = (
        UniqueConstraint("documentation_id", "version"),
        Index(
            "ix_column_documentation_version_org_status",
            "organization_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    documentation_id: Mapped[UUID] = mapped_column(
        ForeignKey("column_documentation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="APPROVED", nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_claim_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_claim.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DescriptionWithdrawal(Base, TimestampMixin):
    """A request to retire an approved description, routed through review.

    Publishing a description was governed from the first commit; un-publishing
    one was not possible at all -- a steward who approved a wrong column
    description had no way to take it back, and the workbook path deliberately
    refuses to read a blank cell as a deletion (an empty cell is
    indistinguishable from one nobody filled in). That left the only remedy as
    "publish a correction", which does not work when the right answer is that
    the platform should say nothing at all.

    Withdrawal is not a delete. The `ColumnDocumentationVersion` /
    `AssetDocumentationVersion` row keeps its content and moves to `WITHDRAWN`,
    so an `AgentRun` grounded on it stays replayable against exactly the text
    it saw -- the same append-only guarantee every other status transition on
    those tables preserves. What changes is that the current-version resolvers
    (which filter `status == "APPROVED"`) stop returning it, and the asset
    reads as undescribed again.

    Routed through `GovernanceReview` like every other governed change, with
    the same maker-checker rule: removing a description an agent may be
    grounding on is not a smaller decision than adding one.
    """

    __tablename__ = "description_withdrawal"
    __table_args__ = (
        UniqueConstraint("governance_review_id"),
        CheckConstraint(
            "subject_type IN ('TABLE', 'COLUMN')", name="withdrawal_subject_type_is_supported"
        ),
        Index("ix_description_withdrawal_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(10), nullable=False)
    #: The column or table whose description is being withdrawn.
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    subject_label: Mapped[str] = mapped_column(String(600), nullable=False)
    #: The exact version row this request was raised against. Re-checked at
    #: approval time: if a newer version has been published in between, the
    #: withdrawal no longer refers to the text anyone reviewed, and is refused
    #: rather than applied to content nobody looked at.
    version_id: Mapped[UUID] = mapped_column(nullable=False)
    withdrawn_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_REVIEW", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL")
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelImportBatch(Base, TimestampMixin):
    """One uploaded model workbook, awaiting or having had a review decision.

    The write half of the download/edit/re-upload round trip
    (`aida.model_export` is the read half). An upload never applies anything:
    it parses, diffs against current state, and records the differences as
    `ModelImportChange` rows for a steward to look at. Only an APPROVE
    decision on this batch's single `GovernanceReview` publishes them.

    One review per *batch*, not per change -- unlike `DocumentClaim`, which
    takes one review per claim because deciding a claim means reading its own
    source paragraph. A workbook's changes share one provenance (this file,
    this uploader) and are reviewed as one edit; at bulk scale, per-row
    reviews would be unusable, which is the whole reason this path exists
    alongside the document-claim one.

    `content_sha256` is over the uploaded bytes. It makes a re-upload of the
    identical file detectable, and ties an applied batch to exactly the file
    that was reviewed.
    """

    __tablename__ = "model_import_batch"
    __table_args__ = (
        UniqueConstraint("governance_review_id"),
        Index("ix_model_import_batch_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # DRAFT once parsed, PENDING_REVIEW once submitted, then APPLIED/REJECTED.
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL")
    )
    change_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applied_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Rows the diff could not turn into a change at all -- an unresolvable id,
    #: an edit to a read-only column, a sheet that is not in the workbook.
    #: Counted rather than dropped so an upload can never look cleaner than it
    #: was.
    rejected_row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelImportChange(Base, TimestampMixin):
    """One field on one object that an uploaded workbook would change.

    `expected_version` is the version number the workbook itself carried in
    its read-only `*_version` column at export time -- the version the person
    editing it was looking at. At apply time it is compared against the
    current version, and a change whose expectation no longer holds is
    SKIPPED_STALE rather than applied: someone else published in the window
    between export and upload, and silently overwriting them is the lost
    update this column exists to prevent.

    `old_value` is what the field held when the diff ran, kept so a reviewer
    can see what is being replaced without re-querying, and so an applied
    batch remains readable after the fact.
    """

    __tablename__ = "model_import_change"
    __table_args__ = (
        Index("ix_model_import_change_batch_status", "batch_id", "status"),
        CheckConstraint(
            "subject_type IN ('TABLE', 'COLUMN')", name="import_subject_type_is_supported"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_import_batch.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sheet_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 1-based row number in the uploaded sheet, so a reported problem can be
    #: found in the file the person is still looking at.
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    subject_label: Mapped[str] = mapped_column(String(600), nullable=False)
    field: Mapped[str] = mapped_column(String(50), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    expected_version: Mapped[int | None] = mapped_column(Integer)
    # PENDING -> APPLIED, or SKIPPED_STALE / SKIPPED_MISSING / REJECTED.
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(500))


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
    # Status values (P2-07):
    #   ACTIVE        -- currently in effect; the only value `_earliest_active_owners`
    #                    and every other policy-decision reader treats as "the owner".
    #   REASSIGNED    -- superseded by a GL-7 leaver reassignment (an operator-driven
    #                    successor now owns the subject).
    #   LAPSED        -- P2-07: `expires_at + grace_days` passed with no re-affirmation.
    #                    Written by `ownership_expiry_warning.expire_lapsed_ownership_assignments`
    #                    and by nothing else. Retained as evidence of who *used to*
    #                    own the subject; never read as the current owner.
    #   LAPSED_LEAVER -- P2-07: the owner principal was deleted (identity event) and
    #                    no successor was named in the merge event. Retained as
    #                    evidence; never the current owner. Written only by
    #                    `ownership_principal_lifecycle.handle_principal_deleted`.
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    assigned_by: Mapped[str] = mapped_column(String(255), nullable=False)
    # P2-07: re-affirmation cadence. Nullable because every pre-P2-07 row was
    # assigned once and never re-affirmed; the sweep skips rows with
    # `expires_at IS NULL` -- they carry no expiry until the first re-affirm
    # (or a fresh assignment under P2-07 code) sets one. New ACTIVE rows
    # written by `apply_bulk_operation` (ASSIGN_OWNERSHIP) get
    # `now + ownership_reaffirm_days`.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Idempotency stamp: written by `warn_upcoming_ownership_expiries` when it
    # emits the "expires in N days" notification, so the same row does not
    # warn twice inside one cycle.
    expiry_warning_emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Last time an owner (or admin) confirmed the assignment via the
    # `/reaffirm` endpoint; also extends `expires_at` by
    # `ownership_reaffirm_days`.
    reaffirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reaffirmed_by: Mapped[str | None] = mapped_column(String(255))


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
    # P2-08: partial unique index on the ACTIVE tuple is the last-mile atomicity
    # guarantee against two concurrent certify calls both landing an ACTIVE
    # row for the same (table, asset_type, column, organization) tuple. The
    # existing app-side "select prior ACTIVE, flip to SUPERSEDED, insert new"
    # is a read-modify-write with no lock between the read and the insert;
    # under a two-connection race both requests can read no-prior-active, both
    # can insert, and only a DB-level uniqueness constraint refuses the second
    # insert. `column_id` participates via COALESCE to the zero-UUID sentinel
    # since PostgreSQL treats NULL as distinct in a unique index (so two
    # concurrent table-level certifies with `column_id IS NULL` would otherwise
    # both slip past). Declared here for ORM/DDL parity; the alembic migration
    # ships the identical partial index server-side.
    __table_args__ = (
        Index("ix_asset_certification_org_status", "organization_id", "status"),
        Index(
            "ix_asset_certification_active_tuple",
            "table_id",
            "asset_type",
            text(
                "COALESCE(column_id, '00000000-0000-0000-0000-000000000000')"
            ),
            "organization_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
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
    # P2-08: manual revoke via POST /v1/tables/{id}/certification/revoke. These
    # three columns are nullable because every pre-P2-08 row was written by the
    # certify or supersede path and never revoked; a REVOKED row (produced only
    # by the new endpoint) sets all three atomically. `rationale` / `certified_by`
    # / `expires_at` stay exactly what the table was certified as, so
    # certification history is never mutated -- the same evidence-preservation
    # rule DQ-3's EXPIRED path already follows.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(255))
    revocation_reason: Mapped[str | None] = mapped_column(String(2000))
    # P2-08: daily warning job (`warn_upcoming_certification_expiries`) stamps
    # this whenever it emits the "expires in N days" notification for a row, so
    # the same cert never warns twice within one warning cycle. Nullable because
    # every row starts un-warned; the row is not backfilled by the migration.
    expiry_warning_emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # P3-09: structured evidence blob captured at certify time. Nullable
    # because every pre-P3-09 row was written with only the free-text
    # ``rationale`` and has no structured evidence to project; the new
    # ``compute_certification_evidence`` helper populates this on every new
    # write and the ``rationale`` column stays populated in parallel for
    # backward-compat human readability. Shape is validated by
    # ``aida.schemas.CertificationEvidence`` (description_version_id,
    # ownership_assignment_ids, quality_snapshot, glossary_term_ids,
    # supporting_dq_check_ids, certifier_notes) and, for legacy backfills, a
    # ``backfilled: true`` sentinel flag so readers can distinguish an
    # evidence blob captured at certify time from one reconstructed from
    # today's state.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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


class AssetDescriptionDraft(Base, TimestampMixin):
    """Deterministically drafted table description; always routed through review.

    Evidence-scored per GL-9: the score sets review priority, it never skips
    review. Rejected drafts are retained (not deleted) as negative knowledge so
    an identical low-value draft is not regenerated on the next run.
    """

    __tablename__ = "asset_description_draft"
    __table_args__ = (Index("ix_asset_description_draft_org_status", "organization_id", "status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    drafted_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    accuracy_score: Mapped[float] = mapped_column(Float, nullable=False)
    clarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    style_score: Mapped[float] = mapped_column(Float, nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL"), unique=True
    )
    published_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("asset_documentation_version.id", ondelete="SET NULL"), index=True
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
    # GL-6: second escalation tier. An entry still unaddressed this long after its
    # first (tier-1) escalation is escalated again, unconditionally through ITSM
    # regardless of what channel tier 1 used -- distinct from `escalated_at`, which
    # only ever records the tier-1 timestamp.
    escalated_tier2_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


#: ST-15: the one agreed lineage-edge ``kind`` vocabulary, matching the target set documented in
#: ``Docs/30-contracts/06-lineage-contract.md`` §2. Enforced at the database level by a CHECK
#: constraint on every ``edge_kind`` column below (migration ``d7b1e5a9c204``). NOTE: this is the
#: *lineage* edge kind -- how one asset derives from another -- and is deliberately distinct from
#: the *relationship/grant* edge-kind axis (``DECLARED_FOREIGN_KEY`` / ``SUGGESTED_RELATIONSHIP`` /
#: ``APPROVED_RELATIONSHIP_CANDIDATE``) carried by ``CrossBoundaryGrant.edge_kinds`` and the
#: unified-graph relationship payloads. Those are a different vocabulary for a different purpose
#: and are not constrained here; conflating the two is what made ST-15 read as a contradiction.
LINEAGE_EDGE_KINDS: tuple[str, ...] = (
    "QUERY",
    "VIEW",
    "PROCEDURE",
    "ETL",
    "DBT",
    "BI",
    "AI_DECISION",
)
_LINEAGE_EDGE_KIND_CHECK_SQL = (
    "edge_kind IN (" + ", ".join(f"'{k}'" for k in LINEAGE_EDGE_KINDS) + ")"
)


class OpenLineageTableEdge(Base, TimestampMixin):
    __tablename__ = "openlineage_table_edge"
    __table_args__ = (
        CheckConstraint(_LINEAGE_EDGE_KIND_CHECK_SQL, name="ck_openlineage_table_edge_edge_kind"),
        UniqueConstraint(
            "run_event_id",
            "input_dataset_namespace",
            "input_dataset_name",
            "output_dataset_namespace",
            "output_dataset_name",
            name="uq_openlineage_table_edge_run_input_output",
        ),
        Index("ix_openlineage_table_edge_run", "run_event_id"),
        # P1-05: parsed-edge review lifecycle -- default ACTIVE preserves the
        # pre-review-mode contract for every row already in the table and
        # every row still written under `auto_active` config; only when the
        # deployment flips `AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE` to
        # `require_review` does a new row land as PROPOSED and wait for
        # `parsed_lineage_review_api` to promote or reject it.
        Index("ix_openlineage_table_edge_review_status", "review_status"),
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
    # P1-05: parsed-edge review lifecycle -- see the module-level comment
    # on `OpenLineageColumnEdge` below and ADR-0026 for the full rationale.
    # Default ACTIVE keeps every existing row and every row written under
    # `auto_active` mode backward-compatible.
    review_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    previous_edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("openlineage_table_edge.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


class OpenLineageColumnEdge(Base, TimestampMixin):
    __tablename__ = "openlineage_column_edge"
    __table_args__ = (
        CheckConstraint(_LINEAGE_EDGE_KIND_CHECK_SQL, name="ck_openlineage_column_edge_edge_kind"),
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
        # P1-05: see OpenLineageTableEdge for the review-lifecycle rationale.
        Index("ix_openlineage_column_edge_review_status", "review_status"),
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
    # P1-05: parsed-edge review lifecycle -- see OpenLineageTableEdge.
    review_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    previous_edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("openlineage_column_edge.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


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
        # P1-05: see OpenLineageTableEdge for the review-lifecycle rationale.
        Index("ix_dbt_lineage_edge_review_status", "review_status"),
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
    # P1-05: parsed-edge review lifecycle -- see OpenLineageTableEdge.
    review_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    previous_edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("dbt_lineage_edge.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


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
            # Without a dialect-specific partial-index clause, SQLAlchemy only
            # applies `postgresql_where` when compiling for Postgres -- SQLite's
            # `Base.metadata.create_all()` (every in-memory test fixture in this
            # codebase) would otherwise compile this as a bare `UNIQUE(product_id)`,
            # wrongly forbidding a second, non-PUBLISHED version of the same
            # product from ever coexisting with a PUBLISHED one in a test.
            sqlite_where=text("status = 'PUBLISHED'"),
        ),
        CheckConstraint("version > 0", name="ck_context_product_version_positive"),
        CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPPORTED', 'SUPERSEDED', "
            "'REJECTED', 'DEPRECATION_REVIEW', 'DEPRECATED')",
            name="ck_context_product_version_status",
        ),
        CheckConstraint(
            "owner_type IN ('INDIVIDUAL', 'GROUP')",
            name="ck_context_product_version_owner_type",
        ),
        CheckConstraint(
            "support_window_days IS NULL OR support_window_days >= 0",
            name="ck_context_product_version_support_window_days",
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
    # AT-7(a)/AT-D1: a PUBLISHED version that is replaced no longer jumps straight
    # to fully-hidden SUPERSEDED -- it spends a support window as SUPPORTED, still
    # readable by a version-pinned consumer, before it is treated as retired.
    # `support_window_days` is the definition this *version* was submitted with
    # (so it travels with the version, like every other field); `None` means
    # "supported until explicit retirement" rather than a fixed duration.
    support_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set once, when this version stops being PUBLISHED (superseded by the next
    # approval). `None` while still PUBLISHED/DRAFT/etc. `support_window_ends_at`
    # is the derived deadline (`superseded_at + support_window_days`), or `None`
    # for both "not yet superseded" and "supported indefinitely".
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    support_window_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_version_id: Mapped[UUID | None] = mapped_column(
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


class ContextProductConsumerBinding(Base, TimestampMixin):
    """AT-7(b): pins one named consumer to one specific Context Product version.

    A staged rollout mechanism, not a blind percentage/weight A/B split (the
    tracker explicitly declines those): an operator moves individually-named
    consumers onto a new version one at a time, so some consumers stay on the
    prior version deliberately while others move, under explicit control
    rather than a random split. One binding per (product, consumer) --
    creating a new one for an already-bound consumer moves it, it does not
    duplicate it (`PUT`-shaped upsert, not append-only history).
    """

    __tablename__ = "context_product_consumer_binding"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "consumer_principal_id",
            name="uq_context_product_consumer_binding_product_consumer",
        ),
        Index(
            "ix_context_product_consumer_binding_org_product",
            "organization_id",
            "product_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consumer_principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    bound_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


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
        # AT-D2: without this, re-parsing the same view definition doubled
        # the graph on every call -- nothing stopped a blind insert of the
        # same edge on top of itself. `view_lineage_api.py` pairs this with
        # an application-level delete-then-insert scoped to the target
        # table(s) a parse actually produced edges for.
        UniqueConstraint(
            "datasource_id",
            "source_table",
            "source_column",
            "target_table",
            "target_column",
            "transformation_type",
            name="uq_view_lineage_edge_natural_key",
        ),
        # P1-05: see OpenLineageTableEdge for the review-lifecycle rationale.
        Index("ix_view_lineage_edge_review_status", "review_status"),
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
    # P1-05: parsed-edge review lifecycle -- see OpenLineageTableEdge.
    review_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    previous_edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("view_lineage_edge.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


class ProcedureLineageEdge(Base, TimestampMixin):
    """Column-level lineage edge extracted from a stored procedure body."""

    __tablename__ = "procedure_lineage_edge"
    __table_args__ = (
        Index("ix_procedure_lineage_edge_org_target", "organization_id", "target_table_id"),
        Index("ix_procedure_lineage_edge_datasource", "datasource_id"),
        # AT-D2: see the matching constraint on ViewLineageEdge.
        UniqueConstraint(
            "datasource_id",
            "source_table",
            "source_column",
            "target_table",
            "target_column",
            "transformation_type",
            name="uq_procedure_lineage_edge_natural_key",
        ),
        # P1-05: see OpenLineageTableEdge for the review-lifecycle rationale.
        Index("ix_procedure_lineage_edge_review_status", "review_status"),
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
    # P1-05: parsed-edge review lifecycle -- see OpenLineageTableEdge.
    review_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    previous_edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("procedure_lineage_edge.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


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


class StudioEvalQuestion(Base, TimestampMixin):
    """A regression question mined from real usage (ST-A8).

    Existence of a row is the evidence: it is only created when a consumption
    edge or BI dashboard binding shows the referenced metric/tool actually
    resolved for someone, so "does it still resolve" is the correct regression
    question to ask of every future change set touching that object. Value-free
    per ADR-0014 -- `evidence_edge_id` references the source
    `consumption_record` or `bi_report_metric_edge` row by id; no raw query
    text or result values are stored.
    """

    __tablename__ = "studio_eval_question"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "object_type",
            "object_id",
            name="uq_studio_eval_question_org_object",
        ),
        Index(
            "ix_studio_eval_question_org_object",
            "organization_id",
            "object_type",
            "object_id",
        ),
        CheckConstraint(
            "evidence_source IN ('CONSUMPTION', 'BI')",
            name="ck_studio_eval_question_evidence_source",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence_source: Mapped[str] = mapped_column(String(30), nullable=False)
    evidence_edge_id: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    mined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class StudioEvalRun(Base, TimestampMixin):
    """One regression-gate execution against a change set's mined eval corpus."""

    __tablename__ = "studio_eval_run"
    __table_args__ = (
        Index("ix_studio_eval_run_change_set", "change_set_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # AU-8: no separate `index=True` here -- __table_args__ already declares
    # ix_studio_eval_run_change_set with this column leading, and no migration
    # ever created a second single-column index; the ORM declaration was drift.
    change_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("studio_change_set.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class StudioEvalResult(Base, TimestampMixin):
    """Per-question outcome of one eval run -- the regression proof for a
    single mined question, kept even after the run's aggregate result."""

    __tablename__ = "studio_eval_result"
    __table_args__ = (Index("ix_studio_eval_result_run", "eval_run_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    eval_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("studio_eval_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    eval_question_id: Mapped[UUID] = mapped_column(
        ForeignKey("studio_eval_question.id", ondelete="CASCADE"), nullable=False, index=True
    )
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


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
    # AU-8: no separate `index=True` here -- __table_args__ already declares
    # ix_notification_event_incident on this column; a second, differently
    # named index over the same single column was drift with no migration.
    # NT-1 (2026-09-04) made both nullable. This table was built for DQ-1,
    # where every notification is about an incident matched by a rule. A
    # governance notification -- an approval request, a kill switch, a
    # certification about to lapse -- has neither, and the alternative was a
    # second near-identical delivery ledger, which is the duplication this
    # platform's own research names as a thing not to build. A row with a
    # NULL incident is a governance notification; a row with one is DQ-1's.
    incident_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_quality_incident.id", ondelete="CASCADE")
    )
    rule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("notification_rule.id", ondelete="CASCADE"), index=True
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


class ExternalQualitySignal(Base):
    """DQ-8: an immutable, normalized quality signal ingested from a third-party
    detector (Monte Carlo, Anomalo, ...).

    Atlas deliberately does not compete on detection science (module 11 §4); this
    is the "open framework" seam that lets best-of-breed detectors feed the same
    incident lifecycle Atlas's own controls use, while staying *distinguishable*
    from internally-computed signals -- both by living in this dedicated table and
    by the ``DataQualityIncident.source == "EXTERNAL"`` marker the reconciliation
    stamps on the incident it opens/reopens/resolves.

    Value-free (INV-6/ADR-0014): this row stores only detector metadata, refs and
    a normalized severity/state -- never source row values. ``details`` is an
    opaque, caller-supplied metadata blob and is validated at the API boundary to
    carry no raw business values.

    Idempotent on ``(organization_id, detector_vendor, detector_native_id,
    observed_at)``: re-delivering the same detector event (at-least-once webhooks)
    returns the already-stored signal instead of duplicating it or re-opening the
    incident a second time.
    """

    __tablename__ = "external_quality_signal"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "detector_vendor",
            "detector_native_id",
            "observed_at",
            name="uq_external_quality_signal_dedup",
        ),
        Index("ix_external_quality_signal_source_created", "datasource_id", "created_at"),
        Index("ix_external_quality_signal_org_vendor", "organization_id", "detector_vendor"),
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
    column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), index=True
    )
    incident_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_quality_incident.id", ondelete="SET NULL"), index=True
    )
    detector_vendor: Mapped[str] = mapped_column(String(50), nullable=False)
    detector_native_id: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    signal_status: Mapped[str] = mapped_column(String(30), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
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
        CheckConstraint(_LINEAGE_EDGE_KIND_CHECK_SQL, name="ck_bi_report_metric_edge_edge_kind"),
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
        CheckConstraint(_LINEAGE_EDGE_KIND_CHECK_SQL, name="ck_bi_metric_column_edge_edge_kind"),
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
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)


class MetadataPlaybook(Base, TimestampMixin):
    """AT-1: a saved, scheduled bulk-metadata action -- a filter, a CT-1 action
    (TAG/CLASSIFY/OWN/CERTIFY), and a schedule, run automatically by the fleet
    scheduler rather than a bespoke one (this row's own exit condition).

    Matches at or below `auto_apply_max_items` are applied immediately through
    the exact same single-item cores CT-1's synchronous endpoints use
    (`aida.catalog_bulk_actions.apply_*_item`), recorded as a
    `CatalogBulkActionRun` with `selection_mode="PLAYBOOK_AUTO"`. A match count
    above that threshold is not applied directly -- it is queued as a
    `BulkStewardshipOperation` behind a `GovernanceReview`, mirroring GL-2's
    (auto-apply is safe at small scale) and GL-5's (review at larger blast
    radius) precedent for exactly this kind of scale-dependent risk. `describe`
    is a named action in this row's own exit text but has no existing CT-1
    single-item core to reuse yet -- honestly out of scope for this pass; see
    the tracker row's own note.
    """

    __tablename__ = "metadata_playbook"
    __table_args__ = (
        UniqueConstraint("organization_id", "name"),
        Index("ix_metadata_playbook_org_enabled", "organization_id", "enabled"),
        CheckConstraint(
            "action IN ('TAG', 'CLASSIFY', 'OWN', 'CERTIFY')",
            name="action_is_supported",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    match_field: Mapped[str] = mapped_column(String(20), default="TABLE_NAME", nullable=False)
    match_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    # CLASSIFY only -- which columns of each matched table to reclassify.
    # Unused (left NULL) by TAG/OWN/CERTIFY, which act on the whole table.
    column_name_pattern: Mapped[str | None] = mapped_column(String(255))
    # Action-specific fields CT-1's own per-action request shape already
    # defines (tag_key/tag_value, classification, owner_type/owner_principal,
    # rationale/expires_after_days) -- validated against that shape at create
    # time (`playbooks_api.py`), not by a database constraint, the same
    # division of responsibility `NotificationRuleRecord.conditions` already
    # has between its own JSON column and the engine that reads it.
    action_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    schedule_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # <= this many matches: applied immediately. > this many: queued for
    # governance review instead. Zero means "always review, never auto-apply."
    auto_apply_max_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


# ---------------------------------------------------------------------------
# SM-4: Metric suggestions from approved annotations
# ---------------------------------------------------------------------------


class SemanticMetricProposal(Base, TimestampMixin):
    """A candidate metric definition proposed from real, already-approved
    evidence: a `MetadataBusinessAnnotation` (approved business context on a
    table) plus a numeric column on that table whose name matches a known
    measure-keyword vocabulary (`metric_suggestion_service.MEASURE_KEYWORDS`).

    Mirrors GL-8's `GlossaryLinkProposal` evidence-inference shape (an
    inferred candidate, not a steward assertion, so it starts `DRAFT` rather
    than `PENDING_APPROVAL`) and GL-9's evidence-scored review gate
    (`metric_suggestion_service.score_evidence` / `ensure_reviewable`): the
    score sets review priority and gates submission, it never skips
    independent review.

    Approval (`metric_suggestion_service.apply_metric_suggestion_proposal`,
    called only from `semantic_api.decide_governance_review`) publishes a
    real `SemanticMetric` + `SemanticMetricVersion` -- see that function's
    docstring for how it satisfies `SemanticMetricVersion`'s mandatory
    `semantic_model_version_id` FK without bundling the new metric into an
    unrelated model version's own review.
    """

    __tablename__ = "semantic_metric_proposal"
    __table_args__ = (
        UniqueConstraint(
            "table_id",
            "measure_column_id",
            "source_annotation_id",
            name="uq_semantic_metric_proposal_evidence",
        ),
        Index("ix_semantic_metric_proposal_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    measure_column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_annotation_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_business_annotation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposed_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    proposed_name: Mapped[str] = mapped_column(String(200), nullable=False)
    proposed_description: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_aggregation: Mapped[str] = mapped_column(String(30), nullable=False)
    proposed_grain: Mapped[str] = mapped_column(String(1000), nullable=False)
    accuracy_score: Mapped[float] = mapped_column(Float, nullable=False)
    clarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    style_score: Mapped[float] = mapped_column(Float, nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL"), unique=True
    )
    published_metric_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("semantic_metric_version.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Group K / AT-12: semantic mining of warehouse query history.
#
# A candidate metric mined from value-free query-log structure (an
# aggregation over a measure column, grouped by a set of grain columns, seen
# repeatedly across the query log) has no `SemanticMetricProposal`-shaped
# evidence -- that model requires a `source_annotation_id` pointing at an
# approved `MetadataBusinessAnnotation` (SM-4's evidence source) and carries
# NL-description-quality scores (accuracy/clarity/style/completeness) that do
# not apply to a candidate whose only evidence is "this shape recurred N
# times in the query log". Reusing it would misrepresent provenance, so this
# is the "clearly-scoped new candidate type" the AT-12 tracker row
# anticipates when the existing model can't represent a metric candidate.
#
# Lands in the SAME unified maker-checker queue every other model-judgement
# candidate on this platform uses (`GovernanceReview`, object_type
# `QUERY_HISTORY_METRIC_CANDIDATE`) rather than a parallel one -- see
# `aida.query_history_miner.submit_query_history_metric_candidate` /
# `apply_query_history_metric_candidate_decision`, dispatched from
# `semantic_api._apply_governance_review_decision` exactly the way AT-11's
# `COLUMN_CLASSIFICATION_PROMOTION` branch is. AT-C2 lane 3 (model
# judgements are proposal-only under a 0.70 confidence cap, maker != checker)
# applies: `confidence` here is enforced <= `QUERY_HISTORY_CONFIDENCE_CAP` in
# `query_history_miner.py`, never asserted directly.
# ---------------------------------------------------------------------------


class QueryHistoryMetricCandidate(Base, TimestampMixin):
    __tablename__ = "query_history_metric_candidate"
    __table_args__ = (
        UniqueConstraint(
            "table_id",
            "measure_column_id",
            "aggregation",
            "grain_fingerprint",
            name="uq_query_history_metric_candidate_shape",
        ),
        Index("ix_query_history_metric_candidate_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    measure_column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    aggregation: Mapped[str] = mapped_column(String(30), nullable=False)
    # Grain columns are stored as an ordered list of `MetadataColumn` id
    # strings (mirrors `SemanticMetricVersion.allowed_dimension_column_ids`);
    # `grain_fingerprint` is a stable hash of that ordered list so the unique
    # constraint above can key on it without a JSON-in-unique-constraint
    # dependency (see `query_history_miner.grain_fingerprint`).
    grain_column_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    grain_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    detection_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL"), unique=True
    )
    published_metric_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("semantic_metric_version.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# QG-6: dynamic masking / tokenization integration
# ---------------------------------------------------------------------------


class ColumnTokenizationPolicy(Base, TimestampMixin):
    """Declares that one catalog column gets tokenized rather than fully redacted.

    `query_gateway.py`'s masking pass already has a conservative default: a
    sensitive output column (module 05 classification in
    `aida.classification.SENSITIVE_CLASSES`) is replaced with the literal
    ``"***MASKED***"`` string. A row here for a given `column_id` is an
    explicit steward decision to *narrow* that default for one column -- the
    value becomes a reversible, format-preserving token
    (`aida.tokenization.TokenizationProvider`) instead of a full redaction, so
    a downstream workflow that genuinely needs the original value back can get
    it through the gated, audited detokenize endpoint
    (`aida.detokenization_api.detokenize_value`) rather than bypassing the
    gateway.

    Scoped to one `column_id` (`aida.models.MetadataColumn`, not a name string)
    so the policy travels with the catalog's own identity for that column --
    the same de-duplicated, table-qualified reference every other per-column
    governance construct in this module uses, rather than a bare name that
    would collide across tables with a same-named column.

    No `strategy` field: existence of an enabled row *is* "tokenize this
    column"; there is deliberately no third state distinct from "no row"
    (redact, the existing conservative default) and "disabled" (a policy that
    was configured and then explicitly turned back off, kept for its audit
    trail rather than deleted) other than the `enabled` flag itself.
    """

    __tablename__ = "column_tokenization_policy"
    __table_args__ = (
        UniqueConstraint("column_id", name="uq_column_tokenization_policy_column"),
        Index(
            "ix_column_tokenization_policy_org_datasource",
            "organization_id",
            "datasource_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "NUMERIC" today (credit-card numbers, SSNs, account and phone numbers --
    # `aida.tokenization`'s Feistel-style construction only transforms the
    # digit run of a value). Left open rather than a bare boolean so a second
    # value shape (e.g. an alphanumeric account identifier) can be declared
    # later without a schema change; an unsupported shape is a matter for the
    # tokenization provider to refuse, not this table to validate.
    value_shape: Mapped[str] = mapped_column(String(20), default="NUMERIC", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# N8: document ingestion -- the data-dictionary-spreadsheet special case
# ---------------------------------------------------------------------------


class Document(Base, TimestampMixin):
    """N8: an uploaded document, scoped to project. `media_type` is
    constrained to `CSV` for this first pass -- the data-dictionary special
    case `Docs/review-2026-08/target/01-metadata-graph-wiki.md` §3 names as
    the highest-value case ("this one case covers a large share of real bank
    documents") -- PDF/DOCX/XLSX structure-preserving extraction is a real,
    separate build (a parsing library this environment does not carry) and is
    honestly deferred, not attempted here.

    Raw file bytes are deliberately never persisted here (the design brief's
    own "stored in object storage, never in a table" -- no object-storage
    integration exists in this codebase to route through yet, so the
    honest choice is to hold nothing rather than fake compliance with a
    plain-table `raw_bytes` column). Only `sha256` (proving what was
    processed, without holding it) and the resulting `DocumentSection` rows
    (each holding its own already-extracted, already-small field text)
    survive past the parse that happens at upload time.
    """

    __tablename__ = "document"
    __table_args__ = (
        Index("ix_document_org_project", "organization_id", "project_id"),
        CheckConstraint("media_type IN ('CSV')", name="media_type_is_supported"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="UPLOADED", nullable=False)
    section_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parse_error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)


class DocumentSection(Base, TimestampMixin):
    """N8: one ordered, addressable row of a parsed data-dictionary document
    -- the object model's `document_section`, narrowed for this pass to the
    structured `schema | table | column | description` shape rather than the
    general page/heading/anchor prose model a PDF/DOCX parse would need.
    `column_name` is nullable: a row naming only a schema and table is a
    table-level description claim.

    Every `DocumentMapping`/`DocumentClaim` this section produces carries
    this row's id, so a steward can always click through from an approved
    claim back to the exact source row it came from -- the design brief's
    "cite" step (§3.5), satisfied structurally even though this pass builds
    no UI to render the click-through itself.
    """

    __tablename__ = "document_section"
    __table_args__ = (UniqueConstraint("document_id", "ordinal"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_schema_name: Mapped[str | None] = mapped_column(String(255))
    raw_table_name: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_column_name: Mapped[str | None] = mapped_column(String(255))
    raw_description: Mapped[str] = mapped_column(Text, nullable=False)


class DocumentMapping(Base, TimestampMixin):
    """N8: the result of resolving one `DocumentSection` against the live
    catalog -- the object model's `document_mapping`.

    `mapping_kind` is `STRUCTURAL` (deterministic exact-name match, this
    pass's only implemented route) or `UNMATCHED` (no candidate found).
    `SUGGESTED` (semantic/embedding-similarity mapping, per the design
    brief's third route) is deliberately not built here: N5 (hybrid
    retrieval) owns this codebase's one embedding-provider integration and is
    itself still IN PROGRESS on a different worktree -- reusing it correctly
    is a follow-on pass, not a good place to fork a second one from this row.
    A `STRUCTURAL` match is never ambiguous by construction: `resolve_
    structural_mappings` records `UNMATCHED` rather than guessing whenever a
    section's names resolve to more than one live candidate.
    """

    __tablename__ = "document_mapping"
    __table_args__ = (
        UniqueConstraint("document_section_id"),
        CheckConstraint("subject_type IN ('TABLE', 'COLUMN')", name="subject_type_is_supported"),
        CheckConstraint(
            "mapping_kind IN ('STRUCTURAL', 'SUGGESTED', 'UNMATCHED')",
            name="mapping_kind_is_supported",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_section_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_section.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(100), index=True)
    mapping_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)


class DocumentClaim(Base, TimestampMixin):
    """N8: an extracted, reviewable assertion from a mapped document section
    -- the object model's `claim`. `predicate` is constrained to `DESCRIBES`
    for this pass (a column-or-table description, the case the sample
    "schema | table | column | description" dictionary shape actually
    produces); `grain`/`pii`-shaped claims the design brief also names are a
    later pass over the same table, not built here.

    Routed through the existing unified `GovernanceReview` queue exactly like
    every other proposal on this platform (`semantic_api.
    decide_governance_review`, `object_type="DOCUMENT_CLAIM"`) -- one review
    per claim, since a steward deciding a claim needs to read its specific
    source section text, not a batch of unrelated ones.

    An `APPROVED` claim used to terminate at this row: when N8 landed there
    was no column-level "business description of record" store to publish
    into, so approval moved a status and nothing could read the result.
    `ColumnDocumentation`/`ColumnDocumentationVersion` is now that store, and
    `document_ingestion.apply_document_claim` publishes on approval -- a
    COLUMN claim into `ColumnDocumentationVersion`, a TABLE claim into the
    `AssetDocumentationVersion` GL-9 also writes to. `source_claim_id` on the
    published version points back here, so a resolved description traces to
    the exact `DocumentSection` text that asserted it.

    Still not claimed: that an approved claim propagates everywhere
    "description" might be read from. It reaches the two description stores
    named above; N10's knowledge-compilation wiki remains later, larger work.
    """

    __tablename__ = "document_claim"
    __table_args__ = (
        UniqueConstraint("governance_review_id"),
        CheckConstraint("subject_type IN ('TABLE', 'COLUMN')", name="subject_type_is_supported"),
        CheckConstraint("predicate IN ('DESCRIBES')", name="predicate_is_supported"),
        Index("ix_document_claim_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_section_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_section.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(20), nullable=False)
    object_value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL")
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# --- GROUP H: ST-A7 context product builder ---
# ---------------------------------------------------------------------------


class StudioContextProductMaterialization(Base, TimestampMixin):
    """Traceability link from a Studio CONTEXT_PRODUCT change-set item (ST-A7)
    to the real module-19 `ContextProduct`/`ContextProductVersion` and
    `GovernanceReview` it produced on submission (`aida.studio_context_product`).

    A new row per change item that successfully materializes -- the shared
    `StudioChangeItem`/`StudioChangeSet` models gain no columns for this;
    this table is the append-only evidence trail proving a specific Studio
    change-set item produced a specific governed object, routed through the
    exact same maker-checker queue a directly-authored context product uses
    (`decide_governance_review`'s `CONTEXT_PRODUCT_VERSION` branch in
    `semantic_api.py`) -- never bypassed or reimplemented here.
    """

    __tablename__ = "studio_context_product_materialization"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'DELETE')",
            name="ck_studio_cp_materialization_operation",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    change_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("studio_change_set.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("studio_change_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    context_product_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product.id", ondelete="CASCADE"), nullable=False, index=True
    )
    context_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    governance_review_id: Mapped[UUID] = mapped_column(
        ForeignKey("governance_review.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# --- AG-10: agent contract, AgentRun attribution and the agent task ledger ---
# ---------------------------------------------------------------------------
# `Docs/00-product/08-market-deep-dive-and-target-architecture-2026-09.md`
# section 4.2 ("The agent contract"). Two additive schema changes make the
# contract real: the `AgentRun.ai_asset_version_id` link the roster module
# (`aida.agent_roster`) documents as missing, and the `agent_task` ledger
# below. `AgentContract` itself is the governed declaration -- identity,
# capability envelope, autonomy tier, budget, eval gate, supervisor and
# kill scope -- keyed one-to-one to an `AGENT`-kind `AiAssetVersion`.

AGENT_AUTONOMY_TIERS = ("T0", "T1", "T2", "T3")
AGENT_SUPERVISOR_PERSONAS = (
    "ANALYST",
    "CONSUMER",
    "STEWARD",
    "REVIEWER",
    "OPERATOR",
    "AUDITOR",
)
AGENT_KILL_SCOPES = ("AGENT", "TIER", "ALL")
AGENT_WRITE_LANES = ("MEASURED_FACT", "PLATFORM_OBSERVATION", "MODEL_JUDGEMENT_PROPOSAL")
AGENT_TASK_STATUSES = ("PROPOSED", "APPLIED", "REJECTED", "FAILED", "SAMPLED")
AGENT_TASK_AUDIT_OUTCOMES = ("PENDING", "AGREED", "DISAGREED")
#: ADR-0027 (proposed, section 5.5 of the deep dive): every auto-applied
#: item is sampled to a human at a configurable rate with a floor of 5%.
AGENT_SAMPLING_RATE_FLOOR = 0.05


class AgentContract(Base, TimestampMixin):
    """The governed contract for one registered agent version (AG-10).

    One row per `AiAssetVersion` of an `AiAsset` whose `asset_kind` is
    `AGENT` (enforced app-side in `aida.agent_contracts`, since the kind
    lives on the parent asset). `agent_principal_id` is the agent's own
    workload identity -- distinct from every human principal, never equal
    to the human who authored the contract (INV-8: the identity that makes
    a proposal is never the identity that checks it, and an agent that
    borrowed its supervisor's identity would collapse that distinction).
    `capability_envelope` is value-free configuration (`tool_slugs`,
    `context_product_ids`, `write_lanes`) that `GovernedAgentOrchestrator.run`
    enforces on every linked run. `kill_engaged` is the current-state
    half of the per-agent kill switch, same shape as `KillSwitchState`:
    the immutable engage/release history lives in `AuditEvent`.
    """

    __tablename__ = "agent_contract"
    __table_args__ = (
        UniqueConstraint("ai_asset_version_id", name="uq_agent_contract_ai_asset_version_id"),
        Index("ix_agent_contract_org_principal", "organization_id", "agent_principal_id"),
        Index("ix_agent_contract_org_kill", "organization_id", "kill_engaged"),
        CheckConstraint(
            "autonomy_tier IN ('T0', 'T1', 'T2', 'T3')", name="ck_agent_contract_autonomy_tier"
        ),
        CheckConstraint(
            "supervisor_persona IN ('ANALYST', 'CONSUMER', 'STEWARD', 'REVIEWER', "
            "'OPERATOR', 'AUDITOR')",
            name="ck_agent_contract_supervisor_persona",
        ),
        CheckConstraint(
            "kill_scope IN ('AGENT', 'TIER', 'ALL')", name="ck_agent_contract_kill_scope"
        ),
        CheckConstraint("sampling_rate >= 0.05", name="ck_agent_contract_sampling_rate_floor"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ai_asset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_asset_version.id", ondelete="CASCADE"), nullable=False
    )
    agent_principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    capability_envelope: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    autonomy_tier: Mapped[str] = mapped_column(String(2), default="T0", nullable=False)
    daily_token_cap: Mapped[int | None] = mapped_column(Integer)
    per_run_token_cap: Mapped[int | None] = mapped_column(Integer)
    wall_clock_seconds_cap: Mapped[int | None] = mapped_column(Integer)
    eval_gate_threshold: Mapped[float | None] = mapped_column(Float)
    supervisor_persona: Mapped[str] = mapped_column(String(20), nullable=False)
    kill_scope: Mapped[str] = mapped_column(String(10), default="AGENT", nullable=False)
    kill_engaged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sampling_rate: Mapped[float] = mapped_column(
        Float, default=AGENT_SAMPLING_RATE_FLOOR, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


#: Terminal/in-flight states for `AgentContractRequest.status`. `PENDING` is
#: the only state a `GovernanceReview` decision may act on (enforced by the
#: generic PENDING-only guard every review decision already applies);
#: `ACTIVATED` means the eval gate passed and the contract was written;
#: `REJECTED` means a human declined it; `EVAL_BLOCKED` is currently unused
#: by the write path (see `agent_contract_request_api`'s module docstring for
#: why a failed eval gate raises rather than lands here) but is kept in the
#: allowlist so a future row can distinguish "declined" from "not yet
#: eval-ready" without a migration.
AGENT_CONTRACT_REQUEST_STATUSES = ("PENDING", "ACTIVATED", "REJECTED", "EVAL_BLOCKED")


class AgentContractRequest(Base, TimestampMixin):
    """AG-10 self-service extension: a *proposed* agent contract, submitted by
    a trusted-but-not-unilateral principal (an `AgentDeveloper`, typically —
    see `agent_contract_request_api.AGENT_CONTRACT_REQUEST_SUBMITTERS`) and
    activated only after both a human governance decision (maker != checker,
    the existing `GovernanceReview` queue every other proposal in this
    codebase already uses) AND a passing AT-8/N17 evaluation gate
    (`aida.agent_eval_gate.compute_agent_eval_gate`) at decision time.

    This does not replace `AgentContract`'s existing direct-write path
    (`PUT .../agents/{version}/contract`, still available to
    `PlatformAdmin`/`AgentDeveloper`/`ModelRiskManager` for corrections) — it
    adds a *reviewed, eval-gated* path alongside it, which is what makes a
    contract change from an external or newly-onboarded agent developer
    something the platform actually checked rather than something it merely
    accepted.

    `definition` stores exactly the fields `agent_contracts.
    AgentContractDefinition` needs to reconstruct itself
    (`capability_envelope`/`autonomy_tier`/`supervisor_persona`/`kill_scope`/
    `sampling_rate`/the three caps/`eval_gate_threshold`) as one JSON blob,
    the same "one proposal, one payload" shape every other reviewed proposal
    in this codebase stores next to its own `GovernanceReview` row (there is
    no shared payload column on `GovernanceReview` itself — see that model's
    own fields).
    """

    __tablename__ = "agent_contract_request"
    __table_args__ = (
        Index("ix_agent_contract_request_org_status", "organization_id", "status"),
        Index("ix_agent_contract_request_version", "ai_asset_version_id"),
        CheckConstraint(
            "status IN ('PENDING', 'ACTIVATED', 'REJECTED', 'EVAL_BLOCKED')",
            name="ck_agent_contract_request_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ai_asset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_asset_version.id", ondelete="CASCADE"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL")
    )
    eval_gate_verdict: Mapped[str | None] = mapped_column(String(20))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTask(Base, TimestampMixin):
    """One unit of agent work (AG-10): intent, value-free inputs fingerprint,
    the proposal it produced (if any), its status and the sampled-audit
    outcome. Written by `aida.agent_tasks.record_agent_task` /
    `finish_agent_task`; every `GovernedAgentOrchestrator.run` produces
    exactly one row. `inputs_fingerprint` is a SHA-256 over a canonical,
    value-free payload (ids, hashes and parameter *names* -- never a source
    value, a question, or a parameter value; INV-6). `evidence` carries the
    same discipline. `agent_run_id` ties an orchestrator-produced task back
    to its `AgentRun`; `proposal_ref_type`/`proposal_ref_id` name the
    governed object (typically a `GovernanceReview`) the task proposed.
    """

    __tablename__ = "agent_task"
    __table_args__ = (
        Index("ix_agent_task_org_started", "organization_id", "started_at"),
        Index("ix_agent_task_org_status", "organization_id", "status"),
        Index("ix_agent_task_org_sampled", "organization_id", "sampled_for_audit"),
        CheckConstraint(
            "status IN ('PROPOSED', 'APPLIED', 'REJECTED', 'FAILED', 'SAMPLED')",
            name="ck_agent_task_status",
        ),
        CheckConstraint(
            "audit_outcome IS NULL OR audit_outcome IN ('PENDING', 'AGREED', 'DISAGREED')",
            name="ck_agent_task_audit_outcome",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ai_asset_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ai_asset_version.id", ondelete="SET NULL"), index=True
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_run.id", ondelete="SET NULL"), index=True
    )
    agent_principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    intent: Mapped[str] = mapped_column(String(100), nullable=False)
    inputs_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_ref_type: Mapped[str | None] = mapped_column(String(100))
    proposal_ref_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="PROPOSED", nullable=False)
    sampled_for_audit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    audit_outcome: Mapped[str | None] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


# ---------------------------------------------------------------------------
# --- ADR-0027: risk-tiered agent checking (reviewer agent) ---
# ---------------------------------------------------------------------------
# `GovernanceReview` gains the pre-review columns below rather than a
# side table, because every one of them is a property *of that review*
# and every read of the review queue wants them in the same row. The
# sampled-audit ledger is separate (`ReviewAuditSample`) because a sample
# has its own lifecycle: it outlives the decision and is resolved by a
# different principal at a different time.

REVIEW_PRE_REVIEW_RECOMMENDATIONS = ("APPROVE", "REJECT", "NONE")
REVIEW_AUDIT_SAMPLE_OUTCOMES = ("PENDING", "AGREED", "DISAGREED")


class ReviewAuditSample(Base, TimestampMixin):
    """One agent decision the deterministic sampler routed to a human.

    ADR-0027 condition (b): every agent-approved item is sampled at a
    configurable rate with a 5% floor. A row here is the open question
    "was the agent right?"; `human_outcome` is the answer, and the
    DISAGREED rate per object type is the metric ADR-0027's revisit
    trigger watches.
    """

    __tablename__ = "review_audit_sample"
    __table_args__ = (
        Index("ix_review_audit_sample_org_outcome", "organization_id", "human_outcome"),
        UniqueConstraint("governance_review_id", name="uq_review_audit_sample_review"),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')", name="ck_review_audit_sample_decision"
        ),
        CheckConstraint(
            "human_outcome IN ('PENDING', 'AGREED', 'DISAGREED')",
            name="ck_review_audit_sample_outcome",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    governance_review_id: Mapped[UUID] = mapped_column(
        ForeignKey("governance_review.id", ondelete="CASCADE"), nullable=False
    )
    agent_principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    object_type: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(2), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    human_outcome: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    human_principal_id: Mapped[str | None] = mapped_column(String(255))
    human_rationale: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewerAgentState(Base, TimestampMixin):
    """Per-organization suspension state (ADR-0027 condition (c)).

    Separate from the process-wide `reviewer_agent_suspended` setting so a
    single human action can stop one organization's agent decisions without
    a deployment and without affecting any other tenant.
    """

    __tablename__ = "reviewer_agent_state"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_reviewer_agent_state_org"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False
    )
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suspended_by: Mapped[str | None] = mapped_column(String(255))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspension_reason: Mapped[str | None] = mapped_column(Text)
