from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlatformApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class DataProductPortDefinition(PlatformApiModel):
    port_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    direction: Literal["INPUT", "OUTPUT"]
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=3, max_length=1000)
    asset_type: Literal["TABLE", "SEMANTIC_MODEL", "CONTEXT_PRODUCT", "API"]
    asset_id: str = Field(min_length=2, max_length=255)


class DataProductDefinition(PlatformApiModel):
    name: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=10_000)
    domain_name: str = Field(min_length=2, max_length=200)
    owner_principal: str = Field(min_length=2, max_length=255)
    usage_terms: str = Field(min_length=10, max_length=10_000)
    classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
    certification_status: Literal["UNCERTIFIED", "CERTIFIED", "EXPIRED"] = "UNCERTIFIED"
    quality_score: int | None = Field(default=None, ge=0, le=100)
    lineage_coverage: int = Field(default=0, ge=0, le=100)
    context_product_version_id: UUID | None = None
    discoverable_roles: list[str] = Field(default_factory=lambda: ["*"], min_length=1)
    consumer_roles: list[str] = Field(default_factory=list)
    ports: list[DataProductPortDefinition] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_product_definition(self) -> "DataProductDefinition":
        port_keys = [port.port_key for port in self.ports]
        if len(port_keys) != len(set(port_keys)):
            raise ValueError("data product port keys must be unique")
        if not any(port.direction == "OUTPUT" for port in self.ports):
            raise ValueError("a data product requires at least one output port")
        for roles, label in (
            (self.discoverable_roles, "discoverable roles"),
            (self.consumer_roles, "consumer roles"),
        ):
            normalized = [role.strip() for role in roles if role.strip()]
            if len(normalized) != len(roles) or len(normalized) != len(set(normalized)):
                raise ValueError(f"{label} must be non-empty and unique")
        return self


class DataProductCreate(DataProductDefinition):
    product_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")


class DataProductVersionCreate(DataProductDefinition):
    pass


class DataProductVersionRead(DataProductDefinition):
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
    created_at: datetime
    updated_at: datetime


class ContractFieldDefinition(PlatformApiModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
    data_type: str = Field(min_length=1, max_length=255)
    required: bool = False
    description: str | None = Field(default=None, max_length=1000)
    classification: str | None = Field(default=None, max_length=100)


class ContractQualityRuleDefinition(PlatformApiModel):
    rule_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    rule_type: Literal["NOT_NULL", "UNIQUE", "ACCEPTED_VALUES", "FRESHNESS", "CUSTOM"]
    field_name: str | None = Field(default=None, max_length=255)
    severity: Literal["INFO", "WARNING", "CRITICAL"] = "WARNING"
    parameters: dict[str, Any] = Field(default_factory=dict)


class DataContractCreate(PlatformApiModel):
    compatibility_mode: Literal["BACKWARD", "FORWARD", "FULL", "NONE"] = "BACKWARD"
    schema_definition: list[ContractFieldDefinition] = Field(min_length=1, max_length=5000)
    quality_rules: list[ContractQualityRuleDefinition] = Field(
        default_factory=list, max_length=1000
    )
    freshness_sla_minutes: int | None = Field(default=None, ge=1, le=525_600)
    availability_sla_percent: float | None = Field(default=None, ge=0, le=100)
    producer_principal: str = Field(min_length=2, max_length=255)
    consumer_roles: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract(self) -> "DataContractCreate":
        names = [field.name.casefold() for field in self.schema_definition]
        if len(names) != len(set(names)):
            raise ValueError("contract field names must be unique")
        rule_keys = [rule.rule_key for rule in self.quality_rules]
        if len(rule_keys) != len(set(rule_keys)):
            raise ValueError("contract quality rule keys must be unique")
        normalized_roles = [role.strip() for role in self.consumer_roles if role.strip()]
        if len(normalized_roles) != len(self.consumer_roles) or len(normalized_roles) != len(
            set(normalized_roles)
        ):
            raise ValueError("contract consumer roles must be non-empty and unique")
        return self


class DataContractVersionRead(DataContractCreate):
    id: UUID
    organization_id: UUID
    product_id: UUID
    version: int
    status: str
    compatibility_status: str
    compatibility_findings: list[dict[str, Any]]
    fingerprint: str
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MarketplaceAccessRequestCreate(PlatformApiModel):
    purpose: str = Field(min_length=10, max_length=2000)
    duration_days: int = Field(default=30, ge=1, le=365)


class MarketplaceAccessRequestRead(PlatformApiModel):
    id: UUID
    organization_id: UUID
    data_product_version_id: UUID
    requested_by: str
    purpose: str
    duration_days: int
    status: str
    governance_review_id: UUID
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    expires_at: datetime | None
    revoked_by: str | None
    revoked_at: datetime | None
    fulfillment_status: str
    fulfillment_provider: str | None
    fulfillment_reference: str | None
    fulfillment_error: str | None
    fulfilled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EntitlementOperation(PlatformApiModel):
    action: Literal["PROVISION", "REVOKE"]


class MarketplaceProductRead(DataProductVersionRead):
    access_status: Literal["ROLE_GRANTED", "REQUEST_APPROVED", "REQUEST_PENDING", "NOT_REQUESTED"]


class PortfolioLifecycleRead(PlatformApiModel):
    data_products_total: int
    data_products_active: int
    data_products_candidate: int
    data_products_retired: int
    data_product_versions_draft: int
    data_product_versions_review_required: int
    data_product_versions_published: int
    data_product_versions_retired: int
    data_contract_versions_draft: int
    data_contract_versions_review_required: int
    data_contract_versions_published: int
    context_products_total: int
    context_product_versions_draft: int
    context_product_versions_review_required: int
    context_product_versions_published: int
    context_product_versions_deprecated: int


class PortfolioAccessRead(PlatformApiModel):
    requests_created: int
    requests_pending: int
    requests_approved: int
    requests_rejected: int
    requests_revoked: int
    requests_expired: int
    active_grants: int
    grants_expiring_within_30_days: int
    fulfillment_pending: int
    fulfillment_provisioned: int
    fulfillment_failed: int
    fulfillment_revoked: int


class PortfolioUsageRead(PlatformApiModel):
    unique_context_consumers: int
    unique_mcp_consumers: int
    unique_agent_principals: int
    context_product_reads: int
    mcp_operations: int
    mcp_resource_reads: int
    mcp_prompt_reads: int
    mcp_tool_calls: int
    mcp_control_operations: int
    agent_runs: int
    governed_tool_agent_runs: int
    model_gateway_agent_runs: int
    development_override_agent_runs: int
    policy_blocked_agent_runs: int
    query_executions: int
    governed_tool_executions: int


class PortfolioQualityRead(PlatformApiModel):
    published_products: int
    scored_products: int
    average_quality_score: float | None
    low_quality_products: int
    certified_products: int
    uncertified_products: int
    average_lineage_coverage: float | None


class PortfolioQueueRead(PlatformApiModel):
    review_required_data_product_versions: int
    review_required_data_contract_versions: int
    review_required_context_product_versions: int
    pending_marketplace_access_requests: int


class PortfolioTopProductRead(PlatformApiModel):
    data_product_version_id: UUID
    product_key: str
    name: str
    domain_name: str
    certification_status: str
    quality_score: int | None
    lineage_coverage: int
    access_request_count: int
    approved_access_count: int
    context_read_count: int


class PortfolioAnalyticsSummaryRead(PlatformApiModel):
    generated_at: datetime
    window_days: int
    low_quality_threshold: int
    lifecycle: PortfolioLifecycleRead
    access: PortfolioAccessRead
    usage: PortfolioUsageRead
    quality: PortfolioQualityRead
    queues: PortfolioQueueRead
    top_products: list[PortfolioTopProductRead]


class PortfolioTrendPointRead(PlatformApiModel):
    bucket_start: datetime
    bucket_end: datetime
    access_requests: int
    context_reads: int
    mcp_operations: int
    mcp_tool_calls: int
    agent_runs: int
    governed_tool_runs: int
    model_gateway_runs: int
    query_executions: int


class PortfolioAnalyticsTrendsRead(PlatformApiModel):
    generated_at: datetime
    window_days: int
    bucket_days: int
    points: list[PortfolioTrendPointRead]


ContextCompilerTarget = Literal[
    "MCP",
    "REST",
    "YAML",
    "OSI",
    "ODCS",
    "SNOWFLAKE_SEMANTIC_VIEW",
    "DATABRICKS_METRIC_VIEW",
]


class ContextCompilationRead(PlatformApiModel):
    target: ContextCompilerTarget
    content_type: str
    content: str
    artifact_hash: str
    source_fingerprint: str
    generated_from: dict[str, Any]


class ContextCompilationDriftRequest(PlatformApiModel):
    target: ContextCompilerTarget
    deployed_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    deployed_content: str | None = Field(default=None, max_length=5_000_000)

    @model_validator(mode="after")
    def require_deployed_evidence(self) -> "ContextCompilationDriftRequest":
        if self.deployed_hash is None and self.deployed_content is None:
            raise ValueError("deployed_hash or deployed_content is required")
        return self


class ContextCompilationDriftRead(PlatformApiModel):
    target: ContextCompilerTarget
    drifted: bool
    expected_hash: str
    deployed_hash: str
    changed_paths: list[str]


class ContextCompilationValidateRequest(PlatformApiModel):
    target: ContextCompilerTarget
    content: str = Field(min_length=2, max_length=5_000_000)


class ContextCompilationValidationRead(PlatformApiModel):
    target: ContextCompilerTarget
    valid: bool
    findings: list[str]


class AiAssetDefinition(PlatformApiModel):
    name: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=10_000)
    intended_use: str = Field(min_length=10, max_length=10_000)
    owner_principal: str = Field(min_length=2, max_length=255)
    provider_type: str = Field(min_length=2, max_length=50)
    risk_tier: Literal["LOW", "MEDIUM", "HIGH", "PROHIBITED"]
    documentation_url: str | None = Field(default=None, max_length=1000)
    context_product_version_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    model_route_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    policy_control_ids: list[str] = Field(default_factory=list, max_length=1000)
    evaluation_evidence: dict[str, Any] = Field(default_factory=dict)
    runtime_evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ai_references(self) -> "AiAssetDefinition":
        for values, label in (
            (self.context_product_version_ids, "context product references"),
            (self.model_route_ids, "model route references"),
            (self.policy_control_ids, "policy controls"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        for evidence, label in (
            (self.evaluation_evidence, "evaluation evidence"),
            (self.runtime_evidence, "runtime evidence"),
        ):
            if any(key.lower() in {"prompt", "question", "sql", "raw_value"} for key in evidence):
                raise ValueError(f"{label} cannot contain raw prompt, SQL, or source values")
        return self


class AiAssetCreate(AiAssetDefinition):
    asset_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    asset_kind: Literal["AI_USE_CASE", "MODEL", "AGENT"]


class AiAssetVersionRead(AiAssetDefinition):
    id: UUID
    organization_id: UUID
    asset_id: UUID
    asset_key: str
    asset_kind: str
    version: int
    status: str
    fingerprint: str
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AiAssessmentControlResult(PlatformApiModel):
    control_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,99}$")
    title: str = Field(min_length=3, max_length=500)
    weight: int = Field(default=1, ge=1, le=100)
    outcome: Literal["PASS", "FAIL", "NOT_APPLICABLE"]
    evidence_reference: str | None = Field(default=None, max_length=1000)
    finding: str | None = Field(default=None, max_length=2000)


class AiAssessmentCreate(PlatformApiModel):
    framework: Literal["EU_AI_ACT", "NIST_AI_RMF", "AI_UC_1", "CUSTOM"]
    framework_version: str = Field(min_length=1, max_length=50)
    control_results: list[AiAssessmentControlResult] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_controls(self) -> "AiAssessmentCreate":
        keys = [item.control_key for item in self.control_results]
        if len(keys) != len(set(keys)):
            raise ValueError("assessment control keys must be unique")
        return self


class AiAssessmentRead(AiAssessmentCreate):
    id: UUID
    organization_id: UUID
    ai_asset_version_id: UUID
    status: str
    score: int
    findings: list[dict[str, Any]]
    assessed_by: str
    created_at: datetime
    updated_at: datetime


class AiTrustFactorRead(PlatformApiModel):
    factor: str
    score: float
    maximum: float
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class AiTrustScoreRead(PlatformApiModel):
    ai_asset_version_id: UUID
    score: int
    grade: Literal["TRUSTED", "CONDITIONAL", "UNTRUSTED", "BLOCKED"]
    factors: list[AiTrustFactorRead]
    blockers: list[str]
    computed_at: datetime


class AiAssessmentTemplateRead(PlatformApiModel):
    template_key: str
    framework: str
    framework_version: str
    title: str
    controls: list[AiAssessmentControlResult]


class AiRemediationCreate(PlatformApiModel):
    finding_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,99}$")
    title: str = Field(min_length=3, max_length=500)
    description: str = Field(min_length=10, max_length=10_000)
    owner_principal: str = Field(min_length=2, max_length=255)
    due_at: datetime | None = None


class AiRemediationUpdate(PlatformApiModel):
    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "ACCEPTED_RISK"]
    resolution_evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_raw_remediation_evidence(self) -> "AiRemediationUpdate":
        forbidden = {"prompt", "question", "sql", "raw_value", "token"}
        if any(str(key).lower() in forbidden for key in self.resolution_evidence):
            raise ValueError("remediation evidence must be value-free")
        return self


class AiRemediationRead(AiRemediationCreate):
    id: UUID
    organization_id: UUID
    ai_asset_version_id: UUID
    status: str
    resolution_evidence: dict[str, Any]
    created_by: str
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AiTrustSnapshotRead(PlatformApiModel):
    id: UUID
    ai_asset_version_id: UUID
    score: int
    grade: str
    factors: list[dict[str, Any]]
    blockers: list[str]
    input_fingerprint: str
    computed_at: datetime


class AiProviderSyncRequest(PlatformApiModel):
    provider_type: str = Field(min_length=2, max_length=50)
    external_reference: str = Field(min_length=2, max_length=500)
    documentation_url: str | None = Field(default=None, max_length=1000)
    evaluation_evidence: dict[str, Any] = Field(default_factory=dict)
    runtime_evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_raw_provider_evidence(self) -> "AiProviderSyncRequest":
        forbidden = {"prompt", "question", "sql", "raw_value", "token", "secret"}
        for evidence in (self.evaluation_evidence, self.runtime_evidence):
            if any(str(key).lower() in forbidden for key in evidence):
                raise ValueError("provider evidence must be value-free")
        return self


class AiDependencyGraphRead(PlatformApiModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
