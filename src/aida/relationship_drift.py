"""R11-FP06: an approved join whose evidence is gone stops being used until a person decides again.

`relationship_validation` records, with every approval, the validation the join rested on, and the
validation read reports how today's compares (UNCHANGED, CHANGED, CORROBORATION_LOST). Reporting
alone left a join approved on a key the source has since dropped feeding every consumer that reads
approved joins -- multi-table tool drafting, column-description evidence, impact analysis.

After a scan of a join's datasource finishes, this re-validates it once:

* **Evidence lost** -- corroboration lost, or a column it joins on gone -- suspends the join: it
  returns to PENDING, so every consumer (all of them read `status == "APPROVED"`) stops using it,
  and it is back in the review queue, where approval re-runs the same evidence gate. The approval
  it held is kept in `evidence["drift"]`.
* **Evidence back, exactly as approved** -- a suspended join whose validation fingerprint equals
  the one it was approved on -- is restored to APPROVED under its original approver: the person's
  decision stands on the same facts again.
* Anything else only moves the join's watermark (`updated_at`), so it is checked again after the
  next scan and not before. CHANGED stays approved: the validation read already reports it.

The suspension is a safety action, like a source-change hold, not a decision: it never approves
or rejects on anyone's behalf. Value-free: ids, codes and fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit
from aida.models import AnalysisRun, RelationshipCandidate, RelationshipCandidateGroup
from aida.relationship_validation import (
    RelationshipColumnsMissingError,
    RelationshipValidation,
    validate_composite_relationship_candidate,
    validate_relationship_candidate,
)
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

RELATIONSHIP_DRIFT_PRINCIPAL: Final = "scheduler:relationship-drift"
DRIFT_CORROBORATION_LOST: Final = "CORROBORATION_LOST"
DRIFT_COLUMNS_MISSING: Final = "COLUMNS_MISSING"
SUSPENSION_REASON: Final = (
    "The evidence this join was approved on is gone from the source, so it is no longer used. "
    "Approve it again once its key or columns are back, or reject it."
)
DEFAULT_CHECK_LIMIT: Final = 200

_Relationship = RelationshipCandidate | RelationshipCandidateGroup


@dataclass(slots=True)
class DriftOutcome:
    checked: int = 0
    suspended: int = 0
    restored: int = 0
    failed: int = 0


def _context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id=RELATIONSHIP_DRIFT_PRINCIPAL,
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )


def _checkable(model: type[_Relationship]) -> ColumnElement[bool]:
    """Approved, or suspended here, and read by a scan that finished since it was last touched."""
    datasources = (
        [RelationshipCandidate.datasource_id, RelationshipCandidate.target_datasource_id]
        if model is RelationshipCandidate
        else [RelationshipCandidateGroup.datasource_id]
    )
    newer_scan = (
        select(AnalysisRun.id)
        .where(
            AnalysisRun.datasource_id.in_(datasources),
            AnalysisRun.status == "COMPLETED",
            AnalysisRun.updated_at > model.updated_at,
        )
        .exists()
    )
    suspended = and_(model.status == "PENDING", model.reviewed_by == RELATIONSHIP_DRIFT_PRINCIPAL)
    return and_(or_(model.status == "APPROVED", suspended), newer_scan)


async def relationship_drift_pending(session: AsyncSession) -> list[UUID]:
    """Organizations with an approved or suspended join that a newer scan has read."""
    single = await session.scalars(
        select(RelationshipCandidate.organization_id)
        .where(_checkable(RelationshipCandidate))
        .distinct()
    )
    composite = await session.scalars(
        select(RelationshipCandidateGroup.organization_id)
        .where(_checkable(RelationshipCandidateGroup))
        .distinct()
    )
    return sorted(set(single) | set(composite), key=str)


async def _validate(session: AsyncSession, row: _Relationship) -> RelationshipValidation | None:
    """Today's validation, or `None` when a column the join names is gone."""
    try:
        if isinstance(row, RelationshipCandidate):
            return await validate_relationship_candidate(session, row)
        return await validate_composite_relationship_candidate(session, row)
    except RelationshipColumnsMissingError:
        return None


def _resource_type(row: _Relationship) -> str:
    return (
        "relationship_candidate"
        if isinstance(row, RelationshipCandidate)
        else "relationship_candidate_group"
    )


def _suspend(row: _Relationship, state: str, fingerprint: str | None, now: datetime) -> None:
    recorded = (row.evidence or {}).get("validation") or {}
    row.evidence = {
        **(row.evidence or {}),
        "drift": {
            "state": state,
            "detected_at": now.isoformat(),
            "current_fingerprint": fingerprint,
            "approved_fingerprint": recorded.get("fingerprint"),
            "approved_by": row.reviewed_by,
            "approved_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "approved_reason": row.review_reason,
        },
    }
    row.status = "PENDING"
    row.reviewed_by = RELATIONSHIP_DRIFT_PRINCIPAL
    row.review_reason = SUSPENSION_REASON
    row.reviewed_at = now


def _restore(row: _Relationship, drift: dict[str, Any], now: datetime) -> None:
    evidence = {key: value for key, value in (row.evidence or {}).items() if key != "drift"}
    row.evidence = evidence
    row.status = "APPROVED"
    row.reviewed_by = drift.get("approved_by")
    row.review_reason = drift.get("approved_reason")
    approved_at = drift.get("approved_at")
    row.reviewed_at = datetime.fromisoformat(approved_at) if approved_at else now


async def _check_one(
    session: AsyncSession, row: _Relationship, context: SecurityContext, now: datetime
) -> str:
    validation = await _validate(session, row)
    action = "CHECKED"
    if row.status == "APPROVED":
        if validation is None:
            _suspend(row, DRIFT_COLUMNS_MISSING, None, now)
            action = "SUSPENDED"
        else:
            recorded = (row.evidence or {}).get("validation") or {}
            if recorded.get("fingerprint") and not validation.approvable:
                _suspend(row, DRIFT_CORROBORATION_LOST, validation.fingerprint, now)
                action = "SUSPENDED"
    else:
        drift = (row.evidence or {}).get("drift") or {}
        if (
            validation is not None
            and validation.approvable
            and drift.get("approved_fingerprint")
            and validation.fingerprint == drift.get("approved_fingerprint")
            and drift.get("approved_by")
        ):
            _restore(row, drift, now)
            action = "RESTORED"
    # The watermark: checked again only after a scan that finishes after this one.
    row.updated_at = now
    if action != "CHECKED":
        record_audit(
            session,
            context,
            action=f"relationship_candidate.{action.lower()}",
            resource_type=_resource_type(row),
            resource_id=str(row.id),
            outcome="SUCCESS",
            correlation_id=str(row.organization_id),
            details={
                "state": ((row.evidence or {}).get("drift") or {}).get("state"),
                "fingerprint": validation.fingerprint if validation is not None else None,
            },
        )
    return action


async def check_relationship_drift(
    session: AsyncSession,
    organization_id: UUID,
    *,
    now: datetime,
    limit: int = DEFAULT_CHECK_LIMIT,
) -> DriftOutcome:
    """Re-validate the organization's checkable joins, at most `limit`. The caller commits."""
    context = _context(organization_id)
    outcome = DriftOutcome()
    targets: list[tuple[type[_Relationship], UUID]] = []
    for model in (RelationshipCandidate, RelationshipCandidateGroup):
        remaining = limit - len(targets)
        if remaining <= 0:
            break
        ids = await session.scalars(
            select(model.id)
            .where(model.organization_id == organization_id, _checkable(model))
            .order_by(model.updated_at)
            .limit(remaining)
        )
        targets.extend((model, row_id) for row_id in ids)
    for model, row_id in targets:
        try:
            async with session.begin_nested():
                row = await session.get(model, row_id, populate_existing=True)
                if not isinstance(row, RelationshipCandidate | RelationshipCandidateGroup):
                    continue
                action = await _check_one(session, row, context, now)
        except Exception:  # noqa: BLE001 -- one join must not stop the pass
            logger.exception("relationship_drift_check_failed", relationship_id=str(row_id))
            outcome.failed += 1
            continue
        outcome.checked += 1
        if action == "SUSPENDED":
            outcome.suspended += 1
        elif action == "RESTORED":
            outcome.restored += 1
    return outcome
