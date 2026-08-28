from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import AuditEvent, OutboxEvent
from aida.security import SecurityContext


def record_audit(
    session: AsyncSession,
    context: SecurityContext,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    outcome: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            organization_id=context.organization_id,
            principal_id=context.principal_id,
            principal_type=context.principal_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            correlation_id=correlation_id,
            source_ip=context.source_ip,
            details=details or {},
        )
    )


def record_outbox(
    session: AsyncSession,
    *,
    organization_id: UUID | None,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        OutboxEvent(
            organization_id=organization_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
    )
