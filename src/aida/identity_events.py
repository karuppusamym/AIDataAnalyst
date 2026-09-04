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
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit, record_outbox
from aida.ownership_principal_lifecycle import (
    PrincipalLeaverResult,
    handle_principal_deleted,
    handle_principal_merged,
)
from aida.security import SecurityContext


async def emit_principal_deleted(
    session: AsyncSession,
    *,
    settings,
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
        event_type="identity.principal.deleted.v1",
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
    settings,
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
        event_type="identity.principal.merged.v1",
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
