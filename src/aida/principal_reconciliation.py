"""F19: the production trigger for principal-leaver ownership reconciliation.

``ownership_principal_lifecycle`` contained real, tested reconciliation logic
and ``identity_events`` contained the emitters that call it -- and no process
that runs in production could reach either. The review's word for that was
"unfinished integration, not dead code", and this module is the missing half:
a scheduled pass on the fleet scheduler (`aida.workflows.scheduler`) that
replays ``identity.principal.deleted.v1`` / ``identity.principal.merged.v1``
outbox rows through the existing handlers.

Why replay the outbox rather than add a new inbox table
-------------------------------------------------------
The two emitters already write those rows in the same transaction as the
identity change; they are the durable record of "a principal left". A separate
consumer-offset table would add schema for a guarantee the data already
provides, and would be a second thing that can be wrong about what has been
processed. What this pass needs instead is for reprocessing to be harmless --
see below.

Replay safety
-------------
Idempotence here is a property of the handlers, not of bookkeeping this module
does. ``handle_principal_deleted`` selects assignments that are ACTIVE *and*
still owned by the departed principal; after the first application there are
none, so a replay writes no audit row, no outbox row, and mutates nothing.
``handle_principal_merged`` is the same shape: once the rows carry the
successor's principal id, a replay of the same merge selects an empty set.
That is why this pass can be scheduled at all -- it will re-see the same events
on the next cycle by construction, and re-seeing them has to be free.

The lookback window and the in-process cadence tracker are therefore
efficiency, not correctness: they bound how much of the outbox is re-read, and
losing them (a scheduler restart) costs one redundant, no-op sweep. This is the
same tradeoff ``run_owner_routing_pass`` and ``run_reaper_scheduler_pass``
already make, for the same reason -- no persisted "next due at" column exists,
and the pass is safe to repeat.

Ownership record (D02)
----------------------
:Owner: platform governance -- ownership and identity lifecycle.
:Default: OFF. ``settings.principal_reconciliation_enabled`` defaults to
    ``False``, so wiring this into the scheduler cannot start mutating
    ownership in an existing deployment. The handler's own
    ``ownership_leaver_auto_reassign`` switch remains a second, independent
    gate.
:Production eligibility: eligible once an identity source (IdP webhook,
    directory sync, admin workflow) actually calls ``identity_events``. With
    no emitter, the pass finds no events and does nothing -- turning it on
    early is harmless but pointless.
:Retirement condition: retire when an external identity service owns ownership
    reassignment directly, together with ``identity_events`` and
    ``ownership_principal_lifecycle``.

Authorisation
-------------
The pass runs inside the scheduler process and attributes every mutation to
the named ``fleet-scheduler`` WORKER principal that the handlers already use
for their audit rows (``ownership_principal_lifecycle._system_context``), so a
reassignment made by this pass is attributable in the audit trail exactly like
one made by a human operator -- not an anonymous background write. Tenant scope
comes from the event itself: the payload's ``organization_id`` narrows the
handler to one tenant, and only an event that genuinely carries no organization
reconciles cross-tenant (identity is a cross-tenant concern, ADR-0018).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aida.db import session_factory
from aida.identity_events import (
    PRINCIPAL_DELETED_EVENT_TYPE,
    PRINCIPAL_MERGED_EVENT_TYPE,
)
from aida.models import OutboxEvent
from aida.ownership_principal_lifecycle import (
    handle_principal_deleted,
    handle_principal_merged,
)
from atlas.platform.config import Settings

logger = structlog.get_logger(__name__)

RECONCILED_EVENT_TYPES = (PRINCIPAL_DELETED_EVENT_TYPE, PRINCIPAL_MERGED_EVENT_TYPE)

# Last time this process ran the pass. Process-local; see the module docstring
# on why losing it costs a redundant no-op sweep and nothing else.
_last_run_at: datetime | None = None


def reset_cadence() -> None:
    """Forget the in-process cadence stamp. For tests; never called in production."""
    global _last_run_at
    _last_run_at = None


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """What one pass did. All zeros is the normal steady state."""

    events_examined: int
    events_applied: int
    assignments_lapsed: int
    assignments_reassigned: int
    skipped: bool
    skip_reason: str | None = None


_SKIPPED_DISABLED = ReconciliationOutcome(
    0, 0, 0, 0, True, "principal_reconciliation_enabled=False"
)
_SKIPPED_NOT_DUE = ReconciliationOutcome(0, 0, 0, 0, True, "not due")


def _organization_id(event: OutboxEvent, payload: dict[str, Any]) -> UUID | None:
    """Tenant scope for one event: the payload's organization, else the row's.

    Both emitters write the same value into both places, so they agree in
    practice; the payload is preferred because it is the copy that travels with
    the event to any other consumer, and disagreeing with those consumers about
    which tenant an event belongs to is worse than disagreeing with the row.
    A genuinely absent organization means "every tenant this principal appears
    in", which is what the handlers do with `organization_id=None`.
    """
    raw = payload.get("organization_id") or event.organization_id
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except ValueError:
        # A malformed id must not silently widen the scan to every tenant.
        logger.warning(
            "principal_reconciliation_bad_organization_id",
            event_id=str(event.id),
            event_type=event.event_type,
        )
        raise


async def run_principal_reconciliation_pass(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> ReconciliationOutcome:
    """Replay recent principal-lifecycle events through the ownership handlers.

    Off unless ``settings.principal_reconciliation_enabled`` is set, and
    rate-limited to ``settings.principal_reconciliation_interval_seconds`` so
    calling it on every scheduler iteration is cheap. One event's failure is
    logged and skipped rather than aborting the sweep -- the same
    fault-isolation shape ``run_owner_routing_pass`` uses -- because a single
    malformed payload must not stop every other leaver from being reconciled.
    """
    global _last_run_at
    if not settings.principal_reconciliation_enabled:
        return _SKIPPED_DISABLED
    effective_now = now or datetime.now(UTC)
    interval = timedelta(seconds=settings.principal_reconciliation_interval_seconds)
    if _last_run_at is not None and effective_now - _last_run_at < interval:
        return _SKIPPED_NOT_DUE

    window_start = effective_now - timedelta(
        seconds=settings.principal_reconciliation_lookback_seconds
    )
    async with session_factory() as session:
        events = (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type.in_(RECONCILED_EVENT_TYPES),
                    OutboxEvent.occurred_at >= window_start,
                )
                .order_by(OutboxEvent.occurred_at)
                .limit(settings.principal_reconciliation_batch_size)
            )
        ).all()

    examined = 0
    applied = 0
    lapsed = 0
    reassigned = 0
    for event in events:
        examined += 1
        payload = event.payload if isinstance(event.payload, dict) else {}
        # A transaction per event: a failure on one leaver must not roll back
        # the reassignments already made for the ones before it.
        try:
            async with session_factory() as session:
                organization_id = _organization_id(event, payload)
                if event.event_type == PRINCIPAL_DELETED_EVENT_TYPE:
                    principal_id = str(payload["principal_id"])
                    result = await handle_principal_deleted(
                        session,
                        settings=settings,
                        principal_id=principal_id,
                        organization_id=organization_id,
                        now=effective_now,
                    )
                else:
                    result = await handle_principal_merged(
                        session,
                        settings=settings,
                        from_principal_id=str(payload["from_principal_id"]),
                        into_principal_id=str(payload["into_principal_id"]),
                        organization_id=organization_id,
                        now=effective_now,
                    )
                await session.commit()
        except Exception:
            logger.exception(
                "principal_reconciliation_event_failed",
                event_id=str(event.id),
                event_type=event.event_type,
            )
            continue
        applied += 1
        lapsed += len(result.lapsed_assignment_ids)
        reassigned += len(result.reassigned_assignment_ids)

    _last_run_at = effective_now
    if lapsed or reassigned:
        # Only logged when something actually changed: a pass that replayed 40
        # already-reconciled events and changed nothing is the expected steady
        # state and does not deserve a line every cycle.
        logger.info(
            "principal_reconciliation_pass",
            events_examined=examined,
            events_applied=applied,
            assignments_lapsed=lapsed,
            assignments_reassigned=reassigned,
        )
    return ReconciliationOutcome(
        events_examined=examined,
        events_applied=applied,
        assignments_lapsed=lapsed,
        assignments_reassigned=reassigned,
        skipped=False,
    )
