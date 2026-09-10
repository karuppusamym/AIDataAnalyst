"""profiling -- PRIVATE. SQLAlchemy models in this module's own schema
(`profiling`, per `Docs/10-architecture/04-module-decomposition.md` Sec.6).

Not importable from outside this module once the `module-privacy`
contract (tracker ST-02) is enforced.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`), review-2026-09-05 point **R04**
("relocate one bounded context at a time with compatibility exports").
Moved verbatim from `aida.models`, which now re-exports these classes for
backward compatibility -- every existing `from aida.models import
AnalysisRun` caller keeps working unchanged. This is a Python-source-location
move only: these classes still declare no `schema=` in `__table_args__` and
still live in the single shared PostgreSQL schema, on the one declarative
base in `atlas.platform.db`. The actual database schema migration (refactor
plan Sec.5 steps 2.3/2.4) is explicitly deferred to a later, separate pass,
so this move needs -- and has -- no Alembic revision of its own.

Owned tables (per Sec.4's register for module 05: "analysis runs, tasks,
table/column profiles, classifications, key inferences"):

* `AnalysisRun`, `AnalysisTask`, `ScanPolicy` -- the run ledger and the
  schedule that produces it. `ScanPolicy` is profiling's, not
  connectivity's, even though it is keyed by `datasource_id` and its two
  endpoints live in `atlas.modules.connectivity.router`: what it schedules
  is an analysis run. That router keeps importing the class from
  `aida.models`, unchanged, through the re-export below.
* `TableProfile`, `ColumnProfile` -- the value-free profile statistics.
* `ProfilingExceptionPolicy`, `ColumnValueProfileArtifact` -- the
  maker-checker gate for value-bearing profiling (ADR-0014 exception) and
  the retention-bound artifact it unlocks.
* `ClassificationEvidence`, `ColumnDerivedClassification` -- the
  classification ledgers. `atlas.modules.catalog.models`' own docstring
  already named these as this module's, not catalog's: "classifications is
  module 05 (profiling)'s registered word ... even though the evidence
  ledger references `MetadataColumn` by ID."

Explicitly NOT moved here despite sitting inside the same span of the old
`aida.models`:

* `PolicyNativeSyncRequest` -- sits physically between
  `ProfilingExceptionPolicy` and `ColumnValueProfileArtifact` and copies the
  former's maker-checker shape, but it gates source-native row/column policy
  DDL (QG-2). That is module 16 (query-gateway) / 17 (policy-governance)
  territory, not profiling's. Shape similarity is not ownership.
* `CompositeKeyCandidate` and the other `*Candidate` models -- Sec.4 gives
  module 05 "key inferences" and module 06 "relationship candidates,
  evidence, decisions, negative knowledge, table families", and these live
  in the second list's neighbourhood. Left for module 06's own pass rather
  than split on the strength of one ambiguous phrase.
* `AccessPolicy`, `Embedding` -- governance and retrieval, immediately above
  this block in the old file purely by accretion order.

Cross-module `ForeignKey`s are kept exactly as they were (`organization.id`,
`datasource.id`, `metadata_table.id`, `metadata_column.id`). Replacing them
with plain ID columns is refactor-plan step 2.4, a deliberately separate and
independently revertible change, and doing it here would alter
`Base.metadata` -- which this move must not.
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
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from atlas.platform.db import Base, TimestampMixin, utc_now


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
