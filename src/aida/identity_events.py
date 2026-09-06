"""P2-07: emission side for the two identity principal-lifecycle events the
ownership-lifecycle handler consumes.

Neither ``identity_merge`` (table-identity merging) nor
``identity_resolution`` (structural table matching) covered *principal*
lifecycle before P2-07 -- the audit finding that an ownership row survives
its owner. This module is deliberately small: an audit-and-outbox emit for
each of the two events, plus a same-transaction call into
``ownership_principal_lifecycle`` so the ACTIVE-> LAPSED_LEAVER flip lands
in the same transaction as the identity change, not on the next outbox
drain.

Existing identity workflows (bank IAM sync, admin CLI delete, etc.) should
call ``emit_principal_deleted`` / ``emit_principal_merged`` at the moment
they remove or merge a principal. The event itself is what downstream
services (SIEM, audit archive, external identity sync) also consume.

Ownership record (D02; F19 in ``Docs/review-2026-09-05/REVIEW.md``)
-------------------------------------------------------------------
:Owner: platform governance -- ownership and identity lifecycle.
:Default: this module mutates nothing on its own. Its handlers are gated by
    ``settings.ownership_leaver_auto_reassign``; the only entry point that
    reaches them from a running process is ``aida.principal_reconciliation``,
    which is off by default (``settings.principal_reconciliation_enabled``).
:Production eligibility: eligible once an identity source actually calls the
    two emitters below -- an IdP webhook, a directory sync, or an admin
    workflow. Until one exists, the reconciliation pass replays the outbox
    rows these functions write, so a deployment that has never called them
    has nothing to reconcile and the pass is a no-op.
:Retirement condition: retire this module when principal lifecycle moves to
    an external identity service that owns ownership reassignment directly.
    Retiring it means deleting the two emitters, the reconciliation pass and
    ``ownership_principal_lifecycle`` together -- they are one feature.

The two ``event_type`` strings are constants here rather than literals at
each call site because the reconciliation consumer
(``aida.principal_reconciliation``) has to select on exactly the strings
this module writes. A consumer that disagreed with the emitter by one
character would silently reconcile nothing, which is the failure mode F19
already found once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.events import record_audit, record_outbox
from aida.ownership_principal_lifecycle import (
    PrincipalLeaverResult,
    handle_principal_deleted,
    handle_principal_merged,
)
from aida.security import SecurityContext

PRINCIPAL_DELETED_EVENT_TYPE = "identity.principal.deleted.v1"
PRINCIPAL_MERGED_EVENT_TYPE = "identity.principal.merged.v1"


async def emit_principal_deleted(
    session: AsyncSession,
    *,
    settings: Settings,
    context: SecurityContext,
    principal_id: str,
    organization_id: UUID | None = None,
    now: datetime | None = None,
    reason: str | None = None,
) -> PrincipalLeaverResult:
    """Record the ``identity.principal.deleted.v1`` event and reconcile
    ownership in the same transaction."""
    effective_now = now or datetime.now(UTC)
    payload = {
        "principal_id": principal_id,
        "organization_id": str(organization_id) if organization_id else None,
        "deleted_at": effective_now.isoformat(),
        "reason": reason,
    }
    record_audit(
        session,
        context,
        action="IDENTITY_PRINCIPAL_DELETED",
        resource_type="principal",
        resource_id=principal_id,
        outcome="SUCCESS",
        correlation_id=principal_id,
        details=payload,
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="principal",
        aggregate_id=principal_id,
        event_type=PRINCIPAL_DELETED_EVENT_TYPE,
        payload=payload,
    )
    return await handle_principal_deleted(
        session,
        settings=settings,
        principal_id=principal_id,
        organization_id=organization_id,
        now=effective_now,
    )


async def emit_principal_merged(
    session: AsyncSession,
    *,
    settings: Settings,
    context: SecurityContext,
    from_principal_id: str,
    into_principal_id: str,
    organization_id: UUID | None = None,
    now: datetime | None = None,
    reason: str | None = None,
) -> PrincipalLeaverResult:
    """Record the ``identity.principal.merged.v1`` event and reconcile
    ownership in the same transaction."""
    effective_now = now or datetime.now(UTC)
    payload = {
        "from_principal_id": from_principal_id,
        "into_principal_id": into_principal_id,
        "organization_id": str(organization_id) if organization_id else None,
        "merged_at": effective_now.isoformat(),
        "reason": reason,
    }
    record_audit(
        session,
        context,
        action="IDENTITY_PRINCIPAL_MERGED",
        resource_type="principal",
        resource_id=from_principal_id,
        outcome="SUCCESS",
        correlation_id=from_principal_id,
        details=payload,
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="principal",
        aggregate_id=from_principal_id,
        event_type=PRINCIPAL_MERGED_EVENT_TYPE,
        payload=payload,
    )
    return await handle_principal_merged(
        session,
        settings=settings,
        from_principal_id=from_principal_id,
        into_principal_id=into_principal_id,
        organization_id=organization_id,
        now=effective_now,
    )
