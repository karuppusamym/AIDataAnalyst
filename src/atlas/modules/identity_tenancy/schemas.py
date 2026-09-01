"""identity tenancy -- PRIVATE. Request/response models for `router.py`.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.schemas`, which now re-exports these classes for backward
compatibility -- every existing `from aida.schemas import X` caller keeps
working unchanged.

Covers the request/response DTOs for this module's owned models
(`atlas.modules.identity_tenancy.models`): organizations, lines of
business, data domains, cross-boundary grants, projects, workspaces,
workspace memberships, source bindings, the business-node classification
tree, and self-service entitlement reporting (OB-7).

`ClassificationDecisionRead` is grouped in with the entitlement-report
DTOs (its only use site, `EntitlementReportRead`) even though the ABAC
decision it describes is evaluated by policy-governance (module 17) --
splitting it into a module not yet extracted would just relocate the
problem, and it carries no policy-authoring logic of its own, only
decision/reason strings for display.

`ApiModel` stays defined in `aida.schemas` rather than moving here or to
`atlas.platform` -- it is the shared pydantic base for every module's
schemas, not identity-tenancy-owned, and moving it is out of scope for
this pass. Importing it back from `aida.schemas` here works safely only
because `aida.schemas`' shim import of this module comes *after*
`ApiModel` is defined in that file -- see the comment there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from aida.integration_catalog import normalized_transformation_metadata_integrations
from aida.schemas import ApiModel


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
    def normalize_integrations(self) -> OrganizationIntegrationPolicyWrite:
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


class WorkspaceEntitlementRead(ApiModel):
    workspace_id: UUID
    workspace_name: str
    workspace_slug: str
    role: str
    granted_by: str
    expires_at: datetime | None


class SourceEntitlementRead(ApiModel):
    workspace_id: UUID
    datasource_id: UUID
    datasource_name: str
    line_of_business_code: str | None
    line_of_business_name: str | None
    schema_scope: list[str]
    permitted_classifications: list[str]
    masking_profile: str
    purpose: str
    expires_at: datetime | None


class ClassificationDecisionRead(ApiModel):
    classification: str
    decision: str
    reasons: list[str]
    contributing_policy_ids: list[str]


class GenerateEntitlementReportRequest(ApiModel):
    # Omit to generate a self-service report for the caller's own identity.
    # Set to pull a report for a different principal -- requires an elevated
    # role and is always audited as generated "on behalf of" that principal.
    principal_id: str | None = Field(default=None, min_length=1, max_length=255)
    principal_type: str = Field(default="USER", max_length=30)


class EntitlementReportRead(ApiModel):
    id: UUID
    organization_id: UUID
    subject_principal_id: str
    subject_principal_type: str
    is_self_service: bool
    requested_by: str
    workspace_memberships: list[WorkspaceEntitlementRead]
    source_entitlements: list[SourceEntitlementRead]
    abac_classification_decisions: list[ClassificationDecisionRead]
    abac_note: str
    checksum: str
    generated_at: datetime
    created_at: datetime
    updated_at: datetime
