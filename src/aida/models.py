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
    datasource_id: Mapped[UUID] = mapped_column(
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
    __tablename__ = "asset_certification"
    __table_args__ = (Index("ix_asset_certification_org_status", "organization_id", "status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    __tablename__ = "dbt_lineage_edge"
    __table_args__ = (
        UniqueConstraint(
            "artifact_import_id",
            "source_resource_id",
            "target_resource_id",
            name="uq_dbt_lineage_edge_import_source_target",
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
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    table_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    semantic_model_version_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    glossary_term_version_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    eligible_tool_version_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    allowed_consumer_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    lineage_depth: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    quality_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
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
