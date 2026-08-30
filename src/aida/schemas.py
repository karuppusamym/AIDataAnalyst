import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aida.catalog_bulk_actions import ALLOWED_CLASSIFICATIONS, CATALOG_BULK_ACTION_MAX_ITEMS
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


class DataDomainCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,49}$")
    parent_domain_id: UUID | None = None


class DataDomainRead(ApiModel):
    id: UUID
    organization_id: UUID
    line_of_business_id: UUID
    parent_domain_id: UUID | None
    name: str
    code: str
    is_default: bool
    status: str
    created_at: datetime
    updated_at: datetime


class CrossBoundaryGrantCreate(ApiModel):
    target_data_domain_id: UUID
    edge_kinds: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=3, max_length=500)
    expires_at: datetime | None = None


class CrossBoundaryGrantRead(ApiModel):
    id: UUID
    organization_id: UUID
    source_data_domain_id: UUID
    target_data_domain_id: UUID
    edge_kinds: list[str]
    reason: str
    status: str
    requested_by: str
    approved_by: str | None
    approved_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProjectCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    data_domain_id: UUID | None = Field(
        default=None,
        description=(
            "Governance domain this project belongs to. Omit to fall back to the line of "
            "business's default (Ungoverned) domain — a project is never blocked on a "
            "taxonomy existing yet; see ADR-0017."
        ),
    )


class ProjectRead(ProjectCreate):
    id: UUID
    organization_id: UUID
    line_of_business_id: UUID
    data_domain_id: UUID
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
    data_domain_id: UUID
    project_id: UUID
    status: str
    capabilities: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DataSourceSummaryRead(ApiModel):
    id: UUID
    organization_id: UUID
    line_of_business_id: UUID
    data_domain_id: UUID
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
    source_description: str | None = Field(default=None, max_length=10_000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)


class MetadataConstraintEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    constraint_type: Literal["PRIMARY_KEY", "UNIQUE", "FOREIGN_KEY"]
    columns: list[str] = Field(min_length=1, max_length=1000)
    referenced_schema: str | None = Field(default=None, max_length=255)
    referenced_table: str | None = Field(default=None, max_length=255)
    referenced_columns: list[str] = Field(default_factory=list, max_length=1000)


# --- envelope 1.1 (gap/02 N1) -----------------------------------------------
#
# 1.1 is additive: every field below is optional, so a 1.0 payload validates
# unchanged and a 1.0 producer keeps working forever. What 1.1 buys is that the
# platform can tell "the producer sent no view definitions" apart from "the
# producer sent them and we dropped them" -- `ingestion.validate_envelope_version`
# rejects the second case rather than answering 201 to it.


class MetadataViewDefinitionEnvelope(ApiModel):
    """The text a view is defined by, and how much of it the source would give.

    `definition_sql is None` is a first-class state meaning *unavailable*, not
    *empty*, and it must be explained: the model refuses a null definition with
    no reason, and refuses a reason alongside a definition. That is deliberately
    stricter than a nullable string, because an unexplained NULL here becomes a
    permanently unexplainable gap in lineage coverage (gap/02 N2).
    """

    definition_sql: str | None = Field(default=None, max_length=1_000_000)
    is_materialized: bool = False
    is_updatable: bool | None = None
    check_option: str | None = Field(default=None, max_length=30)
    truncated: bool = False
    unavailable_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_availability(self) -> "MetadataViewDefinitionEnvelope":
        if self.definition_sql is None and not self.unavailable_reason:
            raise ValueError(
                "a view definition without definition_sql must carry an "
                "unavailable_reason; an unexplained null is indistinguishable "
                "from an empty definition"
            )
        if self.definition_sql is not None and self.unavailable_reason:
            raise ValueError("unavailable_reason is only meaningful when definition_sql is null")
        if self.definition_sql is None and self.truncated:
            raise ValueError("a definition that was never returned cannot be truncated")
        return self


class MetadataRoutineParameterEnvelope(ApiModel):
    name: str | None = Field(default=None, max_length=255)
    ordinal_position: int = Field(ge=1, le=10_000)
    mode: Literal["IN", "OUT", "INOUT", "VARIADIC", "TABLE"] = "IN"
    physical_type: str = Field(min_length=1, max_length=255)
    default_expression: str | None = Field(default=None, max_length=4000)


class MetadataRoutineEnvelope(ApiModel):
    """A stored procedure or function, with its body when the source exposes it.

    Same availability rule as a view definition, for the same reason: procedure
    parsing and procedure-to-tool generation (gap/02 N3, N12) must never mistake
    "not allowed to read it" for "there is nothing to read".
    """

    name: str = Field(min_length=1, max_length=255)
    routine_type: Literal["FUNCTION", "PROCEDURE"]
    language: str | None = Field(default=None, max_length=50)
    body_sql: str | None = Field(default=None, max_length=1_000_000)
    parameters: list[MetadataRoutineParameterEnvelope] = Field(
        default_factory=list, max_length=1000
    )
    return_type: str | None = Field(default=None, max_length=255)
    is_deterministic: bool | None = None
    security_mode: Literal["DEFINER", "INVOKER"] | None = None
    source_description: str | None = Field(default=None, max_length=10_000)
    truncated: bool = False
    unavailable_reason: str | None = Field(default=None, max_length=500)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routine(self) -> "MetadataRoutineEnvelope":
        ordinals = [parameter.ordinal_position for parameter in self.parameters]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("routine parameter ordinals must be unique within a routine")
        if self.body_sql is None and not self.unavailable_reason:
            raise ValueError(
                "a routine without body_sql must carry an unavailable_reason; an "
                "unexplained null is indistinguishable from an empty body"
            )
        if self.body_sql is not None and self.unavailable_reason:
            raise ValueError("unavailable_reason is only meaningful when body_sql is null")
        if self.body_sql is None and self.truncated:
            raise ValueError("a body that was never returned cannot be truncated")
        return self


class MetadataGrantEnvelope(ApiModel):
    """One privilege held by one grantee on one source object.

    Evidence about the estate, never authority in this platform: nothing here
    grants anything and the policy engine does not read it.
    """

    grantee: str = Field(min_length=1, max_length=255)
    grantee_type: Literal["USER", "ROLE", "GROUP", "PUBLIC"] = "ROLE"
    privilege: str = Field(pattern=r"^[A-Z][A-Z0-9_ ]{0,49}$")
    object_type: Literal["TABLE", "VIEW", "PROCEDURE", "FUNCTION", "SCHEMA", "SEQUENCE"] = "TABLE"
    object_name: str = Field(min_length=1, max_length=255)
    schema_name: str | None = Field(default=None, max_length=255)
    is_grantable: bool = False


class MetadataTableEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    object_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,29}$")
    source_description: str | None = Field(default=None, max_length=10_000)
    view_definition: MetadataViewDefinitionEnvelope | None = None
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
    source_description: str | None = Field(default=None, max_length=10_000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    tables: list[MetadataTableEnvelope] = Field(max_length=10_000)
    routines: list[MetadataRoutineEnvelope] = Field(default_factory=list, max_length=10_000)
    grants: list[MetadataGrantEnvelope] = Field(default_factory=list, max_length=100_000)


class MetadataCatalogEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    source_description: str | None = Field(default=None, max_length=10_000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    schemas: list[MetadataSchemaEnvelope] = Field(max_length=5000)


class MetadataIngestionCreate(ApiModel):
    # 1.1 is the current version; 1.0 stays accepted forever (contract §2.1) and
    # remains the *default*, so a producer that never sent the field keeps the
    # behaviour it has today. Opting in to 1.1 is explicit, because 1.1 also
    # opts a FULL snapshot in to reconciling the new axes -- and a producer that
    # was silently promoted would retire the estate's view definitions on its
    # next full scan. Declaring 1.0 while sending 1.1 content is rejected by
    # `ingestion.validate_envelope_version`, not silently stripped.
    envelope_version: Literal["1.0", "1.1"] = "1.0"
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
        total_routines = 0
        for catalog in self.catalogs:
            if len({schema.name for schema in catalog.schemas}) != len(catalog.schemas):
                raise ValueError("schema names must be unique within a catalog")
            self._validate_attributes(catalog.attributes, forbidden_fragments)
            for schema in catalog.schemas:
                if len({table.name for table in schema.tables}) != len(schema.tables):
                    raise ValueError("table names must be unique within a schema")
                self._validate_attributes(schema.attributes, forbidden_fragments)
                total_tables += len(schema.tables)
                # Envelope 1.1: a routine carries its own attribute bag, so it is
                # screened like every other object. An unscreened bag would be a
                # hole in INV-6 the moment 1.1 producers appear.
                total_routines += len(schema.routines)
                for routine in schema.routines:
                    self._validate_attributes(routine.attributes, forbidden_fragments)
                for table in schema.tables:
                    self._validate_attributes(table.attributes, forbidden_fragments)
                    for column in table.columns:
                        self._validate_attributes(column.attributes, forbidden_fragments)
                    total_columns += len(table.columns)
        if total_tables > 50_000 or total_columns > 250_000 or total_routines > 50_000:
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
    envelope_version: Literal["1.0", "1.1"] = "1.0"
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
    classification_source: str
    status: str


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


class MetadataConstraintRead(ApiModel):
    id: UUID
    table_id: UUID
    name: str
    constraint_type: str
    columns: list[str]
    referenced_table_id: UUID | None
    referenced_columns: list[str]
    status: str


class MetadataIndexRead(ApiModel):
    id: UUID
    table_id: UUID
    name: str
    index_type: str
    columns: list[str]
    is_unique: bool
    is_primary: bool
    status: str


class MetadataPartitionRead(ApiModel):
    id: UUID
    table_id: UUID
    name: str
    partition_type: str
    ordinal_position: int
    key_columns: list[str]
    high_value: str | None
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


class DomainLineageGraphRead(ApiModel):
    """Same merged FK + suggested + dbt + OpenLineage graph as
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


class StudioTestResultRead(ApiModel):
    id: UUID
    change_set_id: UUID
    started_at: datetime
    completed_at: datetime | None
    passed: bool
    evidence: dict[str, Any]
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


# --- ADR-0018: three-axis tenancy -------------------------------------------


class WorkspaceCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    purpose: str = Field(default="", max_length=1000)
    isolation_boundary_id: UUID | None = None
    monthly_cost_ceiling: int | None = Field(default=None, ge=0)


class WorkspaceRead(ApiModel):
    id: UUID
    organization_id: UUID
    isolation_boundary_id: UUID | None
    name: str
    slug: str
    purpose: str
    status: str
    monthly_cost_ceiling: int | None
    created_at: datetime
    updated_at: datetime


class WorkspaceMembershipCreate(ApiModel):
    principal_id: str = Field(min_length=1, max_length=255)
    principal_kind: Literal["HUMAN", "AGENT", "SERVICE"] = "HUMAN"
    role: Literal["viewer", "analyst", "steward", "reviewer", "workspace_owner"]
    expires_at: datetime | None = None


class WorkspaceMembershipRead(ApiModel):
    id: UUID
    organization_id: UUID
    workspace_id: UUID
    principal_id: str
    principal_kind: str
    role: str
    granted_by: str
    expires_at: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime


class SourceBindingCreate(ApiModel):
    datasource_id: UUID
    purpose: str = Field(min_length=3, max_length=500)
    schema_scope: list[str] = Field(default_factory=list, max_length=200)
    permitted_classifications: list[str] = Field(default_factory=list, max_length=50)
    masking_profile: str = Field(default="DEFAULT", max_length=50)
    max_query_cost: int | None = Field(default=None, ge=0)


class SourceBindingRead(ApiModel):
    id: UUID
    organization_id: UUID
    workspace_id: UUID
    datasource_id: UUID
    schema_scope: list[str]
    permitted_classifications: list[str]
    masking_profile: str
    purpose: str
    max_query_cost: int | None
    status: str
    requested_by: str
    approved_by: str | None
    approved_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceBindingDecision(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    valid_for_days: int = Field(default=365, ge=1, le=1095)
    rationale: str = Field(default="", max_length=1000)


class BusinessNodeCreate(ApiModel):
    kind: Literal["LOB", "SUB_LOB", "DOMAIN", "SUB_DOMAIN", "CONCEPT"]
    name: str = Field(min_length=2, max_length=200)
    code: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_:-]{1,79}$")
    parent_id: UUID | None = None
    description: str = Field(default="", max_length=2000)
    owner_principal: str | None = Field(default=None, max_length=255)


class BusinessNodeRead(ApiModel):
    id: UUID
    organization_id: UUID
    parent_id: UUID | None
    kind: str
    name: str
    code: str
    description: str
    owner_principal: str | None
    origin: str
    effective_from: datetime
    effective_to: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime


class BusinessAssignmentCreate(ApiModel):
    business_node_id: UUID
    target_type: Literal[
        "PROJECT",
        "WORKSPACE",
        "DATASOURCE",
        "TABLE",
        "COLUMN",
        "VIEW",
        "METRIC",
        "GLOSSARY_TERM",
        "DATA_PRODUCT",
        "KNOWLEDGE_PAGE",
    ]
    target_id: str = Field(min_length=1, max_length=120)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class BusinessAssignmentRead(ApiModel):
    id: UUID
    organization_id: UUID
    business_node_id: UUID
    target_type: str
    target_id: str
    assignment_kind: str
    confidence: float | None
    assigned_by: str
    confirmed_by: str | None
    effective_from: datetime
    effective_to: datetime | None
    status: str


class BusinessNodeRollupRead(ApiModel):
    business_node_id: UUID
    descendant_node_count: int
    assigned_by_target_type: dict[str, int]
    as_of: datetime
    # When the materialised roll-up was last computed. `None` means it has never been
    # built and the counts were computed live on this request.
    computed_at: datetime | None = None


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


# --- OB-1 through OB-4: Observability ----------------------------------------


class SloDefinitionCreate(ApiModel):
    slo_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    name: str = Field(min_length=3, max_length=200)
    target: float = Field(ge=0.0, le=100.0)
    window_days: int = Field(ge=1, le=365)
    threshold: float = Field(ge=0.0, le=100.0)


class SloDefinitionRead(ApiModel):
    id: UUID
    organization_id: UUID
    slo_key: str
    name: str
    target: float
    window_days: int
    threshold: float
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class SloBudgetRead(ApiModel):
    slo_id: UUID
    slo_key: str
    name: str
    target: float
    current_value: float | None
    budget_remaining: float | None
    window_days: int
    status: str


class ArchiveStatusRead(ApiModel):
    total_archives: int
    total_events_archived: int
    latest_archive_id: str | None
    latest_checksum: str | None
    legal_hold_count: int
    status: str


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
    created_at: datetime
    updated_at: datetime
