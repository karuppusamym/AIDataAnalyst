"""Deterministic per-read policy evaluation for governed Context Products."""

from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import DataQualityIncident, DataQualityObservation


@dataclass(frozen=True, slots=True)
class ContextProductQualityDecision:
    allowed: bool
    reasons: tuple[str, ...]
    required_minimum_score: int
    referenced_table_count: int
    observed_table_count: int
    lowest_score: int | None
    active_critical_incident_count: int

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


def evaluate_context_product_quality(
    *,
    table_ids: list[UUID],
    minimum_score: int,
    deny_on_critical_incident: bool,
    latest_scores: dict[UUID, int],
    critical_incident_table_ids: set[UUID],
) -> ContextProductQualityDecision:
    """Fail closed when required quality evidence is missing or below policy."""
    expected = set(table_ids)
    relevant_scores = {
        table_id: score for table_id, score in latest_scores.items() if table_id in expected
    }
    relevant_incidents = critical_incident_table_ids & expected
    reasons: list[str] = []
    if minimum_score > 0:
        missing = expected - set(relevant_scores)
        if missing:
            reasons.append("MISSING_QUALITY_EVIDENCE")
        if any(score < minimum_score for score in relevant_scores.values()):
            reasons.append("QUALITY_SCORE_BELOW_MINIMUM")
    if deny_on_critical_incident and relevant_incidents:
        reasons.append("ACTIVE_CRITICAL_INCIDENT")
    return ContextProductQualityDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        required_minimum_score=minimum_score,
        referenced_table_count=len(expected),
        observed_table_count=len(relevant_scores),
        lowest_score=min(relevant_scores.values(), default=None),
        active_critical_incident_count=len(relevant_incidents),
    )


async def evaluate_context_product_quality_from_db(
    session: AsyncSession,
    *,
    organization_id: UUID,
    table_id_values: list[str],
    requirements: dict[str, Any],
) -> ContextProductQualityDecision:
    table_ids: list[UUID] = []
    for value in table_id_values:
        try:
            table_ids.append(UUID(str(value)))
        except ValueError:
            return ContextProductQualityDecision(
                allowed=False,
                reasons=("INVALID_TABLE_REFERENCE",),
                required_minimum_score=int(requirements.get("minimum_score", 0)),
                referenced_table_count=len(table_id_values),
                observed_table_count=0,
                lowest_score=None,
                active_critical_incident_count=0,
            )
    if not table_ids:
        return evaluate_context_product_quality(
            table_ids=[],
            minimum_score=int(requirements.get("minimum_score", 0)),
            deny_on_critical_incident=bool(
                requirements.get("deny_on_critical_incident", True)
            ),
            latest_scores={},
            critical_incident_table_ids=set(),
        )

    ranked = (
        select(
            DataQualityObservation.table_id.label("table_id"),
            DataQualityObservation.quality_score.label("quality_score"),
            func.row_number()
            .over(
                partition_by=DataQualityObservation.table_id,
                order_by=DataQualityObservation.created_at.desc(),
            )
            .label("position"),
        )
        .where(
            DataQualityObservation.organization_id == organization_id,
            DataQualityObservation.table_id.in_(table_ids),
        )
        .subquery()
    )
    score_rows = (
        await session.execute(
            select(ranked.c.table_id, ranked.c.quality_score).where(ranked.c.position == 1)
        )
    ).all()
    critical_table_ids = set(
        (
            await session.scalars(
                select(DataQualityIncident.table_id)
                .where(
                    DataQualityIncident.organization_id == organization_id,
                    DataQualityIncident.table_id.in_(table_ids),
                    DataQualityIncident.severity == "CRITICAL",
                    DataQualityIncident.status.in_(("OPEN", "ACKNOWLEDGED")),
                )
                .distinct()
            )
        ).all()
    )
    return evaluate_context_product_quality(
        table_ids=table_ids,
        minimum_score=int(requirements.get("minimum_score", 0)),
        deny_on_critical_incident=bool(requirements.get("deny_on_critical_incident", True)),
        latest_scores={table_id: score for table_id, score in score_rows},
        critical_incident_table_ids=critical_table_ids,
    )
