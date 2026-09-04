"""P2-07: warn an OwnershipAssignment's owner N days before it silently lapses,
and, on a longer grace horizon, flip a still-un-re-affirmed assignment
ACTIVE -> LAPSED (and route the subject through the unowned-asset backlog if
it just lost its last active owner).

Before P2-07 an ``OwnershipAssignment`` was claimed once and never re-
affirmed. The audit finding was that a bank whose head of retail-analytics
left three quarters ago still had every table they'd owned reading back as
"owned by <departed-principal>" for the platform's governed-tool decision
path -- silent stale-ownership. This module runs a daily sweep that:

1. Finds every ACTIVE assignment whose ``expires_at`` is inside
   ``now < expires_at < now + warn_days`` and stamps a one-per-cycle warning.
2. Finds every ACTIVE assignment whose ``expires_at + grace_days < now`` and
   flips it to LAPSED; if that was the subject's *last* ACTIVE owner, the
   subject is added to (or refreshed in) the unowned-asset backlog so
   ``route_unowned_asset_backlog`` picks it up on its next pass.

Mirrors ``aida.certification_expiry_warning`` deliberately closely -- same
system-context principal (``fleet-scheduler``), same cooldown shape, same
scheduler-entry pattern -- so P2-07 and P2-08 lift to the same operational
runbook.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit, record_outbox
from aida.models import OwnershipAssignment, UnownedAssetEscalation
from aida.security import SecurityContext


@dataclass(frozen=True, slots=True)
class OwnershipExpiryWarning:
    """One warning the sweep decided to emit (returned for logging/tests)."""

    assignment_id: UUID
    subject_type: str
    subject_id: str
    owner_type: str
    owner_principal: str
    expires_at: datetime
    days_until_expiry: int


@dataclass(frozen=True, slots=True)
class OwnershipLapse:
    """One assignment the expire sweep flipped ACTIVE -> LAPSED."""

    assignment_id: UUID
    organization_id: UUID
    subject_type: str
    subject_id: str
    owner_type: str
    owner_principal: str
    expires_at: datetime
    last_owner: bool


def _system_context(organization_id: UUID) -> SecurityContext:
    """The audit-writer identity for the scheduler-driven ownership sweeps.

    Same non-human ``fleet-scheduler`` principal
    ``certification_expiry_warning._system_context`` uses, so audit consumers
    keep the one filter that separates scheduler activity from user writes.
    """
    return SecurityContext(
        principal_id="fleet-scheduler",
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )


async def warn_upcoming_ownership_expiries(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    warn_days: int = 14,
) -> list[OwnershipExpiryWarning]:
    """Emit an expiry warning for every ACTIVE assignment expiring inside
    ``warn_days``.

    Rows with ``expires_at IS NULL`` are legacy (pre-P2-07) rows and are
    deliberately out of scope until they are re-affirmed or freshly assigned
    under P2-07 code -- exactly the "don't touch what wasn't opted in"
    posture the schema comment on ``OwnershipAssignment.expires_at`` calls
    for. Rows that have already passed their expiry are out of scope for this
    function (``expire_lapsed_ownership_assignments`` handles them).
    """
    effective_now = now or datetime.now(UTC)
    window_end = effective_now + timedelta(days=warn_days)
    cooldown_boundary = effective_now - timedelta(days=warn_days * 2)
    stmt = select(OwnershipAssignment).where(
        OwnershipAssignment.status == "ACTIVE",
        OwnershipAssignment.expires_at.is_not(None),
        OwnershipAssignment.expires_at > effective_now,
        OwnershipAssignment.expires_at < window_end,
        or_(
            OwnershipAssignment.expiry_warning_emitted_at.is_(None),
            OwnershipAssignment.expiry_warning_emitted_at < cooldown_boundary,
        ),
    )
    rows = (await session.scalars(stmt)).all()
    emitted: list[OwnershipExpiryWarning] = []
    for assignment in rows:
        expires_at = assignment.expires_at
        assert expires_at is not None  # narrowed by the filter above
        days_until = max(
            0, int((expires_at - effective_now).total_seconds() // 86_400)
        )
        assignment.expiry_warning_emitted_at = effective_now
        context = _system_context(assignment.organization_id)
        details = {
            "subject_type": assignment.subject_type,
            "subject_id": assignment.subject_id,
            "owner_type": assignment.owner_type,
            "owner_principal": assignment.owner_principal,
            "expires_at": expires_at.isoformat(),
            "days_until_expiry": days_until,
            "warn_days": warn_days,
        }
        record_audit(
            session,
            context,
            action="OWNERSHIP_EXPIRY_WARNING_SENT",
            resource_type="ownership_assignment",
            resource_id=str(assignment.id),
            outcome="SUCCESS",
            correlation_id=str(assignment.id),
            details=details,
        )
        record_outbox(
            session,
            organization_id=assignment.organization_id,
            aggregate_type="ownership_assignment",
            aggregate_id=str(assignment.id),
            event_type="ownership.assignment.expiry_warning.v1",
            payload={
                "assignment_id": str(assignment.id),
                "notify_principal": assignment.owner_principal,
                **details,
            },
        )
        emitted.append(
            OwnershipExpiryWarning(
                assignment_id=assignment.id,
                subject_type=assignment.subject_type,
                subject_id=assignment.subject_id,
                owner_type=assignment.owner_type,
                owner_principal=assignment.owner_principal,
                expires_at=expires_at,
                days_until_expiry=days_until,
            )
        )
    if emitted:
        await session.flush()
    return emitted


async def expire_lapsed_ownership_assignments(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    grace_days: int = 30,
) -> list[OwnershipLapse]:
    """Flip ACTIVE assignments whose ``expires_at + grace_days`` has passed
    to LAPSED, and, per assignment, add the subject to the unowned-asset
    backlog if it just lost its last ACTIVE owner.

    ``grace_days`` (default 30) is the pad past ``expires_at`` before the
    ACTIVE flip actually happens; a notified owner had ``warn_days`` +
    ``grace_days`` to reaffirm before ownership drops. Only ``LAPSED`` is
    written; ``LAPSED_LEAVER`` is the identity-lifecycle handler's
    responsibility and stays in that module.
    """
    effective_now = now or datetime.now(UTC)
    lapse_boundary = effective_now - timedelta(days=grace_days)
    stmt = select(OwnershipAssignment).where(
        OwnershipAssignment.status == "ACTIVE",
        OwnershipAssignment.expires_at.is_not(None),
        OwnershipAssignment.expires_at < lapse_boundary,
    )
    rows = (await session.scalars(stmt)).all()
    lapses: list[OwnershipLapse] = []
    for assignment in rows:
        expires_at = assignment.expires_at
        assert expires_at is not None
        assignment.status = "LAPSED"
        # Check whether any OTHER active owner remains on the same subject.
        # Uses `exists()` so it's a bounded per-row query, not a scan.
        remaining_stmt = select(
            exists().where(
                OwnershipAssignment.organization_id == assignment.organization_id,
                OwnershipAssignment.subject_type == assignment.subject_type,
                OwnershipAssignment.subject_id == assignment.subject_id,
                OwnershipAssignment.status == "ACTIVE",
                OwnershipAssignment.id != assignment.id,
            )
        )
        remaining = bool(await session.scalar(remaining_stmt))
        last_owner = not remaining
        context = _system_context(assignment.organization_id)
        details = {
            "subject_type": assignment.subject_type,
            "subject_id": assignment.subject_id,
            "owner_type": assignment.owner_type,
            "owner_principal": assignment.owner_principal,
            "expires_at": expires_at.isoformat(),
            "grace_days": grace_days,
            "last_owner": last_owner,
        }
        record_audit(
            session,
            context,
            action="OWNERSHIP_ASSIGNMENT_LAPSED",
            resource_type="ownership_assignment",
            resource_id=str(assignment.id),
            outcome="SUCCESS",
            correlation_id=str(assignment.id),
            details=details,
        )
        record_outbox(
            session,
            organization_id=assignment.organization_id,
            aggregate_type="ownership_assignment",
            aggregate_id=str(assignment.id),
            event_type="ownership.assignment.lapsed.v1",
            payload={
                "assignment_id": str(assignment.id),
                **details,
            },
        )
        # If the subject just lost its last ACTIVE owner *and* the subject is
        # a TABLE (the only kind the unowned-asset backlog covers today --
        # see `route_unowned_asset_backlog`), stage an escalation row so the
        # next `route_unowned_asset_backlog` pass picks it up. We insert only
        # if there isn't already an open escalation for the same table (the
        # partial uniqueness of the "one open escalation per subject" invariant
        # the routing engine itself enforces).
        if last_owner and assignment.subject_type == "TABLE":
            try:
                table_uuid = UUID(assignment.subject_id)
            except ValueError:
                table_uuid = None
            if table_uuid is not None:
                # `UnownedAssetEscalation` has a UNIQUE(table_id): at most one row
                # per table ever, resolved or not. If a row already exists (even
                # RESOLVED from a previous cycle) we do not insert a new one;
                # `route_unowned_asset_backlog` will reactivate it on its next
                # pass if the table is unowned again.
                already = await session.scalar(
                    select(UnownedAssetEscalation).where(
                        UnownedAssetEscalation.table_id == table_uuid,
                    )
                )
                if already is None:
                    session.add(
                        UnownedAssetEscalation(
                            organization_id=assignment.organization_id,
                            table_id=table_uuid,
                            status="PENDING",
                            first_detected_unowned_at=effective_now,
                        )
                    )
        lapses.append(
            OwnershipLapse(
                assignment_id=assignment.id,
                organization_id=assignment.organization_id,
                subject_type=assignment.subject_type,
                subject_id=assignment.subject_id,
                owner_type=assignment.owner_type,
                owner_principal=assignment.owner_principal,
                expires_at=expires_at,
                last_owner=last_owner,
            )
        )
    if lapses:
        await session.flush()
    return lapses


# ---------------------------------------------------------------------------
# Scheduler entrypoint: rate-limited daily pass, same shape as
# `certification_expiry_warning.run_certification_expiry_warning_pass`.
# ---------------------------------------------------------------------------

_last_run_at: datetime | None = None


def _due(last_run_at: datetime | None, now: datetime, interval: timedelta) -> bool:
    if last_run_at is None:
        return True
    return (now - last_run_at) >= interval


async def run_ownership_expiry_pass(
    settings, *, now: datetime | None = None
) -> tuple[list[OwnershipExpiryWarning], list[OwnershipLapse]] | None:
    """Scheduler-facing entry: run the warning sweep and then the expire
    sweep if the configured cadence has elapsed, otherwise no-op.

    Returns ``None`` when the pass was skipped, a ``(warnings, lapses)``
    tuple when it ran.
    """
    from aida.db import session_factory

    global _last_run_at
    effective_now = now or datetime.now(UTC)
    interval = timedelta(seconds=settings.ownership_expiry_warn_interval_seconds)
    if not _due(_last_run_at, effective_now, interval):
        return None
    async with session_factory() as session:
        warnings = await warn_upcoming_ownership_expiries(
            session,
            now=effective_now,
            warn_days=settings.ownership_expiry_warn_days,
        )
        lapses = await expire_lapsed_ownership_assignments(
            session,
            now=effective_now,
            grace_days=settings.ownership_expiry_grace_days,
        )
        await session.commit()
    _last_run_at = effective_now
    return warnings, lapses


def _reset_due_state_for_tests() -> None:
    """Test-only helper: clears the in-process cadence tracker."""
    global _last_run_at
    _last_run_at = None
