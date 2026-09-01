"""AT-13: DB-reading composition for `get_asset_context` (`aida.mcp_server`).

Reuses the exact typed composition functions UX-13's `asset_evidence.py`
already reuses from `catalog_read_model.py` for its OWNERSHIP/CERTIFICATION/
DATA_QUALITY evidence sections (`_earliest_active_owners`,
`_latest_approved_documentation`, `_latest_certifications`,
`_certification_state`, `_open_incident_table_ids`, `_latest_observation_at`,
`_quality_state`) -- the same precedence rules, not re-derived.

This module deliberately does NOT call `asset_evidence.compose_asset_evidence`
itself: that function also composes business-meaning, consumption (CX-4) and
AI-decision (LN-3) evidence, outside AT-13's five-fact scope (certification,
quality, classification, lineage depth, owner), and returns human-readable
prose claims rather than typed state. Reusing the lower typed layer instead
gets the same certification/quality precedence without re-parsing prose back
into state, and without the extra unrelated reads.

Classification is genuinely new plumbing, honestly: no table-level
classification field or function exists anywhere in this codebase today --
AT-11 ("classification propagation along lineage, derived kept separate from
asserted") is still TODO in `Docs/60-delivery/03-tracker.md`. What *does*
exist is column-level `MetadataColumn.classification` (module 05), already
the ABAC input `query_gateway.py` masks reads against and the vocabulary
`aida.classification.SENSITIVE_CLASSES` names. `_classification_summary`
below aggregates that existing per-column data up to the table -- a rollup
of already-recorded facts, not a new classification judgement -- and the
composed response says so explicitly (see `mcp_server._handle_get_asset_context`)
rather than presenting a table-level classification as an established,
versioned fact the way GL-5 certification or module 11 quality are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.catalog_read_model import (
    _certification_state,
    _earliest_active_owners,
    _latest_approved_documentation,
    _latest_certifications,
    _latest_observation_at,
    _open_incident_table_ids,
    _quality_state,
)
from aida.classification import SENSITIVE_CLASSES
from aida.models import DataQualityIncident, MetadataColumn, MetadataTable

_OPEN_INCIDENT_STATUSES = ("OPEN", "ACKNOWLEDGED")


@dataclass(frozen=True, slots=True)
class ClassificationSummary:
    """Table-level rollup of `MetadataColumn.classification` -- a new
    aggregation of existing per-column data (see module docstring), not a
    stored table-level fact."""

    total_columns: int
    classified_columns: int
    distinct_classifications: tuple[str, ...]
    has_sensitive_classification: bool


@dataclass(frozen=True, slots=True)
class AssetContextSignals:
    """The composed inputs `get_asset_context` returns and
    `asset_usage_decision.compute_usage_decision` consumes, for one table."""

    owner: str | None
    owner_source: str | None
    certification_state: str
    certification_expires_at: datetime | None
    quality_state: str
    open_incident_count: int
    has_open_critical_incident: bool
    classification: ClassificationSummary


async def _classification_summary(session: AsyncSession, table_id: UUID) -> ClassificationSummary:
    rows = list(
        await session.scalars(
            select(MetadataColumn.classification).where(
                MetadataColumn.table_id == table_id,
                MetadataColumn.status == "ACTIVE",
            )
        )
    )
    classified = [value for value in rows if value and value != "UNCLASSIFIED"]
    return ClassificationSummary(
        total_columns=len(rows),
        classified_columns=len(classified),
        distinct_classifications=tuple(sorted(set(rows))),
        has_sensitive_classification=any(value in SENSITIVE_CLASSES for value in rows),
    )


async def _open_incident_counts(session: AsyncSession, table_id: UUID) -> tuple[int, int]:
    """(open_incident_count, open_critical_incident_count) -- same
    `severity == "CRITICAL"` + `status.in_(("OPEN", "ACKNOWLEDGED"))` filter
    `quality_coupling.py`/`context_product_policy.py`/`quality_api.py`
    already use for "does this have an open critical incident", the same
    `func.count().filter(...)` idiom `quality_api.py` already uses -- not a
    new predicate.
    """
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(DataQualityIncident.severity == "CRITICAL"),
            ).where(
                DataQualityIncident.table_id == table_id,
                DataQualityIncident.status.in_(_OPEN_INCIDENT_STATUSES),
            )
        )
    ).one()
    total, critical = row
    return int(total), int(critical)


async def compose_asset_context_signals(
    session: AsyncSession, table: MetadataTable, *, now: datetime | None = None
) -> AssetContextSignals:
    moment = now or datetime.now(UTC)
    table_ids = [table.id]

    assigned_owner = (await _earliest_active_owners(session, table_ids)).get(table.id)
    owner = assigned_owner
    owner_source: str | None = (
        "ownership_assignment (GL-2, ACTIVE)" if assigned_owner else None
    )
    documentation = (await _latest_approved_documentation(session, table_ids)).get(table.id)
    if owner is None and documentation is not None and documentation.owner_principal:
        owner = documentation.owner_principal
        owner_source = "asset_documentation_version.owner_principal (GL-9 fallback)"

    certification = (await _latest_certifications(session, table_ids)).get(table.id)
    certification_state, certification_expires_at = _certification_state(
        certification, now=moment
    )

    open_incident_ids = await _open_incident_table_ids(session, table_ids)
    latest_observation_at = await _latest_observation_at(session, table_ids)
    quality_state = _quality_state(
        table.id,
        open_incident_ids=open_incident_ids,
        latest_observation_at=latest_observation_at,
        now=moment,
    )
    open_incident_count, open_critical_incident_count = await _open_incident_counts(
        session, table.id
    )

    classification = await _classification_summary(session, table.id)

    return AssetContextSignals(
        owner=owner,
        owner_source=owner_source,
        certification_state=certification_state,
        certification_expires_at=certification_expires_at,
        quality_state=quality_state,
        open_incident_count=open_incident_count,
        has_open_critical_incident=open_critical_incident_count > 0,
        classification=classification,
    )
