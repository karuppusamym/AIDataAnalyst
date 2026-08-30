"""Notification and escalation routing API (DQ-1).

Provides CRUD endpoints for notification rules and notification events,
plus an acknowledgement endpoint for incidents.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import NotificationEventRecord, NotificationRuleRecord
from aida.schemas import (
    NotificationEventRead,
    NotificationRuleCreate,
    NotificationRuleRead,
    NotificationRuleUpdate,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["notifications"])


@router.post("/notification-rules", response_model=NotificationRuleRead, status_code=201)
async def create_notification_rule(
    body: NotificationRuleCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> NotificationRuleRead:
    org_id = context.require_organization()
    rule = NotificationRuleRecord(
        organization_id=org_id,
        name=body.name,
        conditions=body.conditions,
        channel=body.channel,
        recipients=body.recipients,
        escalation_after_minutes=body.escalation_after_minutes,
        enabled=body.enabled,
        created_by=context.principal_id,
    )
    session.add(rule)
    await session.flush()
    record_audit(
        session,
        context,
        action="notification.rule.create",
        resource_type="notification_rule",
        resource_id=str(rule.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"channel": body.channel, "enabled": body.enabled},
    )
    record_outbox(
        session,
        organization_id=org_id,
        aggregate_type="notification_rule",
        aggregate_id=str(rule.id),
        event_type="notification.rule.created.v1",
        payload={"channel": body.channel},
    )
    await session.commit()
    await session.refresh(rule)
    return NotificationRuleRead.model_validate(rule)


@router.get("/notification-rules", response_model=Page)
async def list_notification_rules(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    org_id = context.require_organization()
    filters = [NotificationRuleRecord.organization_id == org_id]
    total = await session.scalar(
        select(func.count()).select_from(NotificationRuleRecord).where(*filters)
    )
    rows = (
        await session.scalars(
            select(NotificationRuleRecord)
            .where(*filters)
            .order_by(NotificationRuleRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[NotificationRuleRead.model_validate(r) for r in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.put("/notification-rules/{rule_id}", response_model=NotificationRuleRead)
async def update_notification_rule(
    rule_id: UUID,
    body: NotificationRuleUpdate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> NotificationRuleRead:
    rule = await session.get(NotificationRuleRecord, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="notification rule not found")
    enforce_organization(context, rule.organization_id)
    for field_name in body.model_fields_set:
        setattr(rule, field_name, getattr(body, field_name))
    await session.flush()
    record_audit(
        session,
        context,
        action="notification.rule.update",
        resource_type="notification_rule",
        resource_id=str(rule.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    await session.commit()
    await session.refresh(rule)
    return NotificationRuleRead.model_validate(rule)


@router.delete("/notification-rules/{rule_id}", status_code=204)
async def delete_notification_rule(
    rule_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> None:
    rule = await session.get(NotificationRuleRecord, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="notification rule not found")
    enforce_organization(context, rule.organization_id)
    record_audit(
        session,
        context,
        action="notification.rule.delete",
        resource_type="notification_rule",
        resource_id=str(rule.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    await session.delete(rule)
    await session.commit()


@router.get("/notifications", response_model=Page)
async def list_notifications(
    incident_id: UUID | None = None,
    notification_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    org_id = context.require_organization()
    filters = [NotificationEventRecord.organization_id == org_id]
    if incident_id:
        filters.append(NotificationEventRecord.incident_id == incident_id)
    if notification_status:
        filters.append(NotificationEventRecord.status == notification_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(NotificationEventRecord).where(*filters)
    )
    rows = (
        await session.scalars(
            select(NotificationEventRecord)
            .where(*filters)
            .order_by(NotificationEventRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[NotificationEventRead.model_validate(r) for r in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/notifications/{notification_id}/acknowledge",
    response_model=NotificationEventRead,
)
async def acknowledge_notification(
    notification_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> NotificationEventRead:
    event = await session.get(NotificationEventRecord, notification_id)
    if event is None:
        raise HTTPException(status_code=404, detail="notification event not found")
    enforce_organization(context, event.organization_id)
    if event.acknowledged_at is not None:
        raise HTTPException(status_code=409, detail="notification already acknowledged")
    event.acknowledged_at = datetime.now(UTC)
    event.acknowledged_by = context.principal_id
    record_audit(
        session,
        context,
        action="notification.event.acknowledge",
        resource_type="notification_event",
        resource_id=str(event.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    await session.commit()
    await session.refresh(event)
    return NotificationEventRead.model_validate(event)
