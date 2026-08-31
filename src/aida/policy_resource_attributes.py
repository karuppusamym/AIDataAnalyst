"""AU-11: real resource attributes for the policy gate on the query path.

`authorization_gate.gate()` -- and, beneath it, `policy_engine.evaluate()` --
already accepts `classifications`, `certification`, `quality_state` and
`freshness_state` as first-class `Resource` attributes (`policy_engine.py`).
Before this module existed, `query_gateway.py` called `gate()` without any of
them, so every policy rule keyed on those axes was structurally unreachable
from the query-execution path even though the engine underneath already
understood them (AU-11, `Docs/60-delivery/03-tracker.md`).

This module resolves real values for the tables a query actually references,
batched -- one query per axis, not one per table -- and collapses each axis
to a single worst-case value across every referenced table:

* **classifications** is the union: a query touching one PII column is a
  query touching PII, so nothing is dropped by collapsing.
* **certification**, **quality_state** and **freshness_state** each take the
  most restrictive state present, because a query is only as certified,
  healthy or fresh as its least certified, healthy or fresh input. A single
  stale or incident-affected table should not hide behind nine clean ones.

Sources, one per axis:

* classification -- `MetadataColumn.classification` (module 05).
* certification -- `AssetCertification` (GL-5's bulk review / CT-5's
  per-table and per-column certification with expiry), via
  `aida.asset_certification.current_asset_certification` for the same
  query-time active-certification projection every other caller uses.
* quality_state -- `DataQualityIncident` (module 11's durable incident
  ledger) first, since an open incident is the durable governance signal;
  falling back to the latest `DataQualityObservation.status` (`HEALTHY` /
  `WARNING` / `CRITICAL` / `NO_BASELINE`) when there is no open incident.
* freshness_state -- `FreshnessWatermarkConfig` + `FreshnessObservation`,
  evaluated through `aida.freshness.evaluate_freshness` (DQ-2), the same
  watermark-only evaluation `quality_api.get_freshness_status` uses --
  ADR-0016: scan age is never presented as freshness.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_certification import current_asset_certification
from aida.freshness import WatermarkConfig, evaluate_freshness
from aida.models import (
    AssetCertification,
    DataQualityIncident,
    DataQualityObservation,
    DataSource,
    FreshnessObservation,
    FreshnessWatermarkConfig,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
)

# Worst-to-best ordering for collapsing per-table states to one value per query.
_QUALITY_STATE_ORDER = {
    "CRITICAL": 4,
    "DEGRADED": 3,
    "WARNING": 2,
    "NO_BASELINE": 1,
    "HEALTHY": 0,
}
_FRESHNESS_STATE_ORDER = {
    "STALE": 3,
    "AWAITING_APPROVAL": 2,
    "NOT_CONFIGURED": 1,
    "FRESH": 0,
}


@dataclass(frozen=True, slots=True)
class ResourceAttributes:
    """What the policy gate needs to know about the tables a query touches."""

    classifications: frozenset[str] = frozenset()
    certification: str | None = None
    quality_state: str | None = None
    freshness_state: str | None = None


async def resolve_referenced_table_ids(
    session: AsyncSession,
    datasource: DataSource,
    referenced_tables: Sequence[str],
) -> frozenset[UUID]:
    """Map the guard's parsed table names onto real `MetadataTable` ids.

    Same leaf-name matching `QueryExecutionGateway._catalog_columns` already
    uses: a name that resolves as a table for column lookup resolves as a
    table here too. Deliberately permissive rather than precise about
    schema-qualification ambiguity -- every axis this feeds is collapsed to
    its worst case anyway, so picking up an extra same-named table from
    another schema can only make a decision more conservative, never less.
    """
    leaf_names = {table.rsplit(".", 1)[-1].lower() for table in referenced_tables}
    if not leaf_names:
        return frozenset()
    rows = await session.scalars(
        select(MetadataTable.id)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            MetadataCatalog.datasource_id == datasource.id,
            MetadataTable.organization_id == datasource.organization_id,
            MetadataTable.status == "ACTIVE",
            func.lower(MetadataTable.name).in_(leaf_names),
        )
    )
    return frozenset(rows.all())


async def _resolve_classifications(
    session: AsyncSession, table_ids: frozenset[UUID]
) -> frozenset[str]:
    if not table_ids:
        return frozenset()
    rows = await session.scalars(
        select(MetadataColumn.classification)
        .distinct()
        .where(
            MetadataColumn.table_id.in_(table_ids),
            MetadataColumn.status == "ACTIVE",
        )
    )
    return frozenset(value for value in rows.all() if value and value != "UNCLASSIFIED")


async def _resolve_certification(
    session: AsyncSession, table_ids: frozenset[UUID], *, now: datetime
) -> str | None:
    """"CERTIFIED" only when every referenced table has a currently-active
    certification (`asset_certification_is_active`); "UNCERTIFIED" the moment
    any one of them does not."""
    if not table_ids:
        return None
    rows = (
        await session.execute(
            select(AssetCertification)
            .where(
                AssetCertification.table_id.in_(table_ids),
                AssetCertification.asset_type == "TABLE",
            )
            # Composite ORDER BY sorts newest-first *within* each table_id group,
            # which is exactly what `current_asset_certification` expects.
            .order_by(AssetCertification.table_id, AssetCertification.created_at.desc())
        )
    ).scalars().all()
    by_table: dict[UUID, list[AssetCertification]] = {}
    for row in rows:
        by_table.setdefault(row.table_id, []).append(row)
    for table_id in table_ids:
        if current_asset_certification(by_table.get(table_id, []), at=now) is None:
            return "UNCERTIFIED"
    return "CERTIFIED"


async def _resolve_quality_state(
    session: AsyncSession, table_ids: frozenset[UUID]
) -> str | None:
    if not table_ids:
        return None
    incident_severities = (
        await session.scalars(
            select(DataQualityIncident.severity).where(
                DataQualityIncident.table_id.in_(table_ids),
                DataQualityIncident.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
        )
    ).all()
    if any(severity == "CRITICAL" for severity in incident_severities):
        return "CRITICAL"
    if incident_severities:
        return "DEGRADED"

    obs_rows = (
        await session.execute(
            select(
                DataQualityObservation.table_id,
                DataQualityObservation.status,
            )
            .where(DataQualityObservation.table_id.in_(table_ids))
            .order_by(DataQualityObservation.created_at.desc())
        )
    ).all()
    if not obs_rows:
        return "NOT_OBSERVED"
    latest_by_table: dict[UUID, str] = {}
    for table_id, status in obs_rows:
        latest_by_table.setdefault(table_id, status)
    return max(latest_by_table.values(), key=lambda status: _QUALITY_STATE_ORDER.get(status, 0))


async def _resolve_freshness_state(
    session: AsyncSession,
    datasource: DataSource,
    table_ids: frozenset[UUID],
    *,
    now: datetime,
) -> str | None:
    if not table_ids:
        return None
    configs = (
        await session.execute(
            select(FreshnessWatermarkConfig).where(
                FreshnessWatermarkConfig.datasource_id == datasource.id,
                FreshnessWatermarkConfig.table_id.in_(table_ids),
            )
        )
    ).scalars().all()
    config_by_table = {config.table_id: config for config in configs}

    observation_rows = (
        await session.execute(
            select(
                FreshnessObservation.table_id,
                FreshnessObservation.watermark_value,
            )
            .where(FreshnessObservation.table_id.in_(table_ids))
            .order_by(FreshnessObservation.observed_at.desc())
        )
    ).all()
    latest_watermark_by_table: dict[UUID, datetime] = {}
    for table_id, watermark_value in observation_rows:
        latest_watermark_by_table.setdefault(table_id, watermark_value)

    states: list[str] = []
    for table_id in table_ids:
        config_row = config_by_table.get(table_id)
        wm_config = (
            None
            if config_row is None
            else WatermarkConfig(
                table_id=str(config_row.table_id),
                watermark_column=config_row.watermark_column,
                classification=config_row.classification,
                threshold_minutes=config_row.threshold_minutes,
                retention_days=config_row.retention_days,
                approved_by=config_row.approved_by,
                approved_at=config_row.approved_at,
                status=config_row.status,
            )
        )
        result = evaluate_freshness(
            wm_config, latest_watermark_by_table.get(table_id), evaluation_time=now
        )
        states.append(result.status)
    return max(states, key=lambda status: _FRESHNESS_STATE_ORDER.get(status, 0))


async def resolve_resource_attributes(
    session: AsyncSession,
    datasource: DataSource,
    table_ids: frozenset[UUID],
    *,
    now: datetime | None = None,
) -> ResourceAttributes:
    """Resolve the gate's `classifications`/`certification`/`quality_state`/
    `freshness_state` for one query, from the tables it actually references.

    An empty `table_ids` (an unresolvable or table-less statement) resolves
    every axis to its empty/`None` default -- the same behaviour the gate had
    before this module existed -- rather than guessing.
    """
    moment = now or datetime.now(UTC)
    return ResourceAttributes(
        classifications=await _resolve_classifications(session, table_ids),
        certification=await _resolve_certification(session, table_ids, now=moment),
        quality_state=await _resolve_quality_state(session, table_ids),
        freshness_state=await _resolve_freshness_state(session, datasource, table_ids, now=moment),
    )
