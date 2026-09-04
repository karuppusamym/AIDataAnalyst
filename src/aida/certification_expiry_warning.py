"""P2-08: warn a certification's owner N days before it silently expires.

An ``AssetCertification`` whose ``expires_at`` slips past ``now`` starts reading
back as ``EXPIRED`` from the query-time projection in
``atlas.modules.catalog.service._certification_state`` (the row itself is
retained evidence, never mutated by a clock, per ``AssetCertification``'s own
docstring). Before P2-08 that transition was silent: the certification's
``certified_by`` steward learned about it only when a downstream policy
decision started returning ``ALLOWED_WITH_CAUTION`` on a table that had been
freshly CERTIFIED the day before, which is exactly the "supervised handover"
gap the audit called out.

This module runs a daily sweep that finds every ACTIVE certification expiring
inside the next ``warn_days`` (default 7) and, for each, records an audit +
outbox event addressed to ``certified_by`` and stamps
``expiry_warning_emitted_at`` so the same cert does not warn twice inside one
cycle. Delivery of the notification to a human channel happens through the
existing outbox publisher; this module's job ends at ``record_outbox`` (same
posture as ``quality_coupling.expire_sustained_incident_certifications`` for
the DQ-3 expiry path -- an audit row plus an outbox event, no direct
notification-engine call inside the sweep).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.events import record_audit, record_outbox
from aida.models import AssetCertification
from aida.security import SecurityContext


@dataclass(frozen=True, slots=True)
class CertificationExpiryWarning:
    """One warning the sweep decided to emit (returned for logging/tests)."""

    certification_id: UUID
    table_id: UUID
    column_id: UUID | None
    asset_type: str
    certified_by: str
    expires_at: datetime
    days_until_expiry: int


def _system_context(organization_id: UUID) -> SecurityContext:
    """The audit-writer identity for the scheduler-driven warning sweep.

    Mirrors the ``fleet-scheduler`` principal
    ``workflows.scheduler.reconcile_cancellation_requests`` already uses for
    its own scheduler-side audit rows: a stable, non-human principal so audit
    consumers can distinguish scheduler activity from user-driven writes.
    """
    return SecurityContext(
        principal_id="fleet-scheduler",
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )


async def warn_upcoming_certification_expiries(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    warn_days: int = 7,
) -> list[CertificationExpiryWarning]:
    """Emit an expiry warning for every ACTIVE cert expiring inside ``warn_days``.

    A cert warns at most once per cycle: ``expiry_warning_emitted_at`` is
    stamped when the warning is recorded, and a cert whose stamp is within
    ``warn_days * 2`` (doubled so a slow scheduler cadence never re-warns the
    same row twice in the same window) is skipped. Certs that have already
    expired (``expires_at <= now``) are out of scope -- the DQ-3 EXPIRED path
    or the query-time projection has already handled them; this sweep is
    strictly a *heads-up* on soon-to-expire live certifications.
    """
    effective_now = now or datetime.now(UTC)
    window_end = effective_now + timedelta(days=warn_days)
    cooldown_boundary = effective_now - timedelta(days=warn_days * 2)
    stmt = select(AssetCertification).where(
        AssetCertification.status == "ACTIVE",
        AssetCertification.expires_at > effective_now,
        AssetCertification.expires_at < window_end,
        or_(
            AssetCertification.expiry_warning_emitted_at.is_(None),
            AssetCertification.expiry_warning_emitted_at < cooldown_boundary,
        ),
    )
    rows = (await session.scalars(stmt)).all()
    emitted: list[CertificationExpiryWarning] = []
    for cert in rows:
        days_until = max(
            0, int((cert.expires_at - effective_now).total_seconds() // 86_400)
        )
        cert.expiry_warning_emitted_at = effective_now
        context = _system_context(cert.organization_id)
        record_audit(
            session,
            context,
            action="CERTIFICATION_EXPIRY_WARNING_SENT",
            resource_type="asset_certification",
            resource_id=str(cert.id),
            outcome="SUCCESS",
            correlation_id=str(cert.id),
            details={
                "table_id": str(cert.table_id),
                "column_id": str(cert.column_id) if cert.column_id else None,
                "asset_type": cert.asset_type,
                "certified_by": cert.certified_by,
                "expires_at": cert.expires_at.isoformat(),
                "days_until_expiry": days_until,
                "warn_days": warn_days,
            },
        )
        record_outbox(
            session,
            organization_id=cert.organization_id,
            aggregate_type="asset_certification",
            aggregate_id=str(cert.id),
            event_type="catalog.asset.certification_expiry_warning.v1",
            payload={
                "certification_id": str(cert.id),
                "table_id": str(cert.table_id),
                "column_id": str(cert.column_id) if cert.column_id else None,
                "asset_type": cert.asset_type,
                "certified_by": cert.certified_by,
                "expires_at": cert.expires_at.isoformat(),
                "days_until_expiry": days_until,
                "notify_principal": cert.certified_by,
            },
        )
        emitted.append(
            CertificationExpiryWarning(
                certification_id=cert.id,
                table_id=cert.table_id,
                column_id=cert.column_id,
                asset_type=cert.asset_type,
                certified_by=cert.certified_by,
                expires_at=cert.expires_at,
                days_until_expiry=days_until,
            )
        )
    if emitted:
        await session.flush()
    return emitted


# ---------------------------------------------------------------------------
# Scheduler entrypoint: rate-limited daily pass, same shape as
# `reaper_service.run_reaper_scheduler_pass`.
# ---------------------------------------------------------------------------

_last_run_at: datetime | None = None


def _due(last_run_at: datetime | None, now: datetime, interval: timedelta) -> bool:
    if last_run_at is None:
        return True
    return (now - last_run_at) >= interval


async def run_certification_expiry_warning_pass(
    settings: Settings, *, now: datetime | None = None
) -> list[CertificationExpiryWarning] | None:
    """Scheduler-facing entry: run a warning sweep if the configured cadence
    has elapsed since the last one, otherwise no-op.

    Returns ``None`` when the pass was skipped (not due), the list of emitted
    warnings when it ran -- same return shape as
    ``reaper_service.run_reaper_scheduler_pass``.
    """
    from aida.db import session_factory  # local import to avoid cycles at import

    global _last_run_at
    effective_now = now or datetime.now(UTC)
    interval = timedelta(seconds=settings.certification_expiry_warn_interval_seconds)
    if not _due(_last_run_at, effective_now, interval):
        return None
    async with session_factory() as session:
        emitted = await warn_upcoming_certification_expiries(
            session,
            now=effective_now,
            warn_days=settings.certification_expiry_warn_days,
        )
        await session.commit()
    _last_run_at = effective_now
    return emitted


def _reset_due_state_for_tests() -> None:
    """Test-only helper: clears the in-process cadence tracker."""
    global _last_run_at
    _last_run_at = None
