"""profiling -- PRIVATE. Request/response models for `router.py`.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`), review-2026-09-05 point **R04**
("separate persistence, API DTOs, and domain values"). Moved verbatim from
`aida.schemas`, which now re-exports these classes for backward
compatibility -- every existing `from aida.schemas import AnalysisRunRead`
caller keeps working unchanged. The relocation procedure is
`Docs/40-engineering/10-bounded-context-relocation-procedure.md`.

Covers the DTOs for this module's owned models
(`atlas.modules.profiling.models`): the analysis-run ledger and its tasks,
the scan schedule, the value-free table/column profiles, the
value-bearing-profiling exception gate, and the classification evidence
ledger plus its authoritative-feed ingest payloads.

Field-for-field verbatim: no field was added, removed, renamed, reordered,
retyped or re-defaulted, and every validator moved with its class, so the
generated OpenAPI schema is unchanged -- which the `scripts/openapi_diff.py`
gate is what proves, not this note.

`FleetSummaryRead` is deliberately NOT here despite sitting inside the same
span of the old `aida.schemas`. It is a cross-module aggregate --
datasource statuses (module 02), analysis-run statuses (this module), scan
policy counts (this module) and outbox backlog (module 20) in one payload --
and `04-module-decomposition.md` Sec.9 assigns `fleet.py` to module 03
(ingestion). A DTO that reports on four modules is not owned by whichever one
it mentions first; it stays in `aida.schemas` until the fleet surface itself
is relocated.

`GraphSummaryRead` likewise stays: it counts catalog objects and reports
graph-projection lag (modules 04 and 10).

`ApiModel` is imported from `atlas.platform.schemas`, the neutral base this module and
`aida.schemas` both use, so this module no longer imports `aida.schemas` and the two
no longer form an import cycle (review 2026-09-05 R03, completed 2026-09-21).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from atlas.platform.schemas import ApiModel


class AnalysisRunCreate(ApiModel):
    mode: str = Field(default="INCREMENTAL", pattern=r"^(FULL|INCREMENTAL)$")


class AnalysisRunRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    resumed_from_run_id: UUID | None
    mode: str
    trigger_type: str
    priority: int
    status: str
    temporal_workflow_id: str | None
    discovered_catalogs: int
    discovered_schemas: int
    discovered_tables: int
    discovered_columns: int
    discovered_constraints: int
    created_objects: int
    changed_objects: int
    deprecated_objects: int
    discovery_selection_fingerprint: str | None = None
    excluded_objects: int = 0
    #: R11-FP02: per kind and per facet, what the run took in and how completely.
    discovery_receipt: dict[str, Any] | None = None
    profiled_tables: int
    profiled_columns: int
    error_class: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ScanPolicyUpsert(ApiModel):
    enabled: bool = True
    interval_minutes: int = Field(ge=5, le=525_600)
    mode: Literal["FULL", "INCREMENTAL"] = "INCREMENTAL"
    priority: int = Field(default=50, ge=0, le=100)
    usage_boost_enabled: bool = False
    maintenance_start_hour_utc: int | None = Field(default=None, ge=0, le=23)
    maintenance_end_hour_utc: int | None = Field(default=None, ge=0, le=23)
    start_at: datetime | None = None

    @model_validator(mode="after")
    def validate_maintenance_window(self) -> ScanPolicyUpsert:
        if (self.maintenance_start_hour_utc is None) != (self.maintenance_end_hour_utc is None):
            raise ValueError("both maintenance-window hours must be provided")
        if (
            self.maintenance_start_hour_utc is not None
            and self.maintenance_start_hour_utc == self.maintenance_end_hour_utc
        ):
            raise ValueError("maintenance-window hours cannot be equal")
        return self


class ScanPolicyRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    enabled: bool
    interval_minutes: int
    mode: str
    priority: int
    usage_boost_enabled: bool
    base_priority: int
    computed_usage_boost: int
    usage_boost_updated_at: datetime | None
    maintenance_start_hour_utc: int | None
    maintenance_end_hour_utc: int | None
    next_run_at: datetime
    last_triggered_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class AnalysisTaskRead(ApiModel):
    id: UUID
    analysis_run_id: UUID
    table_id: UUID | None
    task_type: str
    task_key: str
    status: str
    attempt_count: int
    max_attempts: int
    started_at: datetime | None
    last_heartbeat_at: datetime | None
    completed_at: datetime | None
    heartbeat_detail: dict[str, Any]
    error_class: str | None
    error_message: str | None
    retry_history: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class ClassificationEvidenceRead(ApiModel):
    id: UUID
    column_id: UUID
    classification: str
    source_type: str
    rule_id: str
    confidence: float | None
    matched_signal: dict[str, Any]
    is_current: bool
    created_by: str
    created_at: datetime


class ClassificationFeedRecord(ApiModel):
    schema_name: str = Field(min_length=1, max_length=255)
    table_name: str = Field(min_length=1, max_length=255)
    column_name: str = Field(min_length=1, max_length=255)
    classification: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,29}$")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    note: str | None = Field(default=None, max_length=500)


class ClassificationFeedIngestRequest(ApiModel):
    source: str = Field(min_length=1, max_length=255)
    records: list[ClassificationFeedRecord] = Field(min_length=1, max_length=500)


class ClassificationFeedIngestResponse(ApiModel):
    source: str
    total: int
    matched: int
    changed: int
    unmatched: list[str]


class ProfileFacetStatusRead(ApiModel):
    """R11-FP04: one facet that is absent from a profile, and why.

    Three closed vocabularies (`aida.connectors.base`' `PROFILE_FACETS`,
    `FACET_STATUSES`, `FACET_REASON_CODES`), which is what lets this be served
    at all: the natural implementation of a reason is the driver's own message,
    and a source driver's error text routinely quotes the offending row. A code
    cannot carry a value (INV-6).
    """

    facet: str
    status: Literal["UNSUPPORTED", "NOT_APPLICABLE", "PERMISSION_DENIED", "UNAVAILABLE"]
    reason_code: str


class ColumnProfileRead(ApiModel):
    column_id: UUID
    column_name: str
    classification: str
    null_count: int
    non_null_count: int
    approximate_distinct_count: int
    min_length: int | None
    max_length: int | None
    # -- R11-FP04 value-free aggregate facets ------------------------------
    #
    # Every one is optional, because every one is genuinely absent for some
    # (engine, column type, profile age) combination and `unavailable_facets`
    # is where a reader learns which. None is never "zero".
    #
    # `length_bucket_counts` is positionally aligned to the code-defined
    # scheme `length_bucket_scheme` names (`LENGTH_BUCKET_BOUNDS`). The
    # boundaries are deliberately not in this payload: a bucket edge is a
    # value (ADR-0014), and a client that needs the edges reads the named
    # scheme rather than being handed a histogram of the data.
    distinct_ratio: float | None = None
    effectively_unique: bool | None = None
    cardinality_class: str | None = None
    blank_count: int | None = None
    whitespace_only_count: int | None = None
    length_bucket_scheme: str | None = None
    length_bucket_counts: list[int] | None = None
    frequency_entropy_bits: float | None = None
    unavailable_facets: list[ProfileFacetStatusRead] = Field(default_factory=list)
    # Set when an access policy withheld this column's facets rather than the
    # engine failing to produce them. The column still appears, with its
    # marker and its reason code: a read that silently dropped the column
    # would let a reader conclude something about the table from a fact about
    # their own entitlement.
    facets_withheld: bool = False
    withheld_marker: str | None = None
    withheld_reason_code: str | None = None


class TableProfileRead(ApiModel):
    id: UUID
    analysis_run_id: UUID
    table_id: UUID
    row_count_estimate: int | None
    sampled_row_count: int
    profile_version: str
    status: str
    created_at: datetime
    columns: list[ColumnProfileRead]
    # -- R11-FP04 ----------------------------------------------------------
    #
    # How much of the table this profile saw, as the connector itself reported
    # it. `None` is a third state and not a synonym for UNKNOWN: it means the
    # profile predates the facet, so nothing recorded a scope, whereas UNKNOWN
    # means a connector recorded that it could not say. The design authority
    # requires the read surface to carry "statistical evidence **and sampling
    # limitations**", and a statistic whose scope is unstated is the limitation
    # going unsaid.
    observation_scope: Literal["FULL", "SAMPLE", "UNKNOWN"] | None = None
    # Facets FP-04 names that the value-free half does not compute at all,
    # engine-independent (`aida.connectors.base.UNCOMPUTED_FACET_STATUS`).
    # Served per table rather than repeated on every column because the answer
    # never varies by column.
    uncomputed_facets: list[ProfileFacetStatusRead] = Field(default_factory=list)
    # How many of `columns` had their facets withheld by policy. The count is
    # the half of the withheld convention a client can act on without walking
    # the list.
    withheld_column_count: int = 0


class ProfilingExceptionPolicyCreate(ApiModel):
    """PR-2: request a policy-approved range/top-value profiling exception.

    Scoped to exactly one `(organization_id, datasource_id, classification)`
    triple (`organization_id` comes from the caller's `SecurityContext`,
    `datasource_id` from the URL path) -- `classification` must be one of the
    sensitive classes (`aida.classification.SENSITIVE_CLASSES`); requesting an
    exception for `UNCLASSIFIED`/`PUBLIC`/`INTERNAL` is rejected up front,
    since there is nothing sensitive there to gate.
    """

    classification: str = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=3, max_length=2000)
    retention_days: int = Field(ge=1, le=3650)


class ProfilingExceptionPolicyRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    classification: str
    status: str
    retention_days: int
    requested_by: str
    request_reason: str
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    revoked_by: str | None
    revoked_at: datetime | None
    revocation_reason: str | None
    created_at: datetime
    updated_at: datetime


class ProfilingExceptionDecisionRequest(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_rejection_reason(self) -> ProfilingExceptionDecisionRequest:
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a profiling exception policy")
        return self


class ProfilingExceptionRevokeRequest(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)
