from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import get_settings
from aida.models import AuditEvent, OutboxEvent
from aida.security import SecurityContext
from aida.siem_routing import SecurityEvent, SiemConfig, route_to_siem

# OB-2: audit actions that are security-relevant even though the outcome is
# SUCCESS -- a kill switch being engaged/released, or a token revoked, is
# never a policy *denial*, but a SOC still needs to see the control change.
_SECURITY_CONTROL_ACTIONS = frozenset(
    {
        "model.kill_switch_engage",
        "model.kill_switch_release",
        "token.revoked",
    }
)

_DENIAL_OUTCOMES = frozenset({"DENIED", "FAILED", "FAILURE", "REJECTED"})


def _classify_security_event(action: str, outcome: str) -> tuple[str, str] | None:
    """OB-2: map an audit action/outcome pair to a SIEM (event_type,
    severity) when it is security-relevant. Returns None for the routine
    create/read/update audit events that make up most of the audit trail --
    those never reach a SOC.
    """
    if outcome in _DENIAL_OUTCOMES:
        return "POLICY_VIOLATION", "MEDIUM"
    if action in _SECURITY_CONTROL_ACTIONS:
        return "SECURITY_CONTROL_CHANGE", "MEDIUM"
    return None


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

    # OB-2: this is the one funnel every audit event in the platform passes
    # through (DENIED policy checks, kill-switch engagement, token
    # revocation included), so it is where SIEM routing is wired rather than
    # at each of the dozen-plus individual call sites.
    classified = _classify_security_event(action, outcome)
    if classified is not None:
        event_type, severity = classified
        settings = get_settings()
        route_to_siem(
            SecurityEvent(
                event_type=event_type,
                severity=severity,
                source=context.source_ip or "internal",
                organization_id=(
                    str(context.organization_id) if context.organization_id else None
                ),
                principal_id=context.principal_id,
                correlation_id=correlation_id,
                details={
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            ),
            SiemConfig(
                transport=settings.siem_transport,
                endpoint=settings.siem_endpoint,
                enabled=settings.siem_enabled,
                include_details=settings.siem_include_details,
            ),
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
