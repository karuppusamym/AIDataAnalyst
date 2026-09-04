"""P2-07: reconcile OwnershipAssignment when a principal is merged or deleted.

Before P2-07, deleting a principal in the identity service left every
``OwnershipAssignment`` row with that principal's id still ACTIVE -- the
"dangling ownership on user delete" audit finding. This module is the
handler for two identity events that make the flip a no-op decision instead
of a nightly manual reconcile:

* ``identity.principal.deleted.v1`` -- the principal was removed. Every
  ACTIVE assignment they held is flipped to ``LAPSED_LEAVER`` and, per
  subject that just lost its last active owner, added to the unowned-asset
  backlog so ``route_unowned_asset_backlog`` picks it up.
* ``identity.principal.merged.v1`` -- the principal was merged into a
  successor principal. Every ACTIVE assignment they held is updated in
  place (``owner_principal`` <- successor) with an audit trail, no lapse.

Both handlers are gated by ``settings.ownership_leaver_auto_reassign``.
In deployments that require every ownership flip to be a governed decision,
the flag turns the handler into a no-op and operators fall back to the GL-7
``REASSIGN_LEAVER`` operator flow.

The handler is called both by the outbox worker that consumes the two
event types AND directly by ``identity_events.emit_principal_deleted`` /
``emit_principal_merged`` (see that module for the emission side) so a
same-transaction delete gets same-transaction reassignment without a wait
for the outbox to drain -- the P2-08 pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit, record_outbox
from aida.models import OwnershipAssignment, UnownedAssetEscalation
from aida.security import SecurityContext


@dataclass(frozen=True, slots=True)
class PrincipalLeaverResult:
    """Outcome of a principal-deleted or principal-merged reconcile pass."""

    principal_id: str
    lapsed_assignment_ids: tuple[UUID, ...]
    reassigned_assignment_ids: tuple[UUID, ...]
    subjects_now_unowned: tuple[tuple[str, str], ...]  # (subject_type, subject_id)


def _system_context(organization_id: UUID | None) -> SecurityContext:
    return SecurityContext(
        principal_id="fleet-scheduler",
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )


async def handle_principal_deleted(
    session: AsyncSession,
    *,
    settings,
    principal_id: str,
    organization_id: UUID | None = None,
    now: datetime | None = None,
) -> PrincipalLeaverResult:
    """Flip every ACTIVE assignment owned by ``principal_id`` to
    ``LAPSED_LEAVER``. Config-gated by ``ownership_leaver_auto_reassign`` --
    when off, this returns an empty result and touches nothing.

    ``organization_id`` narrows the scan to a single tenant; omit to
    reconcile every tenant this principal appears in (identity is a cross-
    tenant concern -- see ADR-0018 three-axis tenancy).
    """
    if not settings.ownership_leaver_auto_reassign:
        return PrincipalLeaverResult(
            principal_id=principal_id,
            lapsed_assignment_ids=(),
            reassigned_assignment_ids=(),
            subjects_now_unowned=(),
        )
    effective_now = now or datetime.now(UTC)
    stmt = select(OwnershipAssignment).where(
        OwnershipAssignment.owner_principal == principal_id,
        OwnershipAssignment.status == "ACTIVE",
    )
    if organization_id is not None:
        stmt = stmt.where(OwnershipAssignment.organization_id == organization_id)
    rows = (await session.scalars(stmt)).all()
    lapsed: list[UUID] = []
    unowned_subjects: list[tuple[str, str]] = []
    for assignment in rows:
        assignment.status = "LAPSED_LEAVER"
        lapsed.append(assignment.id)
        # "Was this the subject's last ACTIVE owner?" -- if yes, route it.
        remaining = bool(
            await session.scalar(
                select(
                    exists().where(
                        OwnershipAssignment.organization_id
                        == assignment.organization_id,
                        OwnershipAssignment.subject_type
                        == assignment.subject_type,
                        OwnershipAssignment.subject_id == assignment.subject_id,
                        OwnershipAssignment.status == "ACTIVE",
                        OwnershipAssignment.id != assignment.id,
                    )
                )
            )
        )
        last_owner = not remaining
        details = {
            "subject_type": assignment.subject_type,
            "subject_id": assignment.subject_id,
            "owner_type": assignment.owner_type,
            "deleted_principal_id": principal_id,
            "last_owner": last_owner,
            "trigger_event": "identity.principal.deleted.v1",
        }
        record_audit(
            session,
            _system_context(assignment.organization_id),
            action="OWNERSHIP_AUTO_REASSIGNED_LEAVER",
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
            event_type="ownership.assignment.lapsed_leaver.v1",
            payload={"assignment_id": str(assignment.id), **details},
        )
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
            unowned_subjects.append(
                (assignment.subject_type, assignment.subject_id)
            )
        elif last_owner:
            unowned_subjects.append(
                (assignment.subject_type, assignment.subject_id)
            )
    if lapsed:
        await session.flush()
    return PrincipalLeaverResult(
        principal_id=principal_id,
        lapsed_assignment_ids=tuple(lapsed),
        reassigned_assignment_ids=(),
        subjects_now_unowned=tuple(unowned_subjects),
    )


async def handle_principal_merged(
    session: AsyncSession,
    *,
    settings,
    from_principal_id: str,
    into_principal_id: str,
    organization_id: UUID | None = None,
    now: datetime | None = None,
) -> PrincipalLeaverResult:
    """Redirect every ACTIVE assignment held by ``from_principal_id`` onto
    ``into_principal_id``.

    If the successor already has an ACTIVE assignment on the same
    ``(subject_type, subject_id, owner_type)`` the unique constraint would
    reject the duplicate -- in that case the losing row is flipped to
    ``LAPSED_LEAVER`` (successor already covers the subject; no dangling
    row, no unique-constraint violation).
    """
    if not settings.ownership_leaver_auto_reassign:
        return PrincipalLeaverResult(
            principal_id=from_principal_id,
            lapsed_assignment_ids=(),
            reassigned_assignment_ids=(),
            subjects_now_unowned=(),
        )
    effective_now = now or datetime.now(UTC)
    del effective_now  # merged rows have no `expires_at` update; kept for parity
    stmt = select(OwnershipAssignment).where(
        OwnershipAssignment.owner_principal == from_principal_id,
        OwnershipAssignment.status == "ACTIVE",
    )
    if organization_id is not None:
        stmt = stmt.where(OwnershipAssignment.organization_id == organization_id)
    rows = (await session.scalars(stmt)).all()
    reassigned: list[UUID] = []
    lapsed: list[UUID] = []
    for assignment in rows:
        # Does the successor already own this exact tuple? Then flip the
        # losing row to LAPSED_LEAVER instead of duplicating.
        clash = await session.scalar(
            select(OwnershipAssignment).where(
                OwnershipAssignment.organization_id == assignment.organization_id,
                OwnershipAssignment.subject_type == assignment.subject_type,
                OwnershipAssignment.subject_id == assignment.subject_id,
                OwnershipAssignment.owner_type == assignment.owner_type,
                OwnershipAssignment.owner_principal == into_principal_id,
                OwnershipAssignment.status == "ACTIVE",
            )
        )
        if clash is not None:
            assignment.status = "LAPSED_LEAVER"
            lapsed.append(assignment.id)
            details = {
                "subject_type": assignment.subject_type,
                "subject_id": assignment.subject_id,
                "from_principal_id": from_principal_id,
                "into_principal_id": into_principal_id,
                "outcome_reason": "successor_already_owns_subject",
                "trigger_event": "identity.principal.merged.v1",
            }
        else:
            assignment.owner_principal = into_principal_id
            assignment.assignment_kind = "MERGED"
            reassigned.append(assignment.id)
            details = {
                "subject_type": assignment.subject_type,
                "subject_id": assignment.subject_id,
                "from_principal_id": from_principal_id,
                "into_principal_id": into_principal_id,
                "trigger_event": "identity.principal.merged.v1",
            }
        record_audit(
            session,
            _system_context(assignment.organization_id),
            action="OWNERSHIP_AUTO_REASSIGNED_LEAVER",
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
            event_type="ownership.assignment.merged.v1",
            payload={"assignment_id": str(assignment.id), **details},
        )
    if reassigned or lapsed:
        await session.flush()
    return PrincipalLeaverResult(
        principal_id=from_principal_id,
        lapsed_assignment_ids=tuple(lapsed),
        reassigned_assignment_ids=tuple(reassigned),
        subjects_now_unowned=(),
    )
