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

`ApiModel` stays defined in `aida.schemas` rather than moving here or to
`atlas.platform` -- it is the shared pydantic base for every module's
schemas, not profiling-owned, and moving it is out of scope for this pass.
Importing it back from `aida.schemas` here works safely only because
`aida.schemas`' shim import of this module comes *after* `ApiModel` is
defined in that file -- see the comment there, and the same note in the four
sibling modules' schema files.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from aida.schemas import ApiModel


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


class ColumnProfileRead(ApiModel):
    column_id: UUID
    column_name: str
    classification: str
    null_count: int
    non_null_count: int
    approximate_distinct_count: int
    min_length: int | None
    max_length: int | None


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
