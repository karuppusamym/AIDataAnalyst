"""Quality-runtime coupling (DQ-3).

Integrates quality incident status with runtime decisions: demotion in
retrieval ranking, trust warnings on answers, and tool gating when
upstream dependencies have quality incidents.

The functions above `resolve_table_ids`/`fetch_open_incidents` are pure and
database-free by design (see the tests in `tests/test_quality_coupling.py`).
`resolve_table_ids` and `fetch_open_incidents` are the one real wiring point
that turns a datasource-scoped SQL table reference into the `IncidentSummary`
rows those pure functions consume -- kept here, not duplicated at each call
site, so `tool_api.py::execute_tool` (TL-3) and
`agent_orchestrator.py::GovernedAgentOrchestrator.run` (AG-6) resolve
incidents identically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit, record_outbox
from aida.models import (
    AssetCertification,
    DataQualityIncident,
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
)
from aida.security import SecurityContext


@dataclass(frozen=True, slots=True)
class QualityGate:
    """Gate result for an asset with quality issues."""

    asset_id: str
    incident_severity: str
    gate_action: str  # DEMOTE, WARN, BLOCK


@dataclass(frozen=True, slots=True)
class TrustWarning:
    """Warning surfaced when answers use affected assets."""

    asset_id: str
    message: str
    severity: str
    incident_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ToolGateResult:
    """Gate result for a tool whose dependencies have incidents."""

    tool_id: str
    action: str  # ALLOW, WARN, BLOCK
    affected_assets: list[str] = field(default_factory=list)
    message: str = ""


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    """Simplified incident data for coupling decisions."""

    incident_id: str
    asset_id: str
    severity: str
    status: str  # OPEN, ACKNOWLEDGED, RESOLVED
    anomaly_type: str


def check_quality_gate(
    asset_id: str, incidents: list[IncidentSummary]
) -> QualityGate | None:
    """Check if an asset has active quality incidents warranting a gate action.

    Returns None if no quality gate should be applied.
    """
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    if not active:
        return None

    has_critical = any(i.severity == "CRITICAL" for i in active)
    if has_critical:
        return QualityGate(
            asset_id=asset_id,
            incident_severity="CRITICAL",
            gate_action="BLOCK",
        )

    return QualityGate(
        asset_id=asset_id,
        incident_severity="WARNING",
        gate_action="DEMOTE",
    )


def demote_in_retrieval(
    asset_id: str, incidents: list[IncidentSummary]
) -> float:
    """Return a demotion factor (0.0-1.0) for retrieval ranking.

    1.0 means no demotion, lower values push the asset down in results.
    """
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    if not active:
        return 1.0

    has_critical = any(i.severity == "CRITICAL" for i in active)
    if has_critical:
        return 0.3

    warning_count = sum(1 for i in active if i.severity == "WARNING")
    return max(0.5, 1.0 - (warning_count * 0.15))


def get_trust_warning(
    asset_id: str, incidents: list[IncidentSummary]
) -> TrustWarning | None:
    """Generate a trust warning for answers using affected assets."""
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    if not active:
        return None

    has_critical = any(i.severity == "CRITICAL" for i in active)
    severity = "CRITICAL" if has_critical else "WARNING"
    incident_count = len(active)
    message = (
        f"Asset {asset_id} has {incident_count} active quality "
        f"incident{'s' if incident_count > 1 else ''} "
        f"(highest severity: {severity}). Results may be unreliable."
    )
    return TrustWarning(
        asset_id=asset_id,
        message=message,
        severity=severity,
        incident_ids=[i.incident_id for i in active],
    )


def check_tool_gate(
    tool_id: str,
    dependency_asset_ids: list[str],
    incidents: list[IncidentSummary],
) -> ToolGateResult:
    """Check whether a tool should be blocked/warned based on dependency incidents."""
    affected: list[str] = []
    max_severity = "INFO"

    for asset_id in dependency_asset_ids:
        active = [
            i for i in incidents
            if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
        ]
        if active:
            affected.append(asset_id)
            if any(i.severity == "CRITICAL" for i in active):
                max_severity = "CRITICAL"
            elif max_severity != "CRITICAL":
                max_severity = "WARNING"

    if not affected:
        return ToolGateResult(tool_id=tool_id, action="ALLOW")

    if max_severity == "CRITICAL":
        action = "BLOCK"
        message = (
            f"Tool {tool_id} blocked: {len(affected)} upstream "
            f"asset(s) have critical quality incidents."
        )
    else:
        action = "WARN"
        message = (
            f"Tool {tool_id} warning: {len(affected)} upstream "
            f"asset(s) have quality incidents."
        )

    return ToolGateResult(
        tool_id=tool_id,
        action=action,
        affected_assets=affected,
        message=message,
    )


async def resolve_table_ids(
    session: AsyncSession,
    *,
    datasource: DataSource,
    table_names: Sequence[str],
) -> dict[str, UUID]:
    """Resolve SQL-qualified table names to this datasource's `MetadataTable` ids.

    Accepts the same qualified/unqualified name shapes
    `QueryExecutionGateway.allowed_tables` already authorises against
    (``schema.table``, ``catalog.schema.table``, or an unambiguous bare table
    name), case-insensitively, so a name a tool's SQL was authorised to touch
    resolves to the same table row here. A name that does not resolve within
    this datasource is simply absent from the returned mapping.
    """
    leaf_names = {name.rsplit(".", 1)[-1].lower() for name in table_names}
    if not leaf_names:
        return {}
    rows = (
        await session.execute(
            select(
                MetadataCatalog.name,
                MetadataSchema.name,
                MetadataTable.name,
                MetadataTable.id,
            )
            .join(MetadataSchema, MetadataSchema.catalog_id == MetadataCatalog.id)
            .join(MetadataTable, MetadataTable.schema_id == MetadataSchema.id)
            .where(
                MetadataCatalog.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                func.lower(MetadataTable.name).in_(leaf_names),
            )
        )
    ).all()
    by_qualified: dict[str, UUID] = {}
    by_leaf: dict[str, list[UUID]] = {}
    for catalog_name, schema_name, table_name, table_id in rows:
        by_qualified[f"{schema_name}.{table_name}".lower()] = table_id
        by_qualified[f"{catalog_name}.{schema_name}.{table_name}".lower()] = table_id
        by_leaf.setdefault(table_name.lower(), []).append(table_id)
    for leaf, ids in by_leaf.items():
        if len(ids) == 1:
            by_qualified.setdefault(leaf, ids[0])
    return {
        name: by_qualified[name.lower()] for name in table_names if name.lower() in by_qualified
    }


async def fetch_open_incidents(
    session: AsyncSession,
    *,
    datasource: DataSource,
    table_ids: Sequence[UUID],
) -> list[IncidentSummary]:
    """Fetch the OPEN/ACKNOWLEDGED quality incidents for the given tables."""
    if not table_ids:
        return []
    rows = (
        await session.scalars(
            select(DataQualityIncident).where(
                DataQualityIncident.datasource_id == datasource.id,
                DataQualityIncident.table_id.in_(list(table_ids)),
                DataQualityIncident.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
        )
    ).all()
    return [
        IncidentSummary(
            incident_id=str(row.id),
            asset_id=str(row.table_id),
            severity=row.severity,
            status=row.status,
            anomaly_type=row.anomaly_type,
        )
        for row in rows
    ]


def should_expire_certification(
    asset_id: str,
    incidents: list[IncidentSummary],
    *,
    sustained_threshold: int = 3,
) -> bool:
    """Determine whether sustained incidents should expire asset certification.

    An asset's certification expires when the number of unresolved incidents
    reaches or exceeds the sustained threshold.
    """
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    return len(active) >= sustained_threshold


async def expire_sustained_incident_certifications(
    session: AsyncSession,
    *,
    organization_id: UUID,
    table_ids: Sequence[UUID],
    incidents: list[IncidentSummary],
    context: SecurityContext,
    sustained_threshold: int = 3,
) -> list[AssetCertification]:
    """DQ-3 (module 11 §9's fifth coupling row): a table whose own
    `should_expire_certification` fires loses its certification.

    Expired, not deleted or backdated: only `status` moves to `"EXPIRED"`,
    the identical single-field-write shape every other certification
    transition in this codebase already uses for `"SUPERSEDED"`
    (`api.py::certify_table_asset`, `catalog_bulk_actions.apply_certify_item`,
    `stewardship_service`'s reviewed `CERTIFY_ASSET` branch) --
    `rationale`/`certified_by`/`expires_at` stay exactly what the table was
    certified as, so certification history is never mutated by anything but
    a new certification (`AssetCertification`'s own docstring). No new read
    path is needed either: `catalog_read_model._certification_state`'s
    existing fall-through already reports any non-`ACTIVE`, non-`REVOKED`
    status as `"EXPIRED"`.

    Scoped to `asset_type == "TABLE"` certifications only -- `DataQualityIncident`
    has no `column_id`, so there is no real per-column incident signal to
    expire a `COLUMN` certification on without inventing one.
    """
    if not table_ids:
        return []
    expiring_table_ids = [
        table_id
        for table_id in table_ids
        if should_expire_certification(
            str(table_id), incidents, sustained_threshold=sustained_threshold
        )
    ]
    if not expiring_table_ids:
        return []
    active_certifications = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.organization_id == organization_id,
                AssetCertification.table_id.in_(expiring_table_ids),
                AssetCertification.asset_type == "TABLE",
                AssetCertification.status == "ACTIVE",
            )
        )
    ).all()
    expired: list[AssetCertification] = []
    for certification in active_certifications:
        certification.status = "EXPIRED"
        record_audit(
            session,
            context,
            action="catalog.asset.certification_expired",
            resource_type="asset_certification",
            resource_id=str(certification.id),
            outcome="SUCCESS",
            correlation_id=str(certification.table_id),
            details={
                "table_id": str(certification.table_id),
                "reason": "SUSTAINED_QUALITY_INCIDENTS",
                "sustained_threshold": sustained_threshold,
            },
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="asset_certification",
            aggregate_id=str(certification.id),
            event_type="catalog.asset.certification_expired.v1",
            payload={
                "table_id": str(certification.table_id),
                "certification_id": str(certification.id),
            },
        )
        expired.append(certification)
    return expired
