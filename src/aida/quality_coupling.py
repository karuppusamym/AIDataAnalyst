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

from aida.models import (
    DataQualityIncident,
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
)


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
