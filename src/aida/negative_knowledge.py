"""
Negative Knowledge Surface (Phase E - EE.3)
=============================================

Builds a queryable surface of "what we decided is not true": rejected
relationships, overridden classifications, resolved term conflicts, and
rejected inferences.  Re-proposal suppression prevents the system from
repeatedly suggesting something a human already dismissed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import NegativeAssertionRecord, utc_now

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

AssertionType = Literal[
    "RELATIONSHIP_REJECTED",
    "INFERENCE_REJECTED",
    "TERM_CONFLICT_RESOLVED",
    "CLASSIFICATION_OVERRIDDEN",
]


@dataclass(frozen=True, slots=True)
class NegativeAssertion:
    id: UUID | None
    assertion_type: AssertionType
    subject_id: str
    predicate: dict[str, Any]
    evidence: dict[str, Any]
    rejected_by: str
    rejected_at: datetime
    suppression_active: bool = True
    material_change_hash: str | None = None


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def compute_predicate_hash(predicate: dict[str, Any]) -> str:
    """Deterministic hash of a predicate for deduplication and change detection."""
    canonical = json.dumps(predicate, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


async def record_negative(
    session: AsyncSession,
    organization_id: UUID,
    assertion: NegativeAssertion,
) -> NegativeAssertionRecord:
    """Persist a new negative assertion."""
    material_hash = compute_predicate_hash(assertion.predicate)

    record = NegativeAssertionRecord(
        organization_id=organization_id,
        assertion_type=assertion.assertion_type,
        subject_id=assertion.subject_id,
        predicate=assertion.predicate,
        evidence=assertion.evidence,
        rejected_by=assertion.rejected_by,
        rejected_at=assertion.rejected_at,
        suppression_active=assertion.suppression_active,
        material_change_hash=material_hash,
    )
    session.add(record)
    return record


async def query_negatives(
    session: AsyncSession,
    organization_id: UUID,
    subject_id: str,
) -> list[NegativeAssertionRecord]:
    """Return all negative assertions for a given subject."""
    stmt = (
        select(NegativeAssertionRecord)
        .where(
            and_(
                NegativeAssertionRecord.organization_id == organization_id,
                NegativeAssertionRecord.subject_id == subject_id,
            )
        )
        .order_by(NegativeAssertionRecord.rejected_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def check_re_proposal(
    session: AsyncSession,
    organization_id: UUID,
    subject_id: str,
    predicate: dict[str, Any],
) -> NegativeAssertionRecord | None:
    """Check if a candidate was already rejected (active suppression).

    Returns the matching negative assertion if found with active suppression,
    or None if the candidate is safe to re-propose.
    """
    candidate_hash = compute_predicate_hash(predicate)

    stmt = (
        select(NegativeAssertionRecord)
        .where(
            and_(
                NegativeAssertionRecord.organization_id == organization_id,
                NegativeAssertionRecord.subject_id == subject_id,
                NegativeAssertionRecord.suppression_active.is_(True),
                NegativeAssertionRecord.material_change_hash == candidate_hash,
            )
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def lift_suppression(
    session: AsyncSession,
    organization_id: UUID,
    assertion_id: UUID,
    lifted_by: str,
    reason: str,
) -> NegativeAssertionRecord | None:
    """Manually lift suppression on a negative assertion.

    This allows re-proposal of previously rejected candidates, typically
    when material evidence has changed.
    """
    stmt = select(NegativeAssertionRecord).where(
        and_(
            NegativeAssertionRecord.id == assertion_id,
            NegativeAssertionRecord.organization_id == organization_id,
        )
    )
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        return None

    record.suppression_active = False
    record.suppression_lifted_at = datetime.now(UTC)
    record.suppression_lifted_by = lifted_by
    record.lift_reason = reason
    return record


async def search_negatives(
    session: AsyncSession,
    organization_id: UUID,
    assertion_type: str | None = None,
    suppression_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[NegativeAssertionRecord]:
    """Search negative assertions with optional filters."""
    stmt = (
        select(NegativeAssertionRecord)
        .where(NegativeAssertionRecord.organization_id == organization_id)
    )

    if assertion_type is not None:
        stmt = stmt.where(NegativeAssertionRecord.assertion_type == assertion_type)
    if suppression_active is not None:
        stmt = stmt.where(NegativeAssertionRecord.suppression_active.is_(suppression_active))

    stmt = stmt.order_by(NegativeAssertionRecord.rejected_at.desc()).offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def auto_lift_on_material_change(
    session: AsyncSession,
    organization_id: UUID,
    subject_id: str,
    new_predicate: dict[str, Any],
) -> list[NegativeAssertionRecord]:
    """Auto-lift suppression when evidence changes materially.

    Compares the new predicate hash against existing assertions and lifts
    suppression on any whose material_change_hash no longer matches,
    flagging them with a "previously rejected" marker.
    """
    new_hash = compute_predicate_hash(new_predicate)
    lifted: list[NegativeAssertionRecord] = []

    stmt = (
        select(NegativeAssertionRecord)
        .where(
            and_(
                NegativeAssertionRecord.organization_id == organization_id,
                NegativeAssertionRecord.subject_id == subject_id,
                NegativeAssertionRecord.suppression_active.is_(True),
                NegativeAssertionRecord.material_change_hash != new_hash,
            )
        )
    )
    result = await session.execute(stmt)
    for record in result.scalars().all():
        record.suppression_active = False
        record.suppression_lifted_at = datetime.now(UTC)
        record.suppression_lifted_by = "system:material_change"
        record.lift_reason = "material evidence changed"
        lifted.append(record)

    return lifted
