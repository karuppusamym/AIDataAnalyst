"""Deterministic per-read policy evaluation for governed Context Products."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    ContextProductConsumptionEdge,
    ContextProductVersion,
    DataQualityIncident,
    DataQualityObservation,
)

# AT-7(a)/AT-D1 -- a version that has been replaced by a newer PUBLISHED
# version no longer disappears the instant the new one is approved. It spends
# a support window as SUPPORTED (still readable by a version-pinned
# consumer); discovery keeps surfacing only the current PUBLISHED version.
# Statuses a version-pinned read can ever still serve content for.
_LIVE_READ_STATUSES = frozenset({"PUBLISHED", "SUPPORTED"})
# Statuses that represent "this version existed, and is now gone" -- distinct
# from DRAFT/REVIEW_REQUIRED/REJECTED/DEPRECATION_REVIEW, which never served
# content at all and stay indistinguishable from "never existed" (INV: MCP-3's
# anti-enumeration property).
_RETIRED_STATUSES = frozenset({"SUPERSEDED", "DEPRECATED"})


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


@dataclass(frozen=True, slots=True)
class ContextProductPurposeDecision:
    allowed: bool
    reason: str
    requested_purpose: str | None
    allowed_purposes: tuple[str, ...]

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_context_product_purpose(
    requested_purpose: str | None,
    policy_summary: dict[str, Any],
) -> ContextProductPurposeDecision:
    """Apply exact, normalized purpose ABAC when a product declares allowed purposes."""
    configured = policy_summary.get("allowed_purposes", [])
    allowed_purposes = tuple(
        sorted(
            {
                str(value).strip().casefold()
                for value in configured
                if isinstance(value, str) and value.strip()
            }
        )
    )
    normalized = requested_purpose.strip().casefold() if requested_purpose else None
    if not allowed_purposes:
        return ContextProductPurposeDecision(True, "NOT_RESTRICTED", normalized, ())
    if normalized is None:
        return ContextProductPurposeDecision(
            False, "BUSINESS_PURPOSE_REQUIRED", None, allowed_purposes
        )
    if normalized not in allowed_purposes:
        return ContextProductPurposeDecision(
            False, "BUSINESS_PURPOSE_NOT_ALLOWED", normalized, allowed_purposes
        )
    return ContextProductPurposeDecision(True, "ALLOWED", normalized, allowed_purposes)


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


# --- AT-7(a)/AT-D1: support window and distinguishable retirement -----------


def is_within_support_window(
    version: ContextProductVersion, *, now: datetime | None = None
) -> bool:
    """Whether a SUPPORTED version is still inside its support window.

    `support_window_ends_at` unset on a SUPPORTED version means "supported
    until explicit retirement" (an indefinite window), not "already expired".
    Any other status is never "within a support window" by this function --
    PUBLISHED is checked separately by the caller since it has no window at
    all (it is simply current).
    """
    if version.status != "SUPPORTED":
        return False
    if version.support_window_ends_at is None:
        return True
    current = now or datetime.now(UTC)
    return current < version.support_window_ends_at


def can_serve_pinned_version(
    version: ContextProductVersion, *, now: datetime | None = None
) -> bool:
    """Whether a version-pinned read may still be served full content.

    True for PUBLISHED (the current version) and for SUPPORTED while its
    support window has not elapsed. False for everything else, including a
    SUPPORTED version whose window has passed -- that version is effectively
    retired even though its stored `status` has not been swept to SUPERSEDED
    yet (no scheduler mutates it; retirement is evaluated live on every read
    so correctness never depends on a sweep having run).
    """
    if version.status == "PUBLISHED":
        return True
    return is_within_support_window(version, now=now)


def is_version_retired(version: ContextProductVersion, *, now: datetime | None = None) -> bool:
    """Whether a version is retired: explicitly SUPERSEDED/DEPRECATED, or a
    SUPPORTED version whose support window has elapsed. DRAFT,
    REVIEW_REQUIRED, and REJECTED versions are never "retired" -- they never
    served content, so they stay indistinguishable from "never existed".
    """
    if version.status in _RETIRED_STATUSES:
        return True
    if version.status == "SUPPORTED":
        return not is_within_support_window(version, now=now)
    return False


async def was_previously_authorized_consumer(
    session: AsyncSession, *, version_id: UUID, principal_id: str
) -> bool:
    """The subtle half of AT-7(a): a retired version must read as
    distinguishably "retired, upgrade" to a caller who was genuinely
    authorized for *this exact version* at some point -- proven by an actual
    prior successful consumption edge, not merely by a role that happens to
    match today -- while staying identical to the anti-enumeration "not
    found" for anyone who never actually read it (whether or not their
    current role would be eligible). A role match alone is not proof: a
    caller who was always role-eligible but never once actually consumed this
    version could otherwise fish version numbers to learn which ones used to
    exist. Only a real, recorded prior read counts as "was authorized".
    """
    row_id = await session.scalar(
        select(ContextProductConsumptionEdge.id)
        .where(
            ContextProductConsumptionEdge.context_product_version_id == version_id,
            ContextProductConsumptionEdge.principal_id == principal_id,
            ContextProductConsumptionEdge.policy_decision == "ALLOW",
        )
        .limit(1)
    )
    return row_id is not None


async def current_published_version_number(
    session: AsyncSession, product_id: UUID
) -> int | None:
    """The version number a retirement signal should point a consumer to
    upgrade toward -- `None` if the product currently has no PUBLISHED
    version (e.g. mid-review, or the product itself was retired)."""
    current_version: int | None = await session.scalar(
        select(ContextProductVersion.version).where(
            ContextProductVersion.product_id == product_id,
            ContextProductVersion.status == "PUBLISHED",
        )
    )
    return current_version
