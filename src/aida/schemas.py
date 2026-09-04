import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aida.catalog_bulk_actions import ALLOWED_CLASSIFICATIONS, CATALOG_BULK_ACTION_MAX_ITEMS


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.identity_tenancy.schemas` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.schemas import OrganizationCreate` (etc.) caller keeps working
# unchanged. This import must come after `ApiModel` is defined above: the
# moved module imports `ApiModel` back from this file, so `aida.schemas`
# must already have it bound in its namespace before that circular import
# resolves -- see the docstring in `atlas.modules.identity_tenancy.schemas`.
from atlas.modules.identity_tenancy.schemas import (  # noqa: E402, I001
    BusinessAssignmentCreate as BusinessAssignmentCreate,
    BusinessAssignmentRead as BusinessAssignmentRead,
    BusinessNodeCreate as BusinessNodeCreate,
    BusinessNodeRead as BusinessNodeRead,
    BusinessNodeRollupRead as BusinessNodeRollupRead,
    ClassificationDecisionRead as ClassificationDecisionRead,
    CrossBoundaryGrantCreate as CrossBoundaryGrantCreate,
    CrossBoundaryGrantRead as CrossBoundaryGrantRead,
    DataDomainCreate as DataDomainCreate,
    DataDomainRead as DataDomainRead,
    EntitlementReportRead as EntitlementReportRead,
    GenerateEntitlementReportRequest as GenerateEntitlementReportRequest,
    LineOfBusinessCreate as LineOfBusinessCreate,
    LineOfBusinessRead as LineOfBusinessRead,
    OrganizationCreate as OrganizationCreate,
    OrganizationIntegrationPolicyRead as OrganizationIntegrationPolicyRead,
    OrganizationIntegrationPolicyWrite as OrganizationIntegrationPolicyWrite,
    OrganizationRead as OrganizationRead,
    ProjectCreate as ProjectCreate,
    ProjectRead as ProjectRead,
    SourceBindingCreate as SourceBindingCreate,
    SourceBindingDecision as SourceBindingDecision,
    SourceBindingRead as SourceBindingRead,
    SourceEntitlementRead as SourceEntitlementRead,
    WorkspaceCreate as WorkspaceCreate,
    WorkspaceEntitlementRead as WorkspaceEntitlementRead,
    WorkspaceMembershipCreate as WorkspaceMembershipCreate,
    WorkspaceMembershipRead as WorkspaceMembershipRead,
    WorkspaceRead as WorkspaceRead,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.connectivity.schemas` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.schemas import DataSourceCreate` (etc.) caller keeps working
# unchanged. Same after-`ApiModel` placement requirement as the
# identity_tenancy shim above.
from atlas.modules.connectivity.schemas import (  # noqa: E402, I001
    DATASOURCE_BULK_ONBOARD_MAX_ITEMS as DATASOURCE_BULK_ONBOARD_MAX_ITEMS,
    ConnectorCapabilityRead as ConnectorCapabilityRead,
    ConnectorCertificationRead as ConnectorCertificationRead,
    DataSourceBulkOnboardItemRead as DataSourceBulkOnboardItemRead,
    DataSourceBulkOnboardRequest as DataSourceBulkOnboardRequest,
    DataSourceBulkOnboardResultRead as DataSourceBulkOnboardResultRead,
    DataSourceCreate as DataSourceCreate,
    DataSourceRead as DataSourceRead,
    DataSourceSummaryRead as DataSourceSummaryRead,
    DataSourceUpdate as DataSourceUpdate,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.ingestion.schemas` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.schemas import MetadataIngestionCreate` (etc.) caller keeps
# working unchanged. Same after-`ApiModel` placement requirement as the
# identity_tenancy shim above.
from atlas.modules.ingestion.schemas import (  # noqa: E402, I001
    MetadataAttribute as MetadataAttribute,
    MetadataCatalogEnvelope as MetadataCatalogEnvelope,
    MetadataColumnEnvelope as MetadataColumnEnvelope,
    MetadataConstraintEnvelope as MetadataConstraintEnvelope,
    MetadataGrantEnvelope as MetadataGrantEnvelope,
    MetadataIngestionBatchCreate as MetadataIngestionBatchCreate,
    MetadataIngestionBatchRead as MetadataIngestionBatchRead,
    MetadataIngestionChunkCreate as MetadataIngestionChunkCreate,
    MetadataIngestionChunkRead as MetadataIngestionChunkRead,
    MetadataIngestionCreate as MetadataIngestionCreate,
    MetadataIngestionRead as MetadataIngestionRead,
    MetadataRoutineEnvelope as MetadataRoutineEnvelope,
    MetadataRoutineParameterEnvelope as MetadataRoutineParameterEnvelope,
    MetadataSchemaEnvelope as MetadataSchemaEnvelope,
    MetadataTableEnvelope as MetadataTableEnvelope,
    MetadataViewDefinitionEnvelope as MetadataViewDefinitionEnvelope,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.catalog.schemas` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.schemas import MetadataTableRead` (etc.) caller keeps working
# unchanged. Same after-`ApiModel` placement requirement as the
# identity_tenancy shim above.
from atlas.modules.catalog.schemas import (  # noqa: E402, I001
    MetadataColumnRead as MetadataColumnRead,
    MetadataConstraintRead as MetadataConstraintRead,
    MetadataIndexRead as MetadataIndexRead,
    MetadataPartitionRead as MetadataPartitionRead,
    MetadataTableRead as MetadataTableRead,
)

# Re-exported for backward compatibility -- tracker ST-05 moved the classes
# below to `atlas.modules.observability_audit.schemas` (Phase 3 of
# `Docs/40-engineering/06-refactor-plan.md`). Every existing
# `from aida.schemas import AuditEventRead` (etc.) caller keeps working
# unchanged. Same after-`ApiModel` placement requirement as the
# identity_tenancy shim above.
from atlas.modules.observability_audit.schemas import (  # noqa: E402, I001
    ArchiveStatusRead as ArchiveStatusRead,
    AuditEventRead as AuditEventRead,
    OutboxEventRead as OutboxEventRead,
    SloBudgetRead as SloBudgetRead,
    SloDefinitionCreate as SloDefinitionCreate,
    SloDefinitionRead as SloDefinitionRead,
)


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
    def validate_maintenance_window(self) -> "ScanPolicyUpsert":
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



class FleetSummaryRead(ApiModel):
    organization_id: UUID
    datasource_statuses: dict[str, int]
    analysis_run_statuses: dict[str, int]
    scan_policies_enabled: int
    scan_policies_due: int
    pending_outbox_events: int
    dead_letter_outbox_events: int
    generated_at: datetime


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
    def require_rejection_reason(self) -> "ProfilingExceptionDecisionRequest":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a profiling exception policy")
        return self


class ProfilingExceptionRevokeRequest(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)


class GraphSummaryRead(ApiModel):
    datasource_id: UUID
    catalogs: int
    schemas: int
    tables: int
    columns: int
    sensitive_columns: int
    constraints: int
    foreign_key_relationships: int
    projection_status: str
    projection_lag: dict[str, int]


class SemanticModelVersionCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    change_summary: str = Field(min_length=3, max_length=1000)
    based_on_version_id: UUID | None = None


class SemanticModelCloneRequest(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    change_summary: str = Field(min_length=3, max_length=1000)


class SemanticModelVersionRead(ApiModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    version: int
    name: str
    change_summary: str
    status: str
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    published_at: datetime | None
    based_on_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class SemanticMetricCreate(ApiModel):
    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=3, max_length=4000)
    aggregation: Literal["SUM", "COUNT", "AVG", "MIN", "MAX"]
    grain: str = Field(min_length=2, max_length=255)
    source_table_id: UUID
    measure_column_id: UUID | None = None
    default_time_column_id: UUID | None = None
    allowed_dimension_column_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_measure(self) -> "SemanticMetricCreate":
        if self.aggregation != "COUNT" and self.measure_column_id is None:
            raise ValueError("measure_column_id is required unless aggregation is COUNT")
        if len(set(self.allowed_dimension_column_ids)) != len(self.allowed_dimension_column_ids):
            raise ValueError("allowed dimension columns must be unique")
        return self


class SemanticMetricVersionRead(ApiModel):
    id: UUID
    semantic_model_version_id: UUID
    metric_id: UUID
    metric_slug: str
    metric_name: str
    version: int
    status: str
    description: str
    aggregation: str
    grain: str
    source_table_id: UUID
    measure_column_id: UUID | None
    default_time_column_id: UUID | None
    allowed_dimension_column_ids: list[UUID]
    fingerprint: str
    created_by: str
    created_at: datetime


class GovernanceReviewRead(ApiModel):
    id: UUID
    organization_id: UUID
    object_type: str
    object_id: str
    requested_action: str
    status: str
    requested_by: str
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GovernanceDecisionRequest(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_rejection_reason(self) -> "GovernanceDecisionRequest":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a review")
        return self


# PG-3: a single bulk-decision request against the unified governance review
# queue may touch at most this many reviews -- the exit condition's own
# number ("10,000-item selection workable"), an order of magnitude above
# RL-6/CT-1's 500 since the queue-level cap is meant to cover a full backlog
# sweep across object types, not just one datasource/table's worth of
# candidates. Whether the caller supplied an explicit id list (rejected
# outright above this size) or a filter (silently capped -- see
# `_resolve_governance_review_bulk_subjects` in semantic_api.py), the same
# bound applies.
GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS = 10_000


class GovernanceReviewBulkSelectionFilter(ApiModel):
    """Reuses `list_governance_reviews`'s existing filter shape (status,
    scoped to the caller's organization) plus an optional object_type
    narrowing, rather than inventing a new query language.
    """

    object_type: str | None = Field(default=None, max_length=100)
    status: str = Field(default="PENDING", max_length=30)


class GovernanceReviewBulkDecisionRequest(ApiModel):
    review_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS,
    )
    filter: GovernanceReviewBulkSelectionFilter | None = None
    decision: Literal["APPROVE", "REJECT"]
    # A single rationale applied to every decided item that has no entry in
    # `rationale_by_review_id` -- a convenience default, not the primary
    # mechanism: PG-3's exit condition asks for *per-item* rationale, so each
    # item's rationale of record is `rationale_by_review_id[id]` when
    # present, falling back to this shared value otherwise. An item that ends
    # up with no rationale at all on a REJECT decision fails only that item
    # (partial success) -- it does not reject the whole batch.
    reason: str | None = Field(default=None, max_length=2000)
    rationale_by_review_id: dict[UUID, str] | None = Field(default=None)

    @model_validator(mode="after")
    def validate_selection(self) -> "GovernanceReviewBulkDecisionRequest":
        _require_exactly_one_selection(self.review_ids, self.filter)
        if self.rationale_by_review_id is not None:
            if len(self.rationale_by_review_id) > GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS:
                raise ValueError("rationale_by_review_id may not exceed the batch cap")
            for review_id, rationale in self.rationale_by_review_id.items():
                if not rationale or not rationale.strip():
                    raise ValueError(f"rationale for review {review_id} must not be blank")
                if len(rationale) > 2000:
                    raise ValueError(f"rationale for review {review_id} exceeds 2000 characters")
        if self.decision == "REJECT" and not self.reason and not self.rationale_by_review_id:
            raise ValueError(
                "a rationale is required when rejecting: provide a shared `reason` or a "
                "`rationale_by_review_id` entry per item"
            )
        return self


class GovernanceReviewBulkDecisionItemRead(ApiModel):
    review_id: str
    status: Literal["SUCCEEDED", "FAILED"]
    reason: str | None = None


class GovernanceReviewBulkDecisionResultRead(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    selection_mode: Literal["EXPLICIT", "FILTER"]
    requested_count: int
    succeeded_count: int
    failed_count: int
    truncated: bool
    results: list[GovernanceReviewBulkDecisionItemRead]


class ToolParameterDefinition(ApiModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    parameter_type: Literal["STRING", "INTEGER", "NUMBER", "BOOLEAN", "DATE"]
    required: bool = True
    default: Any | None = None
    allowed_values: list[Any] | None = Field(default=None, max_length=500)
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = Field(default=None, ge=1, le=10_000)
    sensitive: bool = False

    @model_validator(mode="after")
    def validate_bounds(self) -> "ToolParameterDefinition":
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("parameter minimum cannot exceed maximum")
        if self.sensitive and self.default is not None:
            raise ValueError("sensitive parameters cannot define persisted defaults")
        return self


class GovernedToolVersionCreate(ApiModel):
    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=3, max_length=4000)
    datasource_id: UUID
    semantic_model_version_id: UUID | None = None
    sql_template: str = Field(min_length=1, max_length=200_000)
    parameters: list[ToolParameterDefinition] = Field(default_factory=list, max_length=100)
    allowed_roles: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_names_and_roles(self) -> "GovernedToolVersionCreate":
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("tool parameter names must be unique")
        if len(self.allowed_roles) != len(set(self.allowed_roles)):
            raise ValueError("tool allowed roles must be unique")
        if any(not role or len(role) > 100 for role in self.allowed_roles):
            raise ValueError("tool allowed roles are invalid")
        return self


class GovernedToolVersionRead(ApiModel):
    id: UUID
    tool_id: UUID
    organization_id: UUID
    project_id: UUID
    slug: str
    version: int
    status: str
    name: str
    description: str
    datasource_id: UUID
    semantic_model_version_id: UUID | None
    sql_template: str
    referenced_tables: list[str]
    parameters: list[ToolParameterDefinition]
    allowed_roles: list[str]
    fingerprint: str
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # TL-4: completed-execution count for this tool (all versions, bounded
    # lookback window) -- the usage signal `list_tools` and MCP `tools/list`
    # rank by. Always 0 for a brand-new draft; never populated by
    # `model_validate` on a bare ORM row, only by `tool_api._tool_read`.
    usage_count: int = 0


class ToolExecutionRequest(ApiModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)


class SqlValidationRequest(ApiModel):
    sql: str = Field(min_length=1, max_length=200_000)
    dialect: str = Field(default="postgres", min_length=1, max_length=50)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)


class SqlValidationResponse(ApiModel):
    valid: bool
    normalized_sql: str | None
    referenced_tables: list[str]
    referenced_columns: list[str]
    violations: list[str]
    applied_row_limit: int | None


class QueryExecutionRequest(ApiModel):
    sql: str = Field(min_length=1, max_length=200_000)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)
    semantic_version: str | None = Field(default=None, max_length=100)
    # Which workspace is asking (ADR-0018). Optional while the estate migrates: a
    # datasource with exactly one live binding resolves without it. It stops being
    # optional when `unresolved_workspace_posture` flips to DENY, and the request
    # that omits it is refused rather than silently attributed to some workspace.
    workspace_id: UUID | None = None


class QueryExecutionResponse(ApiModel):
    execution_id: UUID
    status: str
    normalized_sql: str
    referenced_tables: list[str]
    referenced_columns: list[str]
    column_lineage: list[dict[str, Any]]
    plan_cost: float
    warehouse_query_id: str | None
    row_count: int
    elapsed_ms: int
    masked_columns: list[str]
    rows: list[dict[str, Any]]


class QueryLineageRead(ApiModel):
    execution_id: UUID
    datasource_id: UUID
    status: str
    referenced_tables: list[str]
    referenced_columns: list[str]
    column_lineage: list[dict[str, Any]]
    semantic_version: str | None
    policy_version: str


class ToolExecutionResponse(ApiModel):
    tool_execution_id: UUID
    tool_version_id: UUID
    tool_slug: str
    tool_version: int
    execution: QueryExecutionResponse
    # TL-3: non-null when `check_tool_gate` demoted this execution to WARN
    # (an upstream dependency has an open, non-critical quality incident); a
    # BLOCK never reaches this response -- it is refused with HTTP 409 before
    # a `ToolExecution` row exists.
    quality_gate: dict[str, Any] | None = None


class AgentAnalysisRequest(ApiModel):
    question: str = Field(min_length=3, max_length=10_000)
    candidate_sql: str | None = Field(default=None, min_length=1, max_length=200_000)
    preferred_tool_version_id: UUID | None = None
    tool_parameters: dict[str, Any] = Field(default_factory=dict)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)


class AgentAnalysisResponse(ApiModel):
    agent_run_id: UUID
    status: str
    generation_source: str
    semantic_version: str | None
    policy_version: str
    step_trace: list[dict[str, Any]]
    retrieval_evidence: list[dict[str, Any]]
    plan_evidence: dict[str, Any]
    execution: QueryExecutionResponse
    explanation: str


class AgentRunRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    principal_id: str
    status: str
    generation_source: str
    model_route: str | None
    semantic_version: str | None
    policy_version: str
    query_execution_id: UUID | None
    step_trace: list[dict[str, Any]]
    retrieval_evidence: list[dict[str, Any]]
    # AT-6: one entry per grounding fragment hashed at assembly time --
    # {"object_type", "object_id", "fragment_digest", "annotation_version_id"}.
    # See `GET /v1/agent-runs/{id}/grounding-receipts` to resolve these back to
    # source content.
    grounding_fragment_digests: list[dict[str, Any]]
    plan_evidence: dict[str, Any]
    recommended_tool_version_id: UUID | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class GroundingFragmentReceiptRead(ApiModel):
    """One resolved AT-6 grounding-fragment digest: what was hashed, and --
    for a `BUSINESS_ANNOTATION` fragment -- the exact (possibly since
    superseded) `MetadataBusinessAnnotationVersion` content it resolves to.
    """

    object_type: str
    object_id: str
    fragment_digest: str
    annotation_version_id: UUID | None
    annotation_version: int | None
    annotation_status: str | None
    business_name: str | None
    business_description: str | None
    digest_verified: bool


class AgentRunGroundingReceiptsRead(ApiModel):
    agent_run_id: UUID
    fragment_count: int
    fragments: list[GroundingFragmentReceiptRead]


class AiRuntimeStatusRead(ApiModel):
    orchestration_mode: Literal["HYBRID"]
    runtime: str
    runtime_version: str
    model_route_status: Literal["CONFIGURED", "NOT_CONFIGURED"]
    model_generation_enabled: bool
    available_model_providers: list[str]
    development_sql_override_enabled: bool
    identity_provider: str
    identity_verification: str
    oidc_configured: bool
    credential_provider: str
    credential_provider_available: bool
    enterprise_security_ready: bool
    deterministic_controls: list[str]
    optional_framework_adapters: list[str]
    data_retention_statement: str


class AgentRetrievalPreviewRequest(ApiModel):
    question: str = Field(min_length=3, max_length=10_000)
    candidate_sql_available: bool = False


class AgentRetrievalPreviewRead(ApiModel):
    datasource_id: UUID
    retrieval_evidence: list[dict[str, Any]]
    plan_evidence: dict[str, Any]


class AgentEvaluationRunRead(ApiModel):
    id: UUID
    organization_id: UUID
    principal_id: str
    suite_version: str
    status: str
    scenario_count: int
    passed_count: int
    failed_count: int
    pass_rate: float
    findings: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class QueryFeedbackUpsert(ApiModel):
    rating: Literal["HELPFUL", "NOT_HELPFUL", "INCORRECT"]
    comment: str | None = Field(default=None, max_length=4000)


class QueryFeedbackRead(ApiModel):
    id: UUID
    organization_id: UUID
    agent_run_id: UUID
    principal_id: str
    rating: str
    created_at: datetime
    updated_at: datetime


class QueryMemoryEvidenceRead(ApiModel):
    id: UUID
    datasource_id: UUID
    agent_run_id: UUID
    query_execution_id: UUID
    semantic_version: str | None
    status: str
    positive_feedback_count: int
    negative_feedback_count: int
    created_at: datetime
    updated_at: datetime


class RelationshipCandidateDiscoveryRequest(ApiModel):
    max_candidates: int = Field(default=500, ge=1, le=5000)


class CrossSourceRelationshipCandidateDiscoveryRequest(ApiModel):
    max_candidates: int = Field(default=500, ge=1, le=5000)
    max_datasource_pairs: int = Field(default=50, ge=1, le=2000)
    target_data_domain_id: UUID | None = Field(
        default=None,
        description=(
            "Pair this domain's datasources against another data_domain's "
            "instead of scanning within this domain alone. Requires an ACTIVE "
            "cross_boundary_grant permitting this domain to see into the "
            "target one (ADR-0017 SS4) -- rejected with 403 otherwise."
        ),
    )


class RelationshipCandidateRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    target_datasource_id: UUID
    source_table_id: UUID
    source_column_id: UUID
    target_table_id: UUID
    target_column_id: UUID
    detection_rule: str
    confidence: float
    evidence: dict[str, Any]
    status: str
    created_by: str
    reviewed_by: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RelationshipCandidateDecision(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_reason(self) -> "RelationshipCandidateDecision":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a relationship")
        return self


class TableRef(ApiModel):
    """A resolved table reference; the return type of ``resolve_canonical``."""

    table_id: UUID
    qualified_name: str


class TableFamilyDiscoveryRequest(ApiModel):
    max_candidates: int = Field(default=200, ge=1, le=2000)


class TableFamilyCandidateRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    schema_id: UUID
    family_type: Literal["SNAPSHOT", "HISTORY", "DELTA", "SCD"]
    member_table_ids: list[UUID]
    base_table_id: UUID | None
    detection_rule: str
    confidence: float
    evidence: dict[str, Any]
    status: str
    created_by: str
    reviewed_by: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TableFamilyCandidateDecision(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_reason(self) -> "TableFamilyCandidateDecision":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a table family candidate")
        return self


class RenameCandidateRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    analysis_run_id: UUID
    schema_id: UUID
    old_table_id: UUID
    new_table_id: UUID
    detection_rule: str
    confidence: float
    evidence: dict[str, Any]
    status: str
    created_by: str
    reviewed_by: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    merged_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RenameCandidateDecision(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_reason(self) -> "RenameCandidateDecision":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a rename candidate")
        return self


class CompositeKeyCandidateRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    table_id: UUID
    column_ids: list[UUID]
    detection_rule: str
    confidence: float
    evidence: dict[str, Any]
    status: str
    created_by: str
    reviewed_by: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CompositeKeyCandidateDecision(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_reason(self) -> "CompositeKeyCandidateDecision":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a composite key candidate")
        return self


class CrossSourceObjectResolutionDiscoveryRequest(ApiModel):
    max_candidates: int = Field(default=500, ge=1, le=5000)
    max_datasource_pairs: int = Field(default=50, ge=1, le=2000)
    target_data_domain_id: UUID | None = Field(
        default=None,
        description=(
            "Pair this domain's datasources against another data_domain's "
            "instead of scanning within this domain alone. Requires an ACTIVE "
            "cross_boundary_grant permitting this domain to see into the "
            "target one (ADR-0017 SS4) -- rejected with 403 otherwise."
        ),
    )


class CrossSourceResolutionCandidateRead(ApiModel):
    id: UUID
    organization_id: UUID
    source_datasource_id: UUID
    source_table_id: UUID
    target_datasource_id: UUID
    target_table_id: UUID
    detection_rule: str
    confidence: float
    evidence: dict[str, Any]
    status: str
    created_by: str
    reviewed_by: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# KG-5: saved Knowledge Graph / Graph Explorer perspectives
# ---------------------------------------------------------------------------

#: Same order of magnitude as ``openlineage.MAX_OPENLINEAGE_EVENT_BYTES`` (1 MiB) for a single
#: caller-supplied JSON blob, but a Graph Explorer view-state snapshot (a center node, a depth,
#: a handful of edge-kind filters, layout/pan/zoom) is far smaller than an OpenLineage run event
#: with its nested datasets/facets, so 256 KiB is a comfortably generous bound rather than a
#: tight one.
GRAPH_PERSPECTIVE_MAX_VIEW_STATE_BYTES = 256 * 1024


def _validate_view_state_size(value: dict[str, Any]) -> dict[str, Any]:
    encoded_length = len(json.dumps(value).encode("utf-8"))
    if encoded_length > GRAPH_PERSPECTIVE_MAX_VIEW_STATE_BYTES:
        raise ValueError(
            "view_state exceeds the "
            f"{GRAPH_PERSPECTIVE_MAX_VIEW_STATE_BYTES}-byte limit "
            f"({encoded_length} bytes)"
        )
    return value


class GraphPerspectiveCreate(ApiModel):
    """Opaque frontend Graph Explorer state, plus queryable metadata.

    ``view_state`` is never interpreted server-side -- only validated as a
    JSON object bounded in size. See ``models.GraphPerspective`` for an
    example shape and the sharing model.
    """

    datasource_id: UUID | None = None
    name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    allowed_viewer_roles: list[str] = Field(default_factory=list, max_length=100)
    view_state: dict[str, Any] = Field(default_factory=dict)

    @field_validator("view_state")
    @classmethod
    def validate_view_state_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_view_state_size(value)

    @field_validator("allowed_viewer_roles")
    @classmethod
    def validate_allowed_viewer_roles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_viewer_roles must be unique")
        if any(not role or len(role) > 100 for role in value):
            raise ValueError("allowed_viewer_roles entries must be non-empty and <= 100 chars")
        return value


class GraphPerspectiveUpdate(ApiModel):
    """All fields optional: only owner-supplied fields are applied (owner-only, see the API)."""

    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    allowed_viewer_roles: list[str] | None = Field(default=None, max_length=100)
    view_state: dict[str, Any] | None = None

    @field_validator("view_state")
    @classmethod
    def validate_view_state_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _validate_view_state_size(value)

    @field_validator("allowed_viewer_roles")
    @classmethod
    def validate_allowed_viewer_roles(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("allowed_viewer_roles must be unique")
        if any(not role or len(role) > 100 for role in value):
            raise ValueError("allowed_viewer_roles entries must be non-empty and <= 100 chars")
        return value


class GraphPerspectiveRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID | None
    name: str
    description: str | None
    owner_principal: str
    allowed_viewer_roles: list[str]
    view_state: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CrossSourceResolutionCandidateDecision(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_reason(self) -> "CrossSourceResolutionCandidateDecision":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a cross-source resolution")
        return self


# RL-6: a single bulk-decision request may touch at most this many candidates,
# whether selected by an explicit id list (rejected outright above this size,
# same as CATALOG_BULK_ACTION_MAX_ITEMS's precedent) or by a filter (silently
# capped -- see `_resolve_relationship_candidate_bulk_subjects` in
# intelligence_api.py).
RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS = 500


class RelationshipCandidateBulkSelectionFilter(ApiModel):
    datasource_id: UUID
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    max_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detection_rule: str | None = Field(default=None, max_length=100)


class RelationshipCandidateBulkDecisionRequest(ApiModel):
    candidate_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS,
    )
    filter: RelationshipCandidateBulkSelectionFilter | None = None
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_selection(self) -> "RelationshipCandidateBulkDecisionRequest":
        _require_exactly_one_selection(self.candidate_ids, self.filter)
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting relationship candidates")
        return self


class RelationshipCandidateBulkDecisionItemRead(ApiModel):
    candidate_id: str
    status: Literal["SUCCEEDED", "FAILED"]
    reason: str | None = None


class RelationshipCandidateBulkDecisionResultRead(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    selection_mode: Literal["EXPLICIT", "FILTER"]
    requested_count: int
    succeeded_count: int
    failed_count: int
    truncated: bool
    results: list[RelationshipCandidateBulkDecisionItemRead]


class RelationshipCandidateCalibrationBucketRead(ApiModel):
    confidence_low: float
    confidence_high: float
    decided_count: int
    approved_count: int
    rejected_count: int
    observed_approval_rate: float | None


class RelationshipCandidateCalibrationRead(ApiModel):
    datasource_id: UUID | None
    bucket_width: float
    total_decided: int
    ground_truth_overrides_applied: int
    buckets: list[RelationshipCandidateCalibrationBucketRead]
    methodology_note: str


class CanonicalTableMappingRead(ApiModel):
    """RL-2: the steward-set canonical member for an APPROVED table family, if any.

    ``resolved_by``/``rationale`` describe the steward decision behind this
    row -- there is no row at all for a family that has not been explicitly
    overridden; see ``resolve_canonical`` for how that case falls back to
    ``TableFamilyCandidate.base_table_id``.
    """

    id: UUID
    organization_id: UUID
    family_candidate_id: UUID
    canonical_table_id: UUID
    canonical_qualified_name: str
    resolved_by: str
    rationale: str
    is_steward_override: bool
    created_at: datetime
    updated_at: datetime


class CanonicalTableOverrideRequest(ApiModel):
    """Steward decision naming (or clearing) the canonical member of an APPROVED family.

    ``table_id`` must be one of the family's ``member_table_ids``; it is
    only accepted against an APPROVED ``TableFamilyCandidate``. Setting it to
    ``None`` clears an existing override and reverts resolution to the
    family's own ``base_table_id`` (``None`` for a family type -- e.g.
    SNAPSHOT -- where the algorithm never picks one) -- itself an auditable
    decision, so a rationale is always required either way.
    """

    table_id: UUID | None = None
    rationale: str = Field(min_length=1, max_length=2000)




class CompositeRelationshipCandidateDiscoveryRequest(ApiModel):
    max_candidates: int = Field(default=200, ge=1, le=2_000)


class CompositeRelationshipCandidateMemberRead(ApiModel):
    ordinal: int
    source_column_id: UUID
    target_column_id: UUID
    source_column_name: str
    target_column_name: str


class CompositeRelationshipCandidateRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    source_table_id: UUID
    target_table_id: UUID
    detection_rule: str
    confidence: float
    evidence: dict[str, Any]
    status: str
    created_by: str
    reviewed_by: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    members: list[CompositeRelationshipCandidateMemberRead]
    created_at: datetime
    updated_at: datetime
class GraphNodeRead(ApiModel):
    id: UUID
    node_type: Literal["TABLE"]
    label: str
    qualified_name: str
    object_type: str
    status: str
    column_count: int
    sensitive_column_count: int
    depth: int = 0
    inbound_edge_count: int = 0
    outbound_edge_count: int = 0


class GraphEdgeRead(ApiModel):
    id: str
    edge_type: Literal["DECLARED_FOREIGN_KEY", "SUGGESTED_RELATIONSHIP"]
    source_node_id: UUID
    target_node_id: UUID
    source_label: str
    target_label: str
    source_columns: list[str]
    target_columns: list[str]
    status: str
    confidence: float
    evidence: dict[str, Any]
    candidate_id: UUID | None = None


class KnowledgeGraphRead(ApiModel):
    datasource_id: UUID
    nodes: list[GraphNodeRead]
    edges: list[GraphEdgeRead]
    total_tables: int
    total_declared_edges: int
    total_suggested_edges: int
    pending_suggestions: int
    truncated: bool
    focus_node_id: UUID | None = None
    direction: Literal["BOTH", "REFERENCES", "REFERENCED_BY"] = "BOTH"
    requested_depth: int = 0
    returned_node_count: int = 0
    returned_edge_count: int = 0
    node_limit: int = 0
    edge_limit: int = 0
    truncation_reasons: list[str] = Field(default_factory=list)


class GraphSearchRead(ApiModel):
    datasource_id: UUID
    query: str
    items: list[GraphNodeRead]
    total: int
    truncated: bool


UnifiedLineageNodeKind = Literal[
    "TABLE", "DBT_MODEL", "DBT_SOURCE", "DBT_SEED", "DBT_SNAPSHOT", "UNRESOLVED_DATASET"
]
UnifiedLineageEdgeSource = Literal[
    "FOREIGN_KEY",
    "SUGGESTED_RELATIONSHIP",
    "DBT_DEPENDENCY",
    "OPENLINEAGE_ETL",
    "VIEW_DEFINITION",
    "PROCEDURE_DEFINITION",
]


class UnifiedLineageNodeRead(ApiModel):
    """One node in the merged lineage graph: a catalog table, or -- when a dbt
    resource or OpenLineage dataset has not been matched to one -- a synthetic
    node so the graph stays connected instead of silently dropping it."""

    id: str
    node_kind: UnifiedLineageNodeKind
    label: str
    qualified_name: str
    matched_table_id: UUID | None = None
    resolved: bool = True
    depth: int = 0
    inbound_edge_count: int = 0
    outbound_edge_count: int = 0


class UnifiedLineageEdgeRead(ApiModel):
    """One typed edge merged from declared FKs, approved/candidate column
    relationships, dbt manifest dependencies, OpenLineage table edges, or
    SQL-parsed view/procedure definition edges (LN-2)."""

    id: str
    edge_source: UnifiedLineageEdgeSource
    source_node_id: str
    target_node_id: str
    source_label: str
    target_label: str
    status: str
    confidence: float
    source_columns: list[str] = Field(default_factory=list)
    target_columns: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class UnifiedLineageGraphRead(ApiModel):
    datasource_id: UUID
    nodes: list[UnifiedLineageNodeRead]
    edges: list[UnifiedLineageEdgeRead]
    counts_by_source: dict[str, int]
    returned_node_count: int = 0
    returned_edge_count: int = 0
    node_limit: int = 0
    edge_limit: int = 0
    truncated: bool = False
    truncation_reasons: list[str] = Field(default_factory=list)


class DomainLineageGraphRead(ApiModel):
    """Same merged FK + suggested + dbt + OpenLineage + view/procedure graph as
    UnifiedLineageGraphRead, federated across every datasource in one
    data_domain (ADR-0017 SS3/SS6) instead of scoped to a single datasource.

    This is a federated bounded view, not a global graph query: each
    contributing datasource's graph is built and capped by the existing,
    unchanged single-datasource builder, then merged under the domain's
    combined node_limit/edge_limit -- ADR-0010's bounded/lazy/value-free
    contract holds at this wider scope exactly as it does at the narrower
    one. Node and edge ids are prefixed per-datasource to guarantee no
    false merge between two different datasources' same-named synthetic
    (unmatched dbt/OpenLineage) nodes.

    A candidate relationship reaching across a data_domain boundary only
    ever renders as an edge here when an ACTIVE cross_boundary_grant permits
    this domain to see into the other one (ADR-0017 SS4, INV-5: deny-by-
    default, never inherited) -- `withheld_cross_boundary_domain_ids` names
    any domain that has such a candidate but no covering grant, reported
    rather than silently dropped, mirroring the withheld:"no_grant"
    transparency this ADR requires everywhere it applies.
    """

    data_domain_id: UUID
    datasource_ids: list[UUID]
    nodes: list[UnifiedLineageNodeRead]
    edges: list[UnifiedLineageEdgeRead]
    counts_by_source: dict[str, int]
    returned_node_count: int = 0
    returned_edge_count: int = 0
    node_limit: int = 0
    edge_limit: int = 0
    truncated: bool = False
    truncation_reasons: list[str] = Field(default_factory=list)
    withheld_cross_boundary_domain_ids: list[UUID] = Field(default_factory=list)


class UnifiedLineageImpactNodeRead(ApiModel):
    node_id: str
    node_kind: UnifiedLineageNodeKind
    label: str
    qualified_name: str
    depth: int
    contributing_edge_sources: list[UnifiedLineageEdgeSource]
    # DQ-3 (module 11 §9, "Impact surfacing"): the same PASSING/STALE/UNKNOWN/
    # INCIDENT_OPEN vocabulary `catalog_read_model._quality_state` already
    # computes for the Catalog screen, not a new one. "NOT_APPLICABLE" for a
    # non-TABLE node (an unmatched dbt/OpenLineage node has no
    # `DataQualityIncident.table_id` to look up).
    quality_state: str = "NOT_APPLICABLE"


class UnifiedLineageImpactRead(ApiModel):
    """Transitive upstream/downstream impact, replacing direct-reference
    counting with a bounded multi-hop traversal of the unified graph."""

    datasource_id: UUID
    focus_node_id: str
    focus_node_kind: UnifiedLineageNodeKind
    focus_label: str
    upstream: list[UnifiedLineageImpactNodeRead]
    downstream: list[UnifiedLineageImpactNodeRead]
    requested_depth: int
    node_limit: int
    upstream_truncated: bool
    downstream_truncated: bool


class ToolDeprecationDependentToolRead(ApiModel):
    """Another PUBLISHED governed tool that depends on a table the
    deprecating version depends on, or a table transitively downstream of
    one (TL-7)."""

    tool_version_id: UUID
    tool_id: UUID
    slug: str
    version: int
    name: str
    shared_table_count: int


class ToolDeprecationDependentContextProductRead(ApiModel):
    """A PUBLISHED context product that would be affected by this tool
    version's deprecation (TL-7)."""

    context_product_version_id: UUID
    product_id: UUID
    product_key: str
    version: int
    name: str
    reason: Literal["ELIGIBLE_TOOL", "SHARED_TABLE"]


class ToolDeprecationImpactRead(ApiModel):
    """Blast radius of deprecating a governed tool version, computed fresh
    against live data -- never a stale, post-hoc snapshot (TL-7).

    `downstream_nodes` reuses LN-7's own node shape
    (`UnifiedLineageImpactNodeRead`) because it *is* LN-7's traversal,
    seeded from this version's own declared `referenced_tables`.
    """

    tool_version_id: UUID
    tool_id: UUID
    slug: str
    version: int
    status: str
    dependency_tables: list[str]
    downstream_nodes: list[UnifiedLineageImpactNodeRead]
    downstream_truncated: bool
    dependent_tool_versions: list[ToolDeprecationDependentToolRead]
    dependent_context_products: list[ToolDeprecationDependentContextProductRead]
    active_consumer_count: int
    recent_execution_count: int
    lookback_days: int
    requested_depth: int
    node_limit: int
    total_blast_radius: int


class SemanticInferenceRequest(ApiModel):
    max_tables: int = Field(default=100, ge=1, le=100)
    use_model: bool = True


class SemanticInferenceRunRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    analysis_run_id: UUID | None
    status: str
    engine_mode: str
    engine_version: str
    model_route: str | None
    table_count: int
    proposal_count: int
    model_enriched_count: int
    rule_only_count: int
    created_by: str
    completed_at: datetime | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime


class MetadataEnrichmentProposalRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    inference_run_id: UUID
    table_id: UUID
    governance_review_id: UUID
    schema_name: str
    table_name: str
    proposal_type: str
    status: str
    engine_type: str
    engine_version: str
    confidence: float
    payload: dict[str, Any]
    evidence: dict[str, Any]
    fingerprint: str
    proposed_by: str
    reviewed_by: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    promoted_tool_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MetadataBusinessAnnotationRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    table_id: UUID
    schema_name: str
    table_name: str
    domain_id: UUID
    domain_key: str
    domain_name: str
    entity_id: UUID
    entity_key: str
    entity_name: str
    source_proposal_id: UUID
    version: int
    business_name: str
    business_description: str
    table_role: str
    grain_statement: str
    synonyms: list[str]
    suggested_questions: list[str]
    tags: list[str]
    confidence: float
    approved_by: str
    approved_at: datetime
    created_at: datetime
    updated_at: datetime


class GlossaryTermCreate(ApiModel):
    term_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    display_name: str = Field(min_length=2, max_length=200)
    definition: str = Field(min_length=10, max_length=10_000)
    category_id: UUID | None = None
    synonyms: list[str] = Field(default_factory=list, max_length=50)
    owner_principal: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_synonyms(self) -> "GlossaryTermCreate":
        self.synonyms = _normalized_terms(self.synonyms, "synonyms")
        return self


class GlossaryTermVersionCreate(ApiModel):
    display_name: str = Field(min_length=2, max_length=200)
    definition: str = Field(min_length=10, max_length=10_000)
    category_id: UUID | None = None
    synonyms: list[str] = Field(default_factory=list, max_length=50)
    owner_principal: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_synonyms(self) -> "GlossaryTermVersionCreate":
        self.synonyms = _normalized_terms(self.synonyms, "synonyms")
        return self


class GlossaryTermVersionRead(ApiModel):
    id: UUID
    organization_id: UUID
    term_id: UUID
    term_key: str
    category_id: UUID | None
    lifecycle_status: str
    version: int
    status: str
    display_name: str
    definition: str
    synonyms: list[str]
    owner_principal: str | None
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AssetDocumentationVersionCreate(ApiModel):
    aliases: list[str] = Field(default_factory=list, max_length=50)
    readme: str = Field(min_length=10, max_length=50_000)
    owner_principal: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_aliases(self) -> "AssetDocumentationVersionCreate":
        normalized = [alias.strip() for alias in self.aliases]
        if any(not alias or len(alias) > 200 for alias in normalized):
            raise ValueError("aliases must contain between 1 and 200 visible characters")
        if len({alias.casefold() for alias in normalized}) != len(normalized):
            raise ValueError("aliases must be unique ignoring case")
        self.aliases = normalized
        return self


class AssetDocumentationVersionRead(ApiModel):
    id: UUID
    organization_id: UUID
    documentation_id: UUID
    table_id: UUID
    version: int
    status: str
    aliases: list[str]
    readme: str
    owner_principal: str | None
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AssetTermLinkCreate(ApiModel):
    term_id: UUID


class AssetTermLinkRead(ApiModel):
    id: UUID
    organization_id: UUID
    table_id: UUID
    term_id: UUID
    term_key: str
    display_name: str
    definition: str
    linked_by: str
    link_type: str
    confidence: float
    source_annotation_id: UUID | None
    created_at: datetime


def _normalized_terms(values: list[str], field_name: str) -> list[str]:
    normalized = [value.strip() for value in values]
    if any(not value or len(value) > 200 for value in normalized):
        raise ValueError(f"{field_name} must contain between 1 and 200 visible characters")
    if len({value.casefold() for value in normalized}) != len(normalized):
        raise ValueError(f"{field_name} must be unique ignoring case")
    return normalized


class GlossaryCategoryCreate(ApiModel):
    category_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    display_name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=5, max_length=5000)
    parent_id: UUID | None = None


class GlossaryCategoryRead(GlossaryCategoryCreate):
    id: UUID
    organization_id: UUID
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class GlossaryTermDeprecationRequest(ApiModel):
    reason: str = Field(min_length=10, max_length=2000)


class OwnershipRuleCreate(ApiModel):
    rule_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    display_name: str = Field(min_length=2, max_length=200)
    match_field: Literal["TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME", "DOMAIN_KEY", "TAG"]
    match_pattern: str = Field(min_length=1, max_length=255)
    owner_type: Literal["INDIVIDUAL", "GROUP"]
    owner_principal: str = Field(min_length=2, max_length=255)


class OwnershipRuleRead(OwnershipRuleCreate):
    id: UUID
    organization_id: UUID
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class OwnershipAssignmentRead(ApiModel):
    id: UUID
    organization_id: UUID
    subject_type: str
    subject_id: str
    owner_type: str
    owner_principal: str
    assignment_kind: str
    source_rule_id: UUID | None
    status: str
    assigned_by: str
    # P2-07: re-affirmation cadence surface. `expires_at` is nullable so
    # legacy pre-P2-07 rows read back as "no expiry", not as a validation
    # error. The four fields below are all writer-populated -- reaffirmed_*
    # by the `/reaffirm` endpoint, expiry_warning_emitted_at by the sweep.
    expires_at: datetime | None = None
    expiry_warning_emitted_at: datetime | None = None
    reaffirmed_at: datetime | None = None
    reaffirmed_by: str | None = None
    created_at: datetime
    updated_at: datetime


class OwnershipAssignmentBulkReaffirmRequest(ApiModel):
    """P2-07: body for `POST /v1/ownership-assignments/bulk-reaffirm`.

    Bounded at 100 ids per call to match the same limit
    `bulk_decide_relationship_candidates` and other bulk endpoints use --
    keeps the SAVEPOINT loop's worst-case wall time predictable.
    """

    assignment_ids: list[UUID] = Field(min_length=1, max_length=100)


class OwnershipAssignmentBulkReaffirmItemResult(ApiModel):
    assignment_id: UUID
    outcome: str  # "REAFFIRMED" | "NOT_FOUND" | "FORBIDDEN" | "ERROR"
    detail: str | None = None


class OwnershipAssignmentBulkReaffirmResult(ApiModel):
    reaffirmed: int
    skipped: int
    items: list[OwnershipAssignmentBulkReaffirmItemResult]


class BulkStewardshipOperationCreate(ApiModel):
    operation_type: Literal["ASSIGN_OWNERSHIP", "LINK_TERM", "DEPRECATE_TERM", "CERTIFY_ASSET"]
    subject_type: Literal["TABLE", "TERM"]
    subject_ids: list[UUID] = Field(min_length=1, max_length=500)
    owner_type: Literal["INDIVIDUAL", "GROUP"] | None = None
    owner_principal: str | None = Field(default=None, max_length=255)
    term_id: UUID | None = None
    rationale: str | None = Field(default=None, max_length=2000)
    expires_at: datetime | None = None
    source_rule_id: UUID | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> "BulkStewardshipOperationCreate":
        if len(set(self.subject_ids)) != len(self.subject_ids):
            raise ValueError("subject_ids must be unique")
        if self.operation_type == "ASSIGN_OWNERSHIP" and not (
            self.owner_type and self.owner_principal
        ):
            raise ValueError("ownership operations require owner_type and owner_principal")
        if self.operation_type == "LINK_TERM" and self.term_id is None:
            raise ValueError("link operations require term_id")
        if self.operation_type in {"DEPRECATE_TERM", "CERTIFY_ASSET"} and not self.rationale:
            raise ValueError("deprecation and certification operations require rationale")
        if self.operation_type == "CERTIFY_ASSET" and self.expires_at is None:
            raise ValueError("certification operations require expires_at")
        return self


class BulkStewardshipOperationRead(ApiModel):
    id: UUID
    organization_id: UUID
    operation_type: str
    subject_type: str
    subject_ids: list[str]
    parameters: dict[str, Any]
    status: str
    governance_review_id: UUID
    requested_by: str
    applied_by: str | None
    applied_at: datetime | None
    applied_count: int
    created_at: datetime
    updated_at: datetime


class LeaverReassignmentRequest(ApiModel):
    """GL-7: reassign every ACTIVE `OwnershipAssignment` a leaving principal
    holds -- table ownership, glossary-term stewardship, and any other
    subject_type GL-2's ownership model covers -- to a successor in one
    governed action. `assignment_ids`, when given, must name only ACTIVE
    assignments currently owned by `leaving_principal` (an explicit,
    caller-chosen subset of the portfolio); omitted, the endpoint discovers
    the leaving principal's whole current portfolio server-side.
    """

    leaving_principal: str = Field(min_length=1, max_length=255)
    successor_principal: str = Field(min_length=1, max_length=255)
    owner_type: Literal["INDIVIDUAL", "GROUP"] = "INDIVIDUAL"
    assignment_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=500)
    rationale: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def validate_leaver_reassignment(self) -> "LeaverReassignmentRequest":
        if self.leaving_principal == self.successor_principal:
            raise ValueError("successor_principal must differ from leaving_principal")
        if self.assignment_ids is not None and len(set(self.assignment_ids)) != len(
            self.assignment_ids
        ):
            raise ValueError("assignment_ids must be unique")
        return self


class GlossaryConflictCreate(ApiModel):
    term_id: UUID | None = None
    conflict_type: Literal["DEFINITION", "SYNONYM_COLLISION", "SOURCE_DISAGREEMENT"]
    position_a: dict[str, Any]
    position_b: dict[str, Any]
    assigned_owner: str | None = Field(default=None, max_length=255)


class GlossaryConflictResolution(ApiModel):
    resolution: Literal["ACCEPT_POSITION_A", "ACCEPT_POSITION_B", "MERGE", "RETAIN_BOTH"]
    resolved_definition: str | None = Field(default=None, max_length=10_000)
    rationale: str = Field(min_length=10, max_length=2000)


class GlossaryConflictRead(ApiModel):
    id: UUID
    organization_id: UUID
    term_id: UUID | None
    conflict_type: str
    status: str
    position_a: dict[str, Any]
    position_b: dict[str, Any]
    assigned_owner: str | None
    raised_by: str
    proposed_resolution: str | None
    proposed_definition: str | None
    resolution_rationale: str | None
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GlossaryLinkProposalGenerate(ApiModel):
    minimum_confidence: float = Field(default=0.75, ge=0.5, le=1.0)
    limit: int = Field(default=200, ge=1, le=500)


class GlossaryLinkProposalRead(ApiModel):
    id: UUID
    organization_id: UUID
    table_id: UUID
    term_id: UUID
    term_display_name: str
    table_name: str
    source_annotation_id: UUID
    confidence: float
    evidence: dict[str, Any]
    status: str
    governance_review_id: UUID | None
    created_by: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AssetDescriptionDraftGenerate(ApiModel):
    table_ids: list[UUID] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_table_ids(self) -> "AssetDescriptionDraftGenerate":
        if len(set(self.table_ids)) != len(self.table_ids):
            raise ValueError("table_ids must be unique")
        return self


class AssetDescriptionDraftRead(ApiModel):
    id: UUID
    organization_id: UUID
    table_id: UUID
    table_name: str
    drafted_text: str
    accuracy_score: float
    clarity_score: float
    style_score: float
    completeness_score: float
    overall_score: float
    evidence: dict[str, Any]
    status: str
    governance_review_id: UUID | None
    published_version_id: UUID | None
    created_by: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CoverageDimensionRead(ApiModel):
    covered: int
    total: int
    percentage: float


class StewardshipCoverageRead(ApiModel):
    organization_id: UUID
    datasource_id: UUID | None
    domain_id: UUID | None = None
    line_of_business_id: UUID | None = None
    table_count: int
    overall_score: float
    dimensions: dict[str, CoverageDimensionRead]
    unowned_table_ids: list[UUID]
    computed_at: datetime


class CoverageSnapshotRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID | None
    domain_id: UUID | None
    line_of_business_id: UUID | None
    table_count: int
    dimensions: dict[str, CoverageDimensionRead]
    overall_score: float
    computed_by: str
    created_at: datetime


class UnownedAssetEscalationRead(ApiModel):
    id: UUID
    organization_id: UUID
    table_id: UUID
    first_detected_unowned_at: datetime
    status: str
    candidate_owner: str | None
    notification_rule_id: UUID | None
    channel: str | None
    recipients: list[str]
    dedup_key: str | None
    routed_at: datetime | None
    escalated_at: datetime | None
    escalated_tier2_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UnownedAssetBacklogRouteRequest(ApiModel):
    datasource_id: UUID | None = None
    domain_id: UUID | None = None
    line_of_business_id: UUID | None = None


class UnownedAssetBacklogRouteResult(ApiModel):
    organization_id: UUID
    routed: list[UnownedAssetEscalationRead]
    escalated: list[UnownedAssetEscalationRead]
    escalated_tier2: list[UnownedAssetEscalationRead]
    resolved_count: int


class BusinessMapNodeRead(ApiModel):
    id: str
    node_type: Literal["DOMAIN", "ENTITY", "TABLE"]
    label: str
    parent_id: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BusinessMapEdgeRead(ApiModel):
    id: str
    edge_type: Literal[
        "DOMAIN_CONTAINS_ENTITY",
        "ENTITY_REPRESENTED_BY_TABLE",
        "CROSS_DOMAIN_FOREIGN_KEY",
    ]
    source_node_id: str
    target_node_id: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class BusinessMapRead(ApiModel):
    organization_id: UUID
    nodes: list[BusinessMapNodeRead]
    edges: list[BusinessMapEdgeRead]
    domain_count: int
    entity_count: int
    table_count: int
    cross_domain_edge_count: int
    truncated: bool


class ModelRouteConfigurationCreate(ApiModel):
    route_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    display_name: str = Field(min_length=3, max_length=200)
    provider_type: Literal[
        "OPENAI",
        "GOOGLE_GEMINI",
        "AZURE_OPENAI",
        "AWS_BEDROCK",
        "GOOGLE_VERTEX",
        "OPENAI_COMPATIBLE_PRIVATE",
        "ON_PREM",
    ]
    model_id: str = Field(min_length=2, max_length=255)
    endpoint_alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,254}$")
    credential_reference: str | None = Field(default=None, max_length=1000)
    data_residency: str = Field(min_length=2, max_length=100)
    retention_policy: Literal["ZERO_RETENTION", "BANK_MANAGED", "PROVIDER_CONTRACT"]
    capabilities: list[Literal["SQL_GENERATION", "EXPLANATION", "EMBEDDINGS", "CLASSIFICATION"]] = (
        Field(min_length=1, max_length=4)
    )
    max_input_tokens: int = Field(default=8000, ge=100, le=1_000_000)
    max_output_tokens: int = Field(default=2000, ge=100, le=100_000)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class ModelRouteConfigurationRead(ApiModel):
    id: UUID
    organization_id: UUID
    route_key: str
    version: int
    status: str
    display_name: str
    provider_type: str
    model_id: str
    endpoint_alias: str
    uses_credential_reference: bool
    data_residency: str
    retention_policy: str
    capabilities: list[str]
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int
    fingerprint: str
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    selected_by_runtime: bool
    adapter_available: bool
    activation_status: str
    created_at: datetime
    updated_at: datetime


class KillSwitchEngageRequest(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)
    route_key: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")


class KillSwitchReleaseRequest(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)
    route_key: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")


class KillSwitchStateRead(ApiModel):
    id: UUID
    organization_id: UUID
    route_key: str
    scope: Literal["ORGANIZATION", "ROUTE"]
    engaged: bool
    reason: str | None
    engaged_by: str | None
    engaged_at: datetime | None
    released_by: str | None
    released_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OpenLineageIngestRequest(ApiModel):
    datasource_id: UUID
    event: dict[str, Any]


class OpenLineageDatasetRead(ApiModel):
    id: UUID
    direction: str
    namespace: str
    name: str
    matched_table_id: UUID | None
    schema_fields: list[str]
    created_at: datetime
    updated_at: datetime


class OpenLineageTableEdgeRead(ApiModel):
    id: UUID
    input_dataset_namespace: str
    input_dataset_name: str
    input_table_id: UUID | None
    output_dataset_namespace: str
    output_dataset_name: str
    output_table_id: UUID | None
    edge_kind: str
    created_at: datetime
    updated_at: datetime


class OpenLineageColumnEdgeRead(ApiModel):
    id: UUID
    input_dataset_namespace: str
    input_dataset_name: str
    input_table_id: UUID | None
    input_column_name: str
    output_dataset_namespace: str
    output_dataset_name: str
    output_table_id: UUID | None
    output_column_name: str
    transformation_type: str | None
    transformation_subtype: str | None
    edge_kind: str
    created_at: datetime
    updated_at: datetime


class OpenLineageRunEventRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    event_fingerprint: str
    event_type: str
    event_time: datetime
    producer: str
    schema_url: str | None
    job_namespace: str
    job_name: str
    run_id: str
    status: str
    input_dataset_count: int
    output_dataset_count: int
    table_edge_count: int
    column_edge_count: int
    unresolved_dataset_count: int
    imported_by: str
    created_at: datetime
    updated_at: datetime
    datasets: list[OpenLineageDatasetRead] = Field(default_factory=list)
    table_edges: list[OpenLineageTableEdgeRead] = Field(default_factory=list)
    column_edges: list[OpenLineageColumnEdgeRead] = Field(default_factory=list)


class DbtProjectCreate(ApiModel):
    project_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    display_name: str = Field(min_length=2, max_length=200)
    datasource_id: UUID
    repository_url: str | None = Field(default=None, max_length=1000)
    target_name: str = Field(default="prod", min_length=1, max_length=100)


class DbtProjectRead(ApiModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    datasource_id: UUID
    project_key: str
    display_name: str
    repository_url: str | None
    target_name: str
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class DbtArtifactImportRequest(ApiModel):
    manifest: dict[str, Any]
    catalog: dict[str, Any] | None = None
    run_results: dict[str, Any] | None = None


class DbtArtifactImportRead(ApiModel):
    id: UUID
    organization_id: UUID
    dbt_project_id: UUID
    manifest_fingerprint: str
    dbt_schema_version: str
    dbt_version: str | None
    invocation_id: str | None
    generated_at: datetime | None
    status: str
    resource_count: int
    model_count: int
    source_count: int
    test_count: int
    lineage_edge_count: int
    matched_resource_count: int
    unmatched_resource_count: int
    imported_by: str
    created_at: datetime
    updated_at: datetime


class DbtResourceRead(ApiModel):
    id: UUID
    artifact_import_id: UUID
    unique_id: str
    resource_type: str
    package_name: str
    name: str
    database_name: str | None
    schema_name: str | None
    relation_name: str | None
    materialization: str | None
    original_file_path: str | None
    description: str | None
    compiled_sql_hash: str | None
    compiled_sql_redacted: str | None
    sql_parse_status: str
    column_names: list[str]
    column_descriptions: dict[str, str] = Field(default_factory=dict)
    column_types: dict[str, str] = Field(default_factory=dict)
    tags: list[str]
    depends_on_unique_ids: list[str]
    matched_table_id: UUID | None
    test_status: str | None = None
    test_failures: int | None = None
    test_execution_time: float | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class DbtLineageNodeRead(ApiModel):
    id: UUID
    unique_id: str
    label: str
    resource_type: str
    materialization: str | None
    matched_table_id: UUID | None
    test_status: str | None = None


class DbtLineageEdgeRead(ApiModel):
    id: UUID
    source_resource_id: UUID
    target_resource_id: UUID
    edge_type: str
    source_column: str = ""
    target_column: str = ""
    transformation_type: str | None = None
    confidence: str | None = None


class DbtLineageRead(ApiModel):
    artifact_import_id: UUID
    nodes: list[DbtLineageNodeRead]
    edges: list[DbtLineageEdgeRead]
    resource_count: int
    edge_count: int
    catalog_match_count: int


class ImpactAnalysisRead(ApiModel):
    table_id: UUID
    table_name: str
    semantic_metric_version_ids: list[UUID]
    governed_tool_version_ids: list[UUID]
    approved_relationship_candidate_ids: list[UUID]
    dbt_resource_ids: list[UUID] = Field(default_factory=list)
    downstream_object_count: int


class DataQualityPolicyUpsert(ApiModel):
    table_id: UUID | None = None
    name: str = Field(default="Source baseline controls", min_length=3, max_length=200)
    enabled: bool = True
    volume_change_percent: float = Field(default=30.0, gt=0, le=1000)
    null_rate_change_percent: float = Field(default=10.0, gt=0, le=100)
    schema_change_enabled: bool = True
    metadata_scan_max_age_minutes: int = Field(default=1440, ge=5, le=525600)


class DataQualityPolicyRead(DataQualityPolicyUpsert):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    scope_key: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class DataQualityObservationRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    table_id: UUID
    table_name: str
    analysis_run_id: UUID
    baseline_profile_id: UUID | None
    policy_id: UUID | None
    status: str
    quality_score: int
    anomaly_types: list[str]
    evidence: dict[str, Any]
    policy_snapshot: dict[str, Any]
    created_at: datetime


class DataQualityIncidentRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    table_id: UUID
    table_name: str
    policy_id: UUID | None
    latest_observation_id: UUID | None = None
    anomaly_type: str
    severity: str
    status: str
    # DQ-8: "INTERNAL" for Atlas's own detectors, "EXTERNAL" for incidents
    # reconciled from a third-party detector signal. Defaulted so historical
    # rows and internal detectors deserialize unchanged.
    source: str = "INTERNAL"
    summary: str
    evidence: dict[str, Any]
    occurrence_count: int
    first_observed_at: datetime
    last_observed_at: datetime
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    resolved_by: str | None
    resolved_at: datetime | None
    resolution_reason: str | None
    created_at: datetime
    updated_at: datetime


class DataQualityIncidentTransition(ApiModel):
    status: Literal["ACKNOWLEDGED", "RESOLVED"]
    reason: str = Field(min_length=3, max_length=1000)


# --- DQ-8: external detector signal ingest --------------------------------------

# A conservative allow-list of scalar JSON leaf types for the opaque ``details``
# blob. INV-6/ADR-0014: the control plane never stores source *row values*; a
# third-party detector's payload is metadata (thresholds, monitor names, rates,
# counts, links) and is accepted as such, but this bounds what a caller can smuggle
# in and keeps the blob free-form without becoming a value sink.
_EXTERNAL_SIGNAL_DETAILS_MAX_KEYS = 50


class ExternalQualitySignalIngest(ApiModel):
    """Normalized inbound envelope for a third-party detector quality signal.

    The caller (or a thin per-vendor adapter) normalizes the detector's native
    payload into this shape: vendor, the Atlas asset it targets, a normalized
    severity and open/resolved state, the detector's own rule/monitor id, when it
    was observed, and an opaque metadata ``details`` blob.
    """

    detector_vendor: str = Field(min_length=2, max_length=50)
    detector_native_id: str = Field(min_length=1, max_length=255)
    table_id: UUID
    column_id: UUID | None = None
    severity: Literal["CRITICAL", "WARNING", "INFO"]
    # Normalized lifecycle state: OPEN drives incident open/reopen, RESOLVED
    # auto-resolves the matching incident. A vendor's richer native state can be
    # retained in ``details`` but must be normalized to one of these two here.
    signal_status: Literal["OPEN", "RESOLVED"]
    summary: str = Field(min_length=1, max_length=500)
    observed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("detector_vendor")
    @classmethod
    def _normalize_vendor(cls, value: str) -> str:
        # Canonicalize to an upper-snake token (e.g. "Monte Carlo" -> "MONTE_CARLO")
        # so the same vendor is one namespace regardless of caller casing/spacing.
        token = "_".join(value.strip().upper().split())
        if not token:
            raise ValueError("detector_vendor must not be blank")
        return token

    @field_validator("details")
    @classmethod
    def _validate_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > _EXTERNAL_SIGNAL_DETAILS_MAX_KEYS:
            raise ValueError(
                f"details may carry at most {_EXTERNAL_SIGNAL_DETAILS_MAX_KEYS} keys"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("details keys must be strings")
            if isinstance(item, dict | list):
                raise ValueError(
                    "details must be a flat metadata map of scalar values; nested "
                    "objects/arrays are rejected to keep the control plane value-free (INV-6)"
                )
        return value


class ExternalQualitySignalRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    table_id: UUID
    column_id: UUID | None
    incident_id: UUID | None
    detector_vendor: str
    detector_native_id: str
    severity: str
    signal_status: str
    summary: str
    observed_at: datetime
    details: dict[str, Any]
    created_by: str
    created_at: datetime


class ExternalQualitySignalIngestResult(ApiModel):
    signal: ExternalQualitySignalRead
    # True when this delivery matched an already-stored signal on
    # (vendor, native id, observed_at) and no new incident work was done.
    deduplicated: bool
    incident_opened: bool
    incident_resolved: bool


class DataQualitySummaryRead(ApiModel):
    datasource_id: UUID
    table_count: int
    observed_table_count: int
    status_counts: dict[str, int]
    open_incident_count: int
    critical_incident_count: int
    average_quality_score: float | None
    last_observed_at: datetime | None
    metadata_scan_age_minutes: float | None
    metadata_scan_status: str
    source_freshness_status: Literal["NOT_CONFIGURED"]


class QualityRulePackUpsert(ApiModel):
    name: str = Field(min_length=3, max_length=200)
    enabled: bool = True
    interval_minutes: int = Field(default=60, ge=5, le=10_080)


class QualityRulePackRead(QualityRulePackUpsert):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    created_by: str
    created_at: datetime
    updated_at: datetime


class QualityRuleUpsert(ApiModel):
    name: str = Field(min_length=3, max_length=200)
    rule_type: Literal["TABLE_ROW_COUNT_MIN", "TABLE_ROW_COUNT_MAX", "COLUMN_NULL_RATE_MAX"]
    table_id: UUID
    column_id: UUID | None = None
    threshold: float
    enabled: bool = True


class QualityRuleRead(QualityRuleUpsert):
    id: UUID
    organization_id: UUID
    rule_pack_id: UUID
    created_by: str
    created_at: datetime
    updated_at: datetime


class ContextProductQualityRequirements(ApiModel):
    minimum_score: int = Field(default=0, ge=0, le=100)
    deny_on_critical_incident: bool = True


def _default_context_product_actions() -> list[Literal["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"]]:
    return ["READ_CONTEXT"]


class ContextProductPolicySummary(ApiModel):
    source_values: Literal["GATEWAY_ONLY"] = "GATEWAY_ONLY"
    retention: Literal["NO_RAW_CONTEXT"] = "NO_RAW_CONTEXT"
    permitted_actions: list[Literal["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"]] = Field(
        default_factory=_default_context_product_actions
    )


class ContextProductDefinition(ApiModel):
    name: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=10_000)
    purpose: str = Field(min_length=10, max_length=1000)
    owner_type: Literal["INDIVIDUAL", "GROUP"]
    owner_principal: str = Field(min_length=2, max_length=255)
    table_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    semantic_model_version_ids: list[UUID] = Field(default_factory=list, max_length=100)
    glossary_term_version_ids: list[UUID] = Field(default_factory=list, max_length=500)
    eligible_tool_version_ids: list[UUID] = Field(default_factory=list, max_length=100)
    allowed_consumer_roles: list[str] = Field(min_length=1, max_length=50)
    lineage_depth: int = Field(default=2, ge=0, le=4)
    quality_requirements: ContextProductQualityRequirements = Field(
        default_factory=ContextProductQualityRequirements
    )
    policy_summary: ContextProductPolicySummary = Field(default_factory=ContextProductPolicySummary)
    # AT-7(a)/AT-D1: how long *this* version stays SUPPORTED (still readable
    # by a version-pinned consumer) after a later version supersedes it.
    # `None` means supported until someone explicitly retires it rather than
    # a fixed duration.
    support_window_days: int | None = Field(default=30, ge=0, le=3650)

    @model_validator(mode="after")
    def validate_bounded_definition(self) -> "ContextProductDefinition":
        reference_groups = (
            self.table_ids,
            self.semantic_model_version_ids,
            self.glossary_term_version_ids,
            self.eligible_tool_version_ids,
        )
        if not any(reference_groups):
            raise ValueError("a context product must include at least one governed reference")
        for values in reference_groups:
            if len(values) != len(set(values)):
                raise ValueError("context product reference identifiers must be unique")
        if len(self.allowed_consumer_roles) != len(set(self.allowed_consumer_roles)):
            raise ValueError("allowed consumer roles must be unique")
        if any(not role.strip() or len(role) > 100 for role in self.allowed_consumer_roles):
            raise ValueError("allowed consumer roles must be non-empty and at most 100 characters")
        return self


class ContextProductCreate(ContextProductDefinition):
    product_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")


class ContextProductVersionCreate(ContextProductDefinition):
    based_on_version_id: UUID | None = None


class ContextProductVersionUpdate(ContextProductDefinition):
    pass


class ContextProductVersionRead(ContextProductDefinition):
    id: UUID
    organization_id: UUID
    product_id: UUID
    product_key: str
    version: int
    status: str
    fingerprint: str
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    published_at: datetime | None
    based_on_version_id: UUID | None
    created_at: datetime
    updated_at: datetime
    # AT-7(a)/AT-D1: system-set, not part of what a maker submits.
    superseded_at: datetime | None = None
    support_window_ends_at: datetime | None = None
    superseded_by_version_id: UUID | None = None


class ContextProductRead(ApiModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    product_key: str
    lifecycle_status: str
    created_by: str
    latest_version: ContextProductVersionRead
    created_at: datetime
    updated_at: datetime


class ContextProductScopeRead(ApiModel):
    """Both ADR-0017 SS9 axes for one context product version, composed for an
    agent or MCP client deciding what it may actually retrieve: `data_domain_ids`
    is the tenancy axis ("where am I allowed to look"), `business_domain_names`
    is the business axis ("what does this represent") -- kept separate rather
    than conflated into one list, per ADR-0017 SS9. `ungranted_data_domain_ids`
    names any domain among them the product's own domain cannot see into today
    (no ACTIVE cross_boundary_grant) -- present, not silently included, mirroring
    the withheld:"no_grant" transparency ADR-0017 SS4 requires of graph traversal.
    """

    context_product_version_id: UUID
    product_data_domain_id: UUID
    data_domain_ids: list[UUID]
    ungranted_data_domain_ids: list[UUID]
    business_domain_names: list[str]
    cross_domain: bool
    table_count: int
    unresolved_table_ids: list[UUID]


class ContextProductConsumerBindingCreate(ApiModel):
    """AT-7(b): pin `consumer_principal_id` (the path parameter) to this
    version. Deliberately a single explicit version reference, not a
    percentage/weight -- the tracker declines blind A/B splits."""

    bound_version_id: UUID


class ContextProductConsumerBindingRead(ApiModel):
    id: UUID
    organization_id: UUID
    product_id: UUID
    consumer_principal_id: str
    bound_version_id: UUID
    bound_version_number: int
    created_by: str
    created_at: datetime
    updated_at: datetime


class LineageEdgeRead(ApiModel):
    """One column-level lineage edge extracted from SQL."""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    transformation_type: str
    confidence: str
    dialect: str


class ViewLineageParseRequest(ApiModel):
    sql: str = Field(min_length=1, max_length=500_000)
    dialect: str = Field(default="postgres", pattern=r"^[a-z][a-z0-9_-]{1,49}$")


class ViewLineageParseResponse(ApiModel):
    edges: list[LineageEdgeRead]
    confidence: str
    dialect: str
    sql_hash: str
    errors: list[str] = Field(default_factory=list)
    persisted_edge_count: int = 0


class ViewLineageEdgeRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    source_table_id: UUID | None
    source_column_id: UUID | None
    target_table_id: UUID | None
    target_column_id: UUID | None
    transformation_type: str
    confidence: str
    dialect: str
    sql_hash: str
    created_at: datetime
    updated_at: datetime


class ProcedureLineageEdgeRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    source_table_id: UUID | None
    source_column_id: UUID | None
    target_table_id: UUID | None
    target_column_id: UUID | None
    transformation_type: str
    confidence: str
    dialect: str
    sql_hash: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Group I addition (Atlas Wave-2, tracker N3/N12): API schemas for
# `procedure_lineage_api.py`'s routine-identity-aware, procedure-aware
# lineage parse (`aida.procedure_lineage`/`aida.procedure_lineage_models`).
# Purely additive -- nothing above this block is changed.
# ---------------------------------------------------------------------------


class DeepProcedureLineageEdgeRead(ApiModel):
    """One edge from the procedure-aware parser (N3) -- richer than
    `LineageEdgeRead`/`ProcedureLineageEdgeRead`: carries the statement it
    came from, whether that statement was a write, whether either side is an
    intermediate (temp table/variable) local to the procedure body, and,
    for a construct the parser could not resolve, the named reason why
    (never a silently dropped statement -- see `aida.procedure_lineage`'s
    module docstring)."""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    transformation_type: str
    confidence: str
    dialect: str
    source_resolved: bool
    statement_ordinal: int
    is_write: bool
    is_intermediate: bool
    control_flow_context: str | None = None
    unparsed_reason: str | None = None
    via_temp_table: str | None = None


class DeepProcedureLineageParseResponse(ApiModel):
    edges: list[DeepProcedureLineageEdgeRead]
    statement_count: int
    confidence: str
    dialect: str
    sql_hash: str
    errors: list[str] = Field(default_factory=list)
    # True iff every statement chunk in the body resolved to a concrete
    # DML/DDL shape or a genuinely lineage-free one (DECLARE/SET/structural
    # control-flow) -- zero UNPARSED chunks. N12 eligibility requires this.
    is_fully_parsed: bool
    # True iff `is_fully_parsed` AND no statement touched INSERT/UPDATE/
    # DELETE/MERGE/CREATE -- proven read-only, not merely "no write found".
    is_read_only: bool
    persisted_edge_count: int = 0


class ProcedureCapabilityConstructRead(ApiModel):
    """One row of the AT-22 parser capability matrix -- see
    `aida.procedure_capability_matrix`'s module docstring for how every
    field here is derived from the parsers' own dispatch code, not
    hand-typed. `construct_name`, not `construct` -- the latter shadows
    pydantic `BaseModel.construct`."""

    construct_name: str
    view_parser_status: str
    procedure_parser_status: str
    evidence: str


class ProcedureCapabilityMatrixRead(ApiModel):
    """AT-22: served live by `GET /v1/procedure-lineage/capability-matrix`,
    generated at request time from `sql_lineage_parser.py`'s and
    `procedure_lineage.py`'s own dispatch code -- the same source
    `scripts/generate_procedure_capability_matrix.py` uses to publish
    `Docs/90-reference/procedure-lineage-capability-matrix.md`, so the
    published page is verifiably backed by a live, callable source rather
    than only a one-off script."""

    generated_at: str
    dialects: list[str]
    constructs: list[ProcedureCapabilityConstructRead]
    unparsed_reasons: list[str]


# ---------------------------------------------------------------------------
# End Group I addition.
# ---------------------------------------------------------------------------


class StudioChangeSetCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)


class StudioChangeSetRead(ApiModel):
    id: UUID
    organization_id: UUID
    name: str
    author: str
    status: str
    base_version_hash: str
    conflict_status: str
    created_at: datetime
    updated_at: datetime


class StudioChangeItemCreate(ApiModel):
    object_type: Literal["METRIC", "TOOL", "TERM", "CONTEXT_PRODUCT"]
    object_id: str = Field(min_length=1, max_length=100)
    operation: Literal["CREATE", "UPDATE", "DELETE"]
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None


class StudioChangeItemRead(ApiModel):
    id: UUID
    organization_id: UUID
    change_set_id: UUID
    object_type: str
    object_id: str
    operation: str
    before_snapshot: dict[str, Any] | None
    after_snapshot: dict[str, Any] | None
    diff: dict[str, Any] | None
    test_status: str
    created_at: datetime
    updated_at: datetime


class StudioConflict(ApiModel):
    object_type: str
    object_id: str
    field_name: str
    change_set_value: Any
    current_value: Any


class StudioDiffEntry(ApiModel):
    field: str
    before: Any
    after: Any


class StudioDiffRead(ApiModel):
    change_set_id: UUID
    items: list[dict[str, Any]]


class StudioImpactPreview(ApiModel):
    change_set_id: UUID
    affected_object_count: int
    affected_objects: list[dict[str, Any]]


class StudioParameterContractValidateRequest(ApiModel):
    sql_template: str = Field(min_length=1, max_length=200_000)
    dialect: str = Field(default="postgres", min_length=1, max_length=50)
    parameters: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class StudioParameterContractValidateResult(ApiModel):
    valid: bool
    errors: list[str]
    definitions: list[dict[str, Any]]
    sample_rendered_sql: str | None = None


class StudioTestResultRead(ApiModel):
    id: UUID
    change_set_id: UUID
    started_at: datetime
    completed_at: datetime | None
    passed: bool
    evidence: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class StudioEvalQuestionRead(ApiModel):
    id: UUID
    organization_id: UUID
    object_type: str
    object_id: str
    evidence_source: str
    evidence_edge_id: str
    label: str
    mined_at: datetime
    created_at: datetime
    updated_at: datetime


class StudioEvalMiningResult(ApiModel):
    consumption_edges_scanned: int
    bi_edges_scanned: int
    questions_created: int
    questions_already_mined: int
    truncated: bool


class StudioEvalResultRead(ApiModel):
    eval_question_id: UUID
    object_type: str
    object_id: str
    label: str
    passed: bool
    evidence: dict[str, Any]


class StudioEvalRunRead(ApiModel):
    id: UUID
    change_set_id: UUID
    started_at: datetime
    completed_at: datetime | None
    passed: bool
    evidence: dict[str, Any]
    results: list[StudioEvalResultRead]


class HealthResponse(ApiModel):
    status: str
    service: str
    version: str
    dependencies: dict[str, str] = Field(default_factory=dict)


class Page(ApiModel):
    items: list[Any]
    limit: int
    offset: int
    total: int


class CursorPage(ApiModel):
    """CT-2: `Page` variant for the high-volume catalog list endpoints (tables,
    columns, constraints, indexes, partitions) that support genuine keyset
    pagination alongside the plain offset mode every other `Page` endpoint uses.

    Deliberately not a subclass of `Page`: `total` here is `int | None`, which
    would narrow-then-widen `Page.total: int` in an unsound way (mypy rightly
    rejects overriding a field with an incompatible type), and -- separately --
    `Page` is the response model for dozens of unrelated list endpoints, so
    widening it there directly would mark every one of them as having a
    breaking, no-longer-guaranteed `total` field in the OpenAPI contract, when
    only these five endpoints actually behave that way.
    """

    items: list[Any]
    limit: int
    offset: int
    # None only on the cursor (keyset) path: counting every matching row is
    # itself an O(n) scan, which cursor pagination exists to avoid, so it is
    # never computed there. The first request of a walk (`cursor=None`) always
    # populates it, same as `Page.total`.
    total: int | None = None
    # Present only when the request used cursor-based (keyset) pagination: an
    # opaque token for the next page, or None once no further rows remain.
    next_cursor: str | None = None


class CatalogRowRead(ApiModel):
    """UX-12: the composed catalog-table-list read model.

    One row assembles fields that otherwise live on five different endpoints
    keyed by table id (description, ownership, certification, quality,
    glossary terms, row estimate) -- see `aida.catalog_read_model` for how
    each field is sourced and composed. Mirrors `CatalogRowRead` in
    `ui-next/src/lib/types.ts` field-for-field; that file is the client
    already typed against this endpoint, so this schema follows it rather
    than the reverse.
    """

    id: UUID
    name: str
    schema_name: str
    datasource_name: str
    object_type: str
    status: str
    description: str | None
    # True when `description` came from an unreviewed proposal (a pending
    # GL-9 draft) rather than an approved source. ADR-0001: models propose,
    # humans and deterministic services decide -- the UI must never render a
    # proposal as though it were established fact.
    description_is_proposed: bool
    owner: str | None
    certification: str  # CERTIFIED | EXPIRED | NONE | REVOKED
    certification_expires_at: datetime | None
    # P3-09: small counts snapshot from the current cert's structured
    # evidence (description version, active owner count, open-incidents-at-
    # certify, glossary-term count). Null when the current cert is legacy
    # (evidence IS NULL) or when there is no current cert.
    certification_evidence_summary: "CertificationEvidenceSummary | None" = None
    quality: str  # PASSING | INCIDENT_OPEN | STALE | UNKNOWN
    glossary_terms: list[str]
    row_count_estimate: int | None
    updated_at: datetime


class EvidenceItemRead(ApiModel):
    """UX-13: one claim in an asset's evidence pane.

    Every fact composed onto `AssetEvidenceRead` -- an ownership assignment,
    an open incident, a consumption event, an AI decision -- is one of these,
    carrying its own `source` string (the module and record the claim was
    read from) so nothing in the pane is asserted without a traceable origin.
    """

    # BUSINESS_MEANING | OWNERSHIP | CERTIFICATION | DATA_QUALITY | CONSUMPTION | AI_DECISION
    category: str
    claim: str
    source: str
    occurred_at: datetime | None = None


class AssetEvidenceRead(ApiModel):
    """UX-13: `GET /v1/metadata/tables/{id}/evidence` -- composes business
    meaning, ownership/certification (GL-2/GL-5), data quality, consumption
    lineage (CX-4) and AI decision lineage including refusals (LN-3) for one
    table. See `aida.asset_evidence` for how each `items` entry is sourced.
    """

    table_id: UUID
    table_name: str
    generated_at: datetime
    items: list[EvidenceItemRead]


class AccessPolicyRead(ApiModel):
    id: UUID
    organization_id: UUID
    code: str
    version: int
    name: str
    description: str
    effect: str
    priority: int
    subject_match: dict[str, Any]
    resource_match: dict[str, Any]
    action_match: list[str]
    transform: dict[str, Any]
    condition: dict[str, Any]
    origin: str
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class AccessPolicyCreate(ApiModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=2000)
    effect: Literal["ALLOW", "DENY", "MASK", "FILTER"]
    priority: int = Field(default=100, ge=0, le=10000)
    subject_match: dict[str, Any] = Field(default_factory=dict)
    resource_match: dict[str, Any] = Field(default_factory=dict)
    action_match: list[str] = Field(default_factory=list, max_length=20)
    transform: dict[str, Any] = Field(default_factory=dict)
    condition: dict[str, Any] = Field(default_factory=dict)
    # A new policy starts DRAFT so that writing one can never silently change who
    # can reach what; activation is a separate, auditable step.
    status: Literal["DRAFT", "ACTIVE"] = "DRAFT"


class AuthorizationProbeRequest(ApiModel):
    """Ask the policy engine what it would decide, without performing the action.

    An access model nobody can interrogate is an access model nobody trusts. This
    is the "why can this principal see this?" endpoint, and it is deliberately
    read-only.
    """

    workspace_id: UUID
    action: Literal[
        "READ_METADATA",
        "READ_DATA",
        "PROPOSE",
        "APPROVE",
        "EXECUTE_TOOL",
        "CONSUME_CONTEXT",
        "EXPORT",
    ]
    resource_type: str = Field(min_length=1, max_length=40)
    resource_id: str | None = Field(default=None, max_length=120)
    datasource_id: UUID | None = None
    schema_name: str | None = Field(default=None, max_length=200)
    classifications: list[str] = Field(default_factory=list, max_length=20)
    certification: str | None = Field(default=None, max_length=40)
    quality_state: str | None = Field(default=None, max_length=40)
    freshness_state: str | None = Field(default=None, max_length=40)
    principal_kind: Literal["HUMAN", "AGENT", "SERVICE"] = "HUMAN"


class AuthorizationProbeRead(ApiModel):
    allowed: bool
    reason_code: str
    workspace_id: UUID | None
    binding_id: UUID | None
    matched_policy_code: str | None
    masked_classifications: list[str]
    row_filters: list[str]
    evaluated_policy_count: int


class SimulatedSubject(ApiModel):
    """One hypothetical "what if this principal asked" case (PG-8)."""

    principal_kind: Literal["HUMAN", "AGENT", "SERVICE"] = "HUMAN"
    roles: list[str] = Field(default_factory=list, max_length=20)
    purpose: str | None = Field(default=None, max_length=100)


class AuthorizationSimulationRequest(ApiModel):
    """"Who could see this?" -- one resource, several hypothetical subjects.

    Unlike `AuthorizationProbeRequest`, which answers for the calling
    principal's own real membership, this varies `subjects` directly against
    the ABAC policy layer (`aida.policy_engine.simulate`) -- the same engine
    `authorization-probes` and the query-execution path both use -- so an
    access review can ask "would a Steward see this? An Agent?" without those
    principals needing to exist yet.
    """

    workspace_id: UUID
    action: Literal[
        "READ_METADATA",
        "READ_DATA",
        "PROPOSE",
        "APPROVE",
        "EXECUTE_TOOL",
        "CONSUME_CONTEXT",
        "EXPORT",
    ]
    resource_type: str = Field(min_length=1, max_length=40)
    resource_id: str | None = Field(default=None, max_length=120)
    datasource_id: UUID | None = None
    schema_name: str | None = Field(default=None, max_length=200)
    classifications: list[str] = Field(default_factory=list, max_length=20)
    certification: str | None = Field(default=None, max_length=40)
    quality_state: str | None = Field(default=None, max_length=40)
    freshness_state: str | None = Field(default=None, max_length=40)
    subjects: list[SimulatedSubject] = Field(min_length=1, max_length=25)


class SimulatedDecision(ApiModel):
    principal_kind: Literal["HUMAN", "AGENT", "SERVICE"]
    roles: list[str]
    allowed: bool
    reason_code: str
    matched_policy_code: str | None
    masked_classifications: list[str]
    row_filters: list[str]


class AuthorizationSimulationRead(ApiModel):
    workspace_id: UUID
    decisions: list[SimulatedDecision]


# --- DQ-1: Notification and Escalation Routing --------------------------------


class NotificationRuleCreate(ApiModel):
    name: str = Field(min_length=3, max_length=200)
    conditions: dict[str, Any] = Field(default_factory=dict)
    channel: Literal["EMAIL", "WEBHOOK", "ITSM"]
    recipients: list[str] = Field(min_length=1, max_length=100)
    escalation_after_minutes: int | None = Field(default=None, ge=1, le=525_600)
    enabled: bool = True


class NotificationRuleUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=3, max_length=200)
    conditions: dict[str, Any] | None = None
    channel: Literal["EMAIL", "WEBHOOK", "ITSM"] | None = None
    recipients: list[str] | None = Field(default=None, min_length=1, max_length=100)
    escalation_after_minutes: int | None = Field(default=None, ge=1, le=525_600)
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "NotificationRuleUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class NotificationRuleRead(ApiModel):
    id: UUID
    organization_id: UUID
    name: str
    conditions: dict[str, Any]
    channel: str
    recipients: list[str]
    escalation_after_minutes: int | None
    enabled: bool
    created_by: str
    created_at: datetime
    updated_at: datetime


class NotificationEventRead(ApiModel):
    id: UUID
    organization_id: UUID
    incident_id: UUID
    rule_id: UUID
    channel: str
    recipients: list[str]
    status: str
    dedup_key: str
    sent_at: datetime | None
    escalated_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    created_at: datetime
    updated_at: datetime


# --- DQ-2: Freshness Watermark Contracts --------------------------------------


class FreshnessConfigUpsert(ApiModel):
    watermark_column: str = Field(min_length=1, max_length=255)
    classification: str = Field(default="INTERNAL", max_length=30)
    threshold_minutes: int = Field(ge=1, le=525_600)
    retention_days: int = Field(default=365, ge=1, le=3650)


class FreshnessConfigRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    table_id: UUID
    watermark_column: str
    classification: str
    threshold_minutes: int
    retention_days: int
    status: str
    approved_by: str | None
    approved_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class FreshnessStatusRead(ApiModel):
    table_id: UUID
    status: str  # FRESH, STALE, NOT_CONFIGURED, AWAITING_APPROVAL
    last_watermark: datetime | None
    age_minutes: float | None
    threshold_minutes: int | None
    evidence: dict[str, Any]


# --- DQ-3: Trust Scoring (EE.5) -----------------------------------------------


class TrustFactorRead(ApiModel):
    name: str
    score: int
    weight: float
    evidence: dict[str, Any]
    explanation: str


class TrustScoreRead(ApiModel):
    overall_score: int
    grade: str
    factors: list[TrustFactorRead]


# --- OB-6: cost / showback aggregation, per line of business -----------------


class LobCostRowRead(ApiModel):
    line_of_business_id: UUID
    line_of_business_code: str
    line_of_business_name: str
    datasource_count: int
    query_count: int
    completed_count: int
    rejected_count: int
    failed_count: int
    total_row_count: int
    total_elapsed_ms: int
    total_plan_cost_units: float | None


class CostShowbackTotalsRead(ApiModel):
    datasource_count: int
    query_count: int
    completed_count: int
    rejected_count: int
    failed_count: int
    total_row_count: int
    total_elapsed_ms: int
    total_plan_cost_units: float | None


class CostShowbackRead(ApiModel):
    organization_id: UUID
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    cost_basis: str
    rows: list[LobCostRowRead]
    totals: CostShowbackTotalsRead


# ---------------------------------------------------------------------------
# TL-1: tool certification corpus and workflow (module 14, tool registry).
# See aida.models for the ORM shape and aida.tool_certification for the
# deterministic corpus runner these schemas front.
# ---------------------------------------------------------------------------


class ToolCertificationExpectation(ApiModel):
    expect: Literal["ACCEPT", "REJECT"]
    sql_contains: list[str] = Field(default_factory=list, max_length=20)
    error_contains: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_shape(self) -> "ToolCertificationExpectation":
        if self.expect == "REJECT" and self.sql_contains:
            raise ValueError("sql_contains only applies when expect is ACCEPT")
        if self.expect == "ACCEPT" and self.error_contains:
            raise ValueError("error_contains only applies when expect is REJECT")
        return self


class ToolCertificationCaseCreate(ApiModel):
    case_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    description: str = Field(min_length=3, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)
    expectation: ToolCertificationExpectation


class ToolCertificationCaseRead(ApiModel):
    id: UUID
    organization_id: UUID
    tool_id: UUID
    case_key: str
    description: str
    parameters: dict[str, Any]
    expectation: dict[str, Any]
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class ToolCertificationRunCreate(ApiModel):
    rationale: str = Field(min_length=10, max_length=2000)
    expires_at: datetime


class ToolCertificationRunRead(ApiModel):
    id: UUID
    organization_id: UUID
    tool_id: UUID
    tool_version_id: UUID
    suite_version: str
    corpus_fingerprint: str
    status: str
    total_cases: int
    passed_cases: int
    score: int
    results: list[dict[str, Any]]
    rationale: str
    executed_by: str
    certified_by: str | None
    decision_reason: str | None
    issued_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ToolCertificationDecisionRequest(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_rejection_reason(self) -> "ToolCertificationDecisionRequest":
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a certification decision")
        return self


class ToolCertificationStatusRead(ApiModel):
    tool_id: UUID
    tool_version_id: UUID | None
    certified: bool
    run_id: UUID | None
    certified_by: str | None
    issued_at: datetime | None
    expires_at: datetime | None
    expired_run_id: UUID | None
    expired_at: datetime | None


# ---------------------------------------------------------------------------
# BI lineage (LN-4, module 09) — Tableau / Power BI / Looker report -> metric
# -> column edges. See aida.bi_lineage and aida.bi_api.
# ---------------------------------------------------------------------------


class BiConnectionCreate(ApiModel):
    datasource_id: UUID
    bi_tool: Literal["TABLEAU", "POWER_BI", "LOOKER"]
    connection_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    display_name: str = Field(min_length=2, max_length=200)
    site_or_workspace: str | None = Field(default=None, max_length=255)


class BiConnectionRead(ApiModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    datasource_id: UUID
    bi_tool: str
    connection_key: str
    display_name: str
    site_or_workspace: str | None
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class BiArtifactImportRequest(ApiModel):
    bi_tool: Literal["TABLEAU", "POWER_BI", "LOOKER"]
    artifact: dict[str, Any]


class BiArtifactImportRead(ApiModel):
    id: UUID
    organization_id: UUID
    connection_id: UUID
    artifact_fingerprint: str
    bi_tool: str
    generated_at: datetime | None
    status: str
    report_count: int
    metric_count: int
    report_metric_edge_count: int
    metric_column_edge_count: int
    matched_column_count: int
    unmatched_column_count: int
    imported_by: str
    created_at: datetime
    updated_at: datetime


class BiReportNodeRead(ApiModel):
    id: UUID
    artifact_import_id: UUID
    parent_report_id: UUID | None
    external_id: str
    name: str
    report_type: str
    project_name: str | None
    created_at: datetime
    updated_at: datetime


class BiMetricNodeRead(ApiModel):
    id: UUID
    artifact_import_id: UUID
    external_id: str
    name: str
    field_type: str
    datasource_name: str | None
    formula_hash: str | None
    formula_present: bool
    created_at: datetime
    updated_at: datetime


class BiReportMetricEdgeRead(ApiModel):
    id: UUID
    report_id: UUID
    metric_id: UUID
    edge_kind: str


class BiMetricColumnEdgeRead(ApiModel):
    id: UUID
    metric_id: UUID
    source_database_name: str | None
    source_schema_name: str | None
    source_table_name: str
    source_column_name: str
    matched_table_id: UUID | None
    matched_column_id: UUID | None
    edge_kind: str


class BiLineageRead(ApiModel):
    artifact_import_id: UUID
    reports: list[BiReportNodeRead]
    metrics: list[BiMetricNodeRead]
    report_metric_edges: list[BiReportMetricEdgeRead]
    metric_column_edges: list[BiMetricColumnEdgeRead]
    report_count: int
    metric_count: int
    matched_column_count: int
    unmatched_column_count: int


# ---------------------------------------------------------------------------
# CT-1: Catalog bulk actions (tag, classify, own, certify)
# ---------------------------------------------------------------------------


def _require_exactly_one_selection(*selections: object) -> None:
    provided = [value for value in selections if value]
    if len(provided) != 1:
        raise ValueError("provide exactly one selection: an explicit id list or a filter")


class CatalogBulkSelectionFilter(ApiModel):
    datasource_id: UUID
    match_field: Literal["TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME"] = "TABLE_NAME"
    match_pattern: str = Field(min_length=1, max_length=255)


class CatalogBulkTagRequest(ApiModel):
    table_ids: list[UUID] | None = Field(
        default=None, min_length=1, max_length=CATALOG_BULK_ACTION_MAX_ITEMS
    )
    filter: CatalogBulkSelectionFilter | None = None
    tag_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    tag_value: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_selection(self) -> "CatalogBulkTagRequest":
        _require_exactly_one_selection(self.table_ids, self.filter)
        return self


class CatalogBulkClassifyRequest(ApiModel):
    table_ids: list[UUID] | None = Field(
        default=None, min_length=1, max_length=CATALOG_BULK_ACTION_MAX_ITEMS
    )
    column_ids: list[UUID] | None = Field(
        default=None, min_length=1, max_length=CATALOG_BULK_ACTION_MAX_ITEMS
    )
    filter: CatalogBulkSelectionFilter | None = None
    column_name_pattern: str = Field(default="*", min_length=1, max_length=255)
    classification: Literal[
        "UNCLASSIFIED", "PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "PHI", "PCI", "SECRET"
    ]

    @model_validator(mode="after")
    def validate_selection(self) -> "CatalogBulkClassifyRequest":
        _require_exactly_one_selection(self.table_ids, self.column_ids, self.filter)
        if self.classification not in ALLOWED_CLASSIFICATIONS:
            raise ValueError("unsupported classification value")
        return self


class CatalogBulkOwnRequest(ApiModel):
    table_ids: list[UUID] | None = Field(
        default=None, min_length=1, max_length=CATALOG_BULK_ACTION_MAX_ITEMS
    )
    filter: CatalogBulkSelectionFilter | None = None
    owner_type: Literal["INDIVIDUAL", "GROUP"]
    owner_principal: str = Field(min_length=2, max_length=255)

    @model_validator(mode="after")
    def validate_selection(self) -> "CatalogBulkOwnRequest":
        _require_exactly_one_selection(self.table_ids, self.filter)
        return self


class CatalogBulkCertifyRequest(ApiModel):
    table_ids: list[UUID] | None = Field(
        default=None, min_length=1, max_length=CATALOG_BULK_ACTION_MAX_ITEMS
    )
    filter: CatalogBulkSelectionFilter | None = None
    rationale: str = Field(min_length=10, max_length=2000)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_selection(self) -> "CatalogBulkCertifyRequest":
        _require_exactly_one_selection(self.table_ids, self.filter)
        return self


class CatalogBulkActionItemRead(ApiModel):
    subject_id: str
    status: Literal["SUCCEEDED", "FAILED"]
    reason: str | None = None


class CatalogBulkActionRunRead(ApiModel):
    id: UUID
    organization_id: UUID
    action: str
    selection_mode: str
    parameters: dict[str, Any]
    requested_count: int
    succeeded_count: int
    failed_count: int
    results: list[CatalogBulkActionItemRead]
    requested_by: str
    created_at: datetime


# ---------------------------------------------------------------------------
# SM-2: Glossary term binding to semantic objects
# ---------------------------------------------------------------------------


class TermSemanticBindingCreate(ApiModel):
    term_id: UUID
    semantic_object_type: Literal["METRIC"] = "METRIC"
    semantic_object_id: UUID


class TermSemanticBindingRead(ApiModel):
    id: UUID
    organization_id: UUID
    term_id: UUID
    term_key: str
    term_display_name: str
    term_definition: str
    semantic_object_type: str
    semantic_object_id: UUID
    semantic_object_name: str
    status: str
    requested_by: str
    approved_by: str | None
    approved_at: datetime | None
    governance_review_id: UUID | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# SM-4: Metric suggestions from approved annotations
# ---------------------------------------------------------------------------


class MetricSuggestionProposalGenerate(ApiModel):
    limit: int = Field(default=100, ge=1, le=500)


class MetricSuggestionProposalRead(ApiModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    table_id: UUID
    table_name: str
    measure_column_id: UUID
    measure_column_name: str
    source_annotation_id: UUID
    proposed_slug: str
    proposed_name: str
    proposed_description: str
    proposed_aggregation: str
    proposed_grain: str
    accuracy_score: float
    clarity_score: float
    style_score: float
    completeness_score: float
    overall_score: float
    evidence: dict[str, Any]
    status: str
    governance_review_id: UUID | None
    published_metric_version_id: UUID | None
    created_by: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# CT-5: asset certification lifecycle with expiry (single table or column)
# ---------------------------------------------------------------------------


class CertificationDecisionRequest(ApiModel):
    """Module 04's ``CertificationDecision``: certify the table itself, or one column."""

    asset_type: Literal["TABLE", "COLUMN"] = "TABLE"
    column_id: UUID | None = None
    rationale: str = Field(min_length=10, max_length=2000)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_target(self) -> "CertificationDecisionRequest":
        if self.asset_type == "COLUMN" and self.column_id is None:
            raise ValueError("certifying a column requires column_id")
        if self.asset_type == "TABLE" and self.column_id is not None:
            raise ValueError("column_id is only meaningful when asset_type is COLUMN")
        return self


class CertificationRevokeRequest(ApiModel):
    """P2-08: request body for
    ``POST /v1/tables/{table_id}/certification/revoke``.

    ``column_id`` mirrors ``CertificationDecisionRequest``: absent when
    revoking the table-level cert, present when revoking a specific column's.
    ``reason`` is required and durable -- it is stored as
    ``AssetCertification.revocation_reason`` and echoed in the outbox event
    that downstream policy decisions consume.
    """

    reason: str = Field(min_length=10, max_length=2000)
    column_id: UUID | None = None


class CertificationEvidence(ApiModel):
    """P3-09: structured, machine-consumable snapshot of what a certifier
    was implicitly attesting to at certify time -- populated on every new
    write alongside the free-text ``rationale`` field, empty (``None``) on
    every pre-P3-09 row (the migration adds the column nullable and never
    backfills historical rows in-place). Shape composed by
    ``aida.certification_evidence.compute_certification_evidence``; a
    future ``revoke-on-evidence-change`` job keys off these fields.
    """

    schema_version: str = "1"
    captured_at: datetime | None = None
    description_version_id: UUID | None = None
    ownership_assignment_ids: list[UUID] = Field(default_factory=list)
    quality_snapshot: dict[str, Any] = Field(default_factory=dict)
    glossary_term_ids: list[UUID] = Field(default_factory=list)
    supporting_dq_check_ids: list[UUID] = Field(default_factory=list)
    certifier_notes: str | None = None
    # True on rows populated by `backfill_certification_evidence_v1`
    # (best-effort snapshot from *now's* state, not as-of-certify).
    backfilled: bool = False
    backfilled_at: datetime | None = None


class CertificationEvidenceSummary(ApiModel):
    """P3-09: the small, catalog-UI-facing projection of
    ``CertificationEvidence`` -- fold of counts the hover tooltip renders on
    the certification cell of the catalog grid. Null on ``CatalogRowRead``
    when the current cert has no structured evidence (legacy row).
    """

    description_version_id: UUID | None = None
    active_owner_count: int = 0
    open_incident_count_at_certify: int = 0
    glossary_term_count: int = 0
    backfilled: bool = False


class AssetCertificationRead(ApiModel):
    id: UUID
    organization_id: UUID
    table_id: UUID
    column_id: UUID | None
    asset_type: str
    status: str
    rationale: str
    certified_by: str
    expires_at: datetime
    is_active: bool
    # P2-08: revoke details, populated only on rows the manual revoke endpoint
    # wrote (`status == "REVOKED"`); nullable everywhere else.
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None
    # P3-09: structured evidence captured at certify time. Null on every
    # pre-P3-09 row (the column is nullable and no historical row is
    # mutated by the schema migration).
    evidence: CertificationEvidence | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# --- GROUP A: retrieval (RT-5 / RT-9 global-search endpoint) ---
# ---------------------------------------------------------------------------


class GlobalSearchHitRead(ApiModel):
    """One ranked hit from ``GET /v1/organizations/{organization_id}/global-search``
    (RT-5's API half, RT-9's genuine cross-source retrieval).

    ``evidence`` is the *real* per-signal fusion breakdown
    (``aida.retrieval.hybrid_retrieve_enhanced``'s output for the datasource
    this hit came from) -- lexical/vector/graph/quality_trust/usage_popularity,
    whichever signals actually fired -- not a synthesized summary. Every
    ranking factor stays inspectable (RT-3).
    """

    object_type: str
    object_id: str
    display_name: str
    score: float
    datasource_id: UUID
    reason_codes: list[str]
    evidence: dict[str, Any]
    metadata: dict[str, Any]


class GlobalSearchResponse(ApiModel):
    """Response for ``GET /v1/organizations/{organization_id}/global-search``."""

    items: list[GlobalSearchHitRead]
    total: int
    datasource_count: int
    limit: int
    fusion_method: str
    vector_enabled: bool
    graph_enabled: bool


# ---------------------------------------------------------------------------
# --- GROUP H: ST-A7 context product builder ---
# ---------------------------------------------------------------------------


class StudioContextProductValidateRequest(ApiModel):
    """Stateless shape-validation request for a CONTEXT_PRODUCT change-set
    item, mirroring `StudioParameterContractValidateRequest` (ST-A4). Lets an
    author validate a draft context product definition incrementally, without
    an existing change set or change item -- the exact same check the
    CONTEXT_PRODUCT item's own test gate runs (`_validate_context_product_item`
    -> `validate_context_product_contract`, `studio.py`)."""

    operation: Literal["CREATE", "UPDATE", "DELETE"]
    object_id: str = Field(min_length=1, max_length=100)
    snapshot: dict[str, Any] | None = None


class StudioContextProductValidateResult(ApiModel):
    valid: bool
    errors: list[str]
    definition: dict[str, Any] | None = None
    product_key: str | None = None
    project_id: str | None = None


class StudioContextProductMaterializationRead(ApiModel):
    """One `StudioContextProductMaterialization` row -- the durable link from
    a CONTEXT_PRODUCT change-set item to the real `ContextProduct`/
    `ContextProductVersion`/`GovernanceReview` it produced on submission."""

    id: UUID
    organization_id: UUID
    change_set_id: UUID
    change_item_id: UUID
    operation: str
    context_product_id: UUID
    context_product_version_id: UUID
    governance_review_id: UUID
    created_by: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# P1-05 / ADR-0026: parsed lineage-edge review schemas.
#
# The five non-governed parser-produced lineage edge tables all share the
# same review lifecycle (PROPOSED → ACTIVE | REJECTED | SUPERSEDED) but keep
# their own storage. These DTOs are the common wire vocabulary the review
# endpoint uses -- decide/bulk-decide/queue all speak in ParsedLineageEdge*
# regardless of which underlying table the edge lives in.
# ---------------------------------------------------------------------------


PARSED_LINEAGE_EDGE_TYPES = (
    "VIEW",
    "PROCEDURE",
    "DBT",
    "OPENLINEAGE_TABLE",
    "OPENLINEAGE_COLUMN",
)
PARSED_LINEAGE_BULK_DECISION_MAX_ITEMS = 100


class ParsedLineageEdgeReviewQueueItemRead(ApiModel):
    """One PROPOSED parsed-lineage edge as it appears in the review queue.

    Deliberately narrow: enough for the reviewer to judge the edge and
    dereference the source SQL, without loading the raw SQL text into
    the queue payload itself (source_sql_reference names the tool/id
    the reviewer's UI dereferences on demand)."""

    edge_id: UUID
    edge_type: Literal[
        "VIEW", "PROCEDURE", "DBT", "OPENLINEAGE_TABLE", "OPENLINEAGE_COLUMN"
    ]
    organization_id: UUID
    created_at: datetime
    created_by: str | None
    confidence: str | float | None
    source_label: str
    target_label: str
    transformation_type: str | None
    source_sql_reference: dict[str, str]


class ParsedLineageEdgeReviewQueueRead(ApiModel):
    items: list[ParsedLineageEdgeReviewQueueItemRead]
    limit: int
    offset: int
    total: int


class ParsedLineageEdgeDecisionRequest(ApiModel):
    """Decision on one PROPOSED parsed-lineage edge."""

    edge_type: Literal[
        "VIEW", "PROCEDURE", "DBT", "OPENLINEAGE_TABLE", "OPENLINEAGE_COLUMN"
    ]
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=1, max_length=2000)


class ParsedLineageEdgeDecisionRead(ApiModel):
    edge_id: UUID
    edge_type: str
    review_status: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_reason: str | None


class ParsedLineageEdgeBulkDecisionItem(ApiModel):
    edge_id: UUID
    edge_type: Literal[
        "VIEW", "PROCEDURE", "DBT", "OPENLINEAGE_TABLE", "OPENLINEAGE_COLUMN"
    ]


class ParsedLineageEdgeBulkDecisionRequest(ApiModel):
    items: list[ParsedLineageEdgeBulkDecisionItem] = Field(
        min_length=1, max_length=PARSED_LINEAGE_BULK_DECISION_MAX_ITEMS
    )
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=1, max_length=2000)


class ParsedLineageEdgeBulkDecisionItemRead(ApiModel):
    edge_id: UUID
    edge_type: str
    status: Literal["SUCCEEDED", "FAILED"]
    reason: str | None = None


class ParsedLineageEdgeBulkDecisionResultRead(ApiModel):
    decision: Literal["APPROVED", "REJECTED"]
    requested_count: int
    succeeded_count: int
    failed_count: int
    results: list[ParsedLineageEdgeBulkDecisionItemRead]
