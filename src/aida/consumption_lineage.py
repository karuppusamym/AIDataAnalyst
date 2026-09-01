"""
Consumption-as-lineage service (CX-4).

Records and queries consumption edges that capture who consumed what resource,
when, through which channel, and under which policy decision.  These edges
form a runtime lineage graph that complements the compile-time lineage
extracted from SQL, dbt, and OpenLineage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import ConsumptionRecord


@dataclass(frozen=True, slots=True)
class ConsumptionEdge:
    """Immutable value object representing a single consumption event."""

    consumer_id: str
    consumer_type: str
    resource_type: str
    resource_id: str
    channel: str
    correlation_id: str
    policy_decision: str
    business_purpose: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def record_consumption(
    session: AsyncSession,
    *,
    organization_id: UUID,
    edge: ConsumptionEdge,
) -> ConsumptionRecord:
    """Persist a consumption lineage edge and return the created record."""
    record = ConsumptionRecord(
        organization_id=organization_id,
        consumer_id=edge.consumer_id,
        consumer_type=edge.consumer_type,
        resource_type=edge.resource_type,
        resource_id=edge.resource_id,
        channel=edge.channel,
        correlation_id=edge.correlation_id,
        policy_decision=edge.policy_decision,
        business_purpose=edge.business_purpose,
        details=edge.details or {},
    )
    session.add(record)
    return record


async def get_consumption_for_resource(
    session: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: str,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ConsumptionRecord], int]:
    """Return consumption edges for a specific resource, newest first."""
    base = select(ConsumptionRecord).where(
        ConsumptionRecord.organization_id == organization_id,
        ConsumptionRecord.resource_type == resource_type,
        ConsumptionRecord.resource_id == resource_id,
    )
    total_result = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result or 0
    rows = (
        await session.scalars(
            base.order_by(ConsumptionRecord.consumed_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total


async def get_consumption_by_consumer(
    session: AsyncSession,
    *,
    organization_id: UUID,
    consumer_id: str,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ConsumptionRecord], int]:
    """Return consumption edges for a specific consumer, newest first."""
    base = select(ConsumptionRecord).where(
        ConsumptionRecord.organization_id == organization_id,
        ConsumptionRecord.consumer_id == consumer_id,
    )
    total_result = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result or 0
    rows = (
        await session.scalars(
            base.order_by(ConsumptionRecord.consumed_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total


async def get_consumption_by_resource_counts(
    session: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    limit: int = 500,
) -> list[tuple[str, int, datetime]]:
    """AT-5: the top ``limit`` resources of ``resource_type`` by consumption-read
    count, aggregated in one grouped query rather than loaded row-by-row.

    Returns ``(resource_id, count, last_consumed_at)`` tuples, count
    descending. Bounding by ``limit`` (rather than a time window) keeps this
    a single indexed aggregate query regardless of how much consumption
    history an organization has accumulated -- the same
    ``ix_consumption_record_resource`` index `get_consumption_for_resource`
    already relies on covers ``(organization_id, resource_type, resource_id,
    consumed_at)``, so grouping by ``resource_id`` within that prefix stays
    an index-driven aggregation rather than a full-table scan.
    """
    rows = (
        await session.execute(
            select(
                ConsumptionRecord.resource_id,
                func.count().label("consumption_count"),
                func.max(ConsumptionRecord.consumed_at).label("last_consumed_at"),
            )
            .where(
                ConsumptionRecord.organization_id == organization_id,
                ConsumptionRecord.resource_type == resource_type,
            )
            .group_by(ConsumptionRecord.resource_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return [(resource_id, count, last_consumed_at) for resource_id, count, last_consumed_at in rows]


async def get_consumption_graph(
    session: AsyncSession,
    *,
    organization_id: UUID,
    limit: int = 500,
    offset: int = 0,
) -> tuple[list[ConsumptionRecord], int]:
    """Return all consumption edges for an organization, newest first."""
    base = select(ConsumptionRecord).where(
        ConsumptionRecord.organization_id == organization_id,
    )
    total_result = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result or 0
    rows = (
        await session.scalars(
            base.order_by(ConsumptionRecord.consumed_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total
