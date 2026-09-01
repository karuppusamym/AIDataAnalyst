"""connectivity -- PRIVATE. Request/response models for `router.py`.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.schemas`, which now re-exports these classes for backward
compatibility -- every existing `from aida.schemas import X` caller keeps
working unchanged.

Covers the request/response DTOs for this module's owned models
(`atlas.modules.connectivity.models`): datasource registration (including
bulk onboarding, IN-1), and the connector capability/certification
read surfaces. `ConnectorCapabilityRead` has no backing table of its own
-- it projects the built-in connector registry's declared capabilities --
but is grouped here since it is connectivity's own read surface, not any
other module's.

`ApiModel` stays defined in `aida.schemas` rather than moving here or to
`atlas.platform` -- it is the shared pydantic base for every module's
schemas, not connectivity-owned, and moving it is out of scope for this
pass. Importing it back from `aida.schemas` here works safely only
because `aida.schemas`' shim import of this module comes *after*
`ApiModel` is defined in that file -- see the comment there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from aida.schemas import ApiModel


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
    def require_change(self) -> DataSourceUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one datasource field must be provided")
        return self


# IN-1: a single bulk-onboarding request may register at most this many
# datasources in one operation -- the tracker's own exit condition ("200
# sources onboarded in one operation") names the number; the cap is set to
# exactly that round number rather than pulled from CATALOG_BULK_ACTION_MAX_ITEMS
# or RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS (both 500), since a batch of
# 200 *datasource registrations* -- each its own credential-reference,
# connector-type and per-project-uniqueness check, and its own audit/outbox
# event -- is deliberately smaller than a batch of catalog tag/decision
# mutations on existing rows. A request above the cap is rejected outright
# (422, same as CatalogBulk*Request's `max_length` precedent), never silently
# truncated to the first 200.
DATASOURCE_BULK_ONBOARD_MAX_ITEMS = 200


class DataSourceBulkOnboardRequest(ApiModel):
    datasources: list[DataSourceCreate] = Field(
        min_length=1, max_length=DATASOURCE_BULK_ONBOARD_MAX_ITEMS
    )


class DataSourceBulkOnboardItemRead(ApiModel):
    index: int
    name: str
    status: Literal["SUCCEEDED", "FAILED"]
    datasource_id: UUID | None = None
    reason: str | None = None


class DataSourceBulkOnboardResultRead(ApiModel):
    requested_count: int
    succeeded_count: int
    failed_count: int
    results: list[DataSourceBulkOnboardItemRead]


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
