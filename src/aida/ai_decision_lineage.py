"""First-class AI decision edges for the lineage graph.

Records every AI decision -- retrieval selections, retrieval rejections,
tool selections, tool rejections, and refusals -- as an explicit edge in
the lineage graph.  Rejection reasons are *always* recorded, not just
selections.  Refusals record which control fired.

All edges are value-free: no source data values appear in the edge
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import AiDecisionRecord

DECISION_LINEAGE_VERSION = "ai-decision-lineage-v1"


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

DecisionType = Literal[
    "RETRIEVAL_SELECTED",
    "RETRIEVAL_REJECTED",
    "TOOL_SELECTED",
    "TOOL_REJECTED",
    "REFUSAL",
]


@dataclass(frozen=True, slots=True)
class AiDecisionEdge:
    """One AI decision edge in the lineage graph."""

    run_id: UUID
    decision_type: DecisionType
    source_node: str
    target_node: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    control_version: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def record_decision(
    session: AsyncSession,
    organization_id: UUID,
    edge: AiDecisionEdge,
) -> UUID:
    """Persist a single AI decision edge.  Returns the record id."""
    record = AiDecisionRecord(
        organization_id=organization_id,
        run_id=edge.run_id,
        decision_type=edge.decision_type,
        source_node=edge.source_node,
        target_node=edge.target_node,
        reason=edge.reason,
        evidence=edge.evidence,
        control_version=edge.control_version,
        decided_at=edge.timestamp,
    )
    session.add(record)
    return record.id


def record_decisions(
    session: AsyncSession,
    organization_id: UUID,
    edges: list[AiDecisionEdge],
) -> list[UUID]:
    """Persist multiple decision edges in one batch."""
    return [record_decision(session, organization_id, edge) for edge in edges]


async def get_decisions_for_run(
    session: AsyncSession,
    run_id: UUID,
) -> list[AiDecisionRecord]:
    """Retrieve all decision edges for a given run."""
    stmt = (
        select(AiDecisionRecord)
        .where(AiDecisionRecord.run_id == run_id)
        .order_by(AiDecisionRecord.decided_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_decisions_for_asset(
    session: AsyncSession,
    asset_id: str,
    limit: int = 200,
) -> list[AiDecisionRecord]:
    """Retrieve decision edges involving a specific asset (as target_node)."""
    stmt = (
        select(AiDecisionRecord)
        .where(AiDecisionRecord.target_node == asset_id)
        .order_by(AiDecisionRecord.decided_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_refusals(
    session: AsyncSession,
    organization_id: UUID,
    limit: int = 200,
    offset: int = 0,
) -> list[AiDecisionRecord]:
    """Retrieve all refusal decisions for audit."""
    stmt = (
        select(AiDecisionRecord)
        .where(
            AiDecisionRecord.organization_id == organization_id,
            AiDecisionRecord.decision_type == "REFUSAL",
        )
        .order_by(AiDecisionRecord.decided_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())
