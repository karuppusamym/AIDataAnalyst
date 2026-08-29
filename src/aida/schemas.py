from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aida.integration_catalog import normalized_transformation_metadata_integrations


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class OrganizationCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")


class OrganizationRead(OrganizationCreate):
    id: UUID
    status: str
    created_at: datetime
    updated_at: datetime


class OrganizationIntegrationPolicyWrite(ApiModel):
    transformation_metadata_integrations: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_integrations(self) -> "OrganizationIntegrationPolicyWrite":
        self.transformation_metadata_integrations = normalized_transformation_metadata_integrations(
            self.transformation_metadata_integrations
        )
        return self


class OrganizationIntegrationPolicyRead(ApiModel):
    id: UUID
    organization_id: UUID
    transformation_metadata_integrations: dict[str, bool]
    created_at: datetime
    updated_at: datetime


class LineOfBusinessCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,49}$")


class LineOfBusinessRead(LineOfBusinessCreate):
    id: UUID
    organization_id: UUID
    status: str
    created_at: datetime
    updated_at: datetime


class ProjectCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")


class ProjectRead(ProjectCreate):
    id: UUID
    organization_id: UUID
    line_of_business_id: UUID
    status: str
    created_at: datetime
    updated_at: datetime


class DataSourceCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    connector_type: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,49}$")
    dialect: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,49}$")
    environment: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{1,29}$")
    network_zone: str = Field(default="default", min_length=1, max_length=100)
    credential_reference: str = Field(min_length=6, max_length=500)
    max_concurrency: int = Field(default=4, ge=1, le=100)


class DataSourceRead(DataSourceCreate):
    id: UUID
    organization_id: UUID
    line_of_business_id: UUID
    project_id: UUID
    status: str
    capabilities: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DataSourceSummaryRead(ApiModel):
    id: UUID
    organization_id: UUID
    line_of_business_id: UUID
    project_id: UUID
    name: str
    connector_type: str
    dialect: str
    environment: str
    network_zone: str
    status: str
    max_concurrency: int
    capabilities: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DataSourceUpdate(ApiModel):
    enabled: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=100)
    network_zone: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_change(self) -> "DataSourceUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one datasource field must be provided")
        return self


class ConnectorCapabilityRead(ApiModel):
    connector_type: str
    display_name: str
    dialect: str
    implementation_status: str
    transports: list[str]
    maturity: str
    version: str
    notes: str
    capabilities: dict[str, bool]


class ConnectorCertificationRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    connector_type: str
    connector_version: str
    suite_version: str
    status: str
    score: int
    checks: list[dict[str, Any]]
    initiated_by: str
    completed_at: datetime
    created_at: datetime
    updated_at: datetime


MetadataAttribute = str | int | float | bool | None


class MetadataColumnEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    ordinal_position: int = Field(ge=1, le=100_000)
    physical_type: str = Field(min_length=1, max_length=255)
    nullable: bool
    default_expression: str | None = Field(default=None, max_length=4000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)


class MetadataConstraintEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    constraint_type: Literal["PRIMARY_KEY", "UNIQUE", "FOREIGN_KEY"]
    columns: list[str] = Field(min_length=1, max_length=1000)
    referenced_schema: str | None = Field(default=None, max_length=255)
    referenced_table: str | None = Field(default=None, max_length=255)
    referenced_columns: list[str] = Field(default_factory=list, max_length=1000)


class MetadataTableEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    object_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,29}$")
    source_description: str | None = Field(default=None, max_length=10_000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    columns: list[MetadataColumnEnvelope] = Field(max_length=10_000)
    constraints: list[MetadataConstraintEnvelope] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_table_members(self) -> "MetadataTableEnvelope":
        column_names = [column.name for column in self.columns]
        if len(column_names) != len(set(column_names)):
            raise ValueError("column names must be unique within a table")
        ordinals = [column.ordinal_position for column in self.columns]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("column ordinals must be unique within a table")
        available = set(column_names)
        for constraint in self.constraints:
            if not set(constraint.columns).issubset(available):
                raise ValueError(f"constraint {constraint.name} refers to an unknown local column")
            has_reference = bool(constraint.referenced_schema and constraint.referenced_table)
            if constraint.constraint_type == "FOREIGN_KEY" and not has_reference:
                raise ValueError("foreign keys require referenced_schema and referenced_table")
            if constraint.constraint_type == "FOREIGN_KEY" and (
                len(constraint.columns) != len(constraint.referenced_columns)
            ):
                raise ValueError("foreign-key local and referenced column counts must match")
        return self


class MetadataSchemaEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    tables: list[MetadataTableEnvelope] = Field(max_length=10_000)


class MetadataCatalogEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    schemas: list[MetadataSchemaEnvelope] = Field(max_length=5000)


class MetadataIngestionCreate(ApiModel):
    envelope_version: Literal["1.0"] = "1.0"
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    producer: str = Field(min_length=2, max_length=200)
    transport: Literal["PUSH", "STREAM"] = "PUSH"
    snapshot_type: Literal["FULL", "INCREMENTAL"] = "FULL"
    emitted_at: datetime
    catalogs: list[MetadataCatalogEnvelope] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_envelope(self) -> "MetadataIngestionCreate":
        forbidden_fragments = ("sample", "row_value", "password", "secret", "token", "credential")
        total_tables = 0
        total_columns = 0
        for catalog in self.catalogs:
            if len({schema.name for schema in catalog.schemas}) != len(catalog.schemas):
                raise ValueError("schema names must be unique within a catalog")
            self._validate_attributes(catalog.attributes, forbidden_fragments)
            for schema in catalog.schemas:
                if len({table.name for table in schema.tables}) != len(schema.tables):
                    raise ValueError("table names must be unique within a schema")
                self._validate_attributes(schema.attributes, forbidden_fragments)
                total_tables += len(schema.tables)
                for table in schema.tables:
                    self._validate_attributes(table.attributes, forbidden_fragments)
                    for column in table.columns:
                        self._validate_attributes(column.attributes, forbidden_fragments)
                    total_columns += len(table.columns)
        if total_tables > 50_000 or total_columns > 250_000:
            raise ValueError("envelope exceeds the synchronous ingestion safety boundary")
        return self

    @staticmethod
    def _validate_attributes(
        attributes: dict[str, MetadataAttribute], forbidden_fragments: tuple[str, ...]
    ) -> None:
        if len(attributes) > 50:
            raise ValueError("metadata attributes are limited to 50 entries per object")
        for key, value in attributes.items():
            normalized = key.lower()
            if any(fragment in normalized for fragment in forbidden_fragments):
                raise ValueError(
                    f"attribute key is not permitted by the value-free contract: {key}"
                )
            if len(key) > 100 or (isinstance(value, str) and len(value) > 2000):
                raise ValueError("metadata attribute key or value exceeds its size boundary")


class MetadataIngestionRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    analysis_run_id: UUID | None
    idempotency_key: str
    envelope_version: str
    producer: str
    transport: str
    snapshot_type: str
    payload_fingerprint: str
    status: str
    object_counts: dict[str, Any]
    change_counts: dict[str, Any]
    submitted_by: str
    error_class: str | None
    error_message: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MetadataIngestionBatchCreate(ApiModel):
    envelope_version: Literal["1.0"] = "1.0"
    batch_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    producer: str = Field(min_length=2, max_length=200)
    snapshot_type: Literal["FULL", "INCREMENTAL"] = "INCREMENTAL"
    expected_chunks: int = Field(ge=1, le=1000)


class MetadataIngestionChunkCreate(ApiModel):
    chunk_number: int = Field(ge=1, le=1000)
    chunk_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    emitted_at: datetime
    catalogs: list[MetadataCatalogEnvelope] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_chunk_contract(self) -> "MetadataIngestionChunkCreate":
        if self.emitted_at.tzinfo is None:
            raise ValueError("emitted_at must include a timezone")
        MetadataIngestionCreate(
            idempotency_key=self.chunk_key,
            producer="batch-chunk-validator",
            transport="PUSH",
            snapshot_type="INCREMENTAL",
            emitted_at=self.emitted_at,
            catalogs=self.catalogs,
        )
        return self


class MetadataIngestionBatchRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    analysis_run_id: UUID | None
    batch_key: str
    envelope_version: str
    producer: str
    snapshot_type: str
    expected_chunks: int
    received_chunks: int
    processed_chunks: int
    status: str
    temporal_workflow_id: str | None
    object_counts: dict[str, Any]
    change_counts: dict[str, Any]
    submitted_by: str
    finalized_at: datetime | None
    completed_at: datetime | None
    error_class: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class MetadataIngestionChunkRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    batch_id: UUID
    chunk_number: int
    chunk_key: str
    emitted_at: datetime
    payload_fingerprint: str
    object_counts: dict[str, Any]
    change_counts: dict[str, Any]
    status: str
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


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
    maintenance_start_hour_utc: int | None
    maintenance_end_hour_utc: int | None
    next_run_at: datetime
    last_triggered_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class AuditEventRead(ApiModel):
    id: int
    organization_id: UUID | None
    principal_id: str
    principal_type: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    correlation_id: str
    source_ip: str | None
    details: dict[str, Any]
    occurred_at: datetime


class FleetSummaryRead(ApiModel):
    organization_id: UUID
    datasource_statuses: dict[str, int]
    analysis_run_statuses: dict[str, int]
    scan_policies_enabled: int
    scan_policies_due: int
    pending_outbox_events: int
    dead_letter_outbox_events: int
    generated_at: datetime


class OutboxEventRead(ApiModel):
    id: UUID
    organization_id: UUID | None
    aggregate_type: str
    aggregate_id: str
    event_type: str
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    occurred_at: datetime
    published_at: datetime | None


class MetadataColumnRead(ApiModel):
    id: UUID
    name: str
    ordinal_position: int
    physical_type: str
    nullable: bool
    classification: str
    status: str


class MetadataConstraintRead(ApiModel):
    id: UUID
    table_id: UUID
    name: str
    constraint_type: str
    columns: list[str]
    referenced_table_id: UUID | None
    referenced_columns: list[str]
    status: str


class MetadataTableRead(ApiModel):
    id: UUID
    datasource_id: UUID
    schema_id: UUID
    name: str
    object_type: str
    status: str
    fingerprint: str


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
    plan_evidence: dict[str, Any]
    recommended_tool_version_id: UUID | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


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


class RelationshipCandidateRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
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
    "FOREIGN_KEY", "SUGGESTED_RELATIONSHIP", "DBT_DEPENDENCY", "OPENLINEAGE_ETL"
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
    relationships, dbt manifest dependencies, or OpenLineage table edges."""

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


class UnifiedLineageImpactNodeRead(ApiModel):
    node_id: str
    node_kind: UnifiedLineageNodeKind
    label: str
    qualified_name: str
    depth: int
    contributing_edge_sources: list[UnifiedLineageEdgeSource]


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
    created_at: datetime
    updated_at: datetime


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


class ContextProductQualityRequirements(ApiModel):
    minimum_score: int = Field(default=0, ge=0, le=100)
    deny_on_critical_incident: bool = True


def _default_context_product_actions() -> list[
    Literal["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"]
]:
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
    policy_summary: ContextProductPolicySummary = Field(
        default_factory=ContextProductPolicySummary
    )

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
