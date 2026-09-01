"""observability audit -- PRIVATE. Request/response models for `router.py`.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.schemas`, which now re-exports these classes for backward
compatibility -- every existing `from aida.schemas import X` caller keeps
working unchanged.

Covers the request/response DTOs for this module's owned models
(`atlas.modules.observability_audit.models`): the audit ledger, the
outbox, SLO definitions/budgets, and archive status. `SloBudgetRead` and
`ArchiveStatusRead` are composed read models, but composed only from this
module's own tables (`SloDefinition`+`SloMeasurement`,
`AuditArchiveRecord` respectively) -- unlike `FleetSummaryRead` or
`LobCostRowRead`/`CostShowbackTotalsRead` (OB-6), which stay in
`aida.schemas` because they genuinely compose fields sourced from other
modules' tables (datasources, analysis runs, query executions, lines of
business) not yet extracted.

`EntitlementReportRead` (OB-7) is **not** here -- it moved to
`atlas.modules.identity_tenancy.schemas` earlier in this same refactor
pass, before this module's ownership of its backing table
(`AccessReviewReportRecord`) was worked out. See the docstring on that
model in `atlas.modules.observability_audit.models` for why a DTO and
its backing table living in different modules here is intentional
composition, not an inconsistency to fix.

`ApiModel` stays defined in `aida.schemas` rather than moving here or to
`atlas.platform` -- it is the shared pydantic base for every module's
schemas, not this module's, and moving it is out of scope for this pass.
Importing it back from `aida.schemas` here works safely only because
`aida.schemas`' shim import of this module comes *after* `ApiModel` is
defined in that file -- see the comment there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from aida.schemas import ApiModel


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
