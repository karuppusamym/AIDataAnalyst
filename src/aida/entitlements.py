"""Idempotent external entitlement provisioning isolated from governance decisions."""

from dataclasses import dataclass
from typing import Literal

import httpx

from aida.config import Settings
from aida.models import DataProductAccessRequest

EntitlementAction = Literal["PROVISION", "REVOKE"]


@dataclass(frozen=True, slots=True)
class EntitlementResult:
    status: Literal["PENDING", "PROVISIONED", "REVOKED", "FAILED"]
    provider: str
    reference: str | None = None
    error: str | None = None


async def apply_entitlement(
    settings: Settings,
    access_request: DataProductAccessRequest,
    action: EntitlementAction,
) -> EntitlementResult:
    """Invoke a provider with an immutable idempotency key and a value-free payload."""
    if settings.entitlement_provider == "outbox":
        return EntitlementResult(status="PENDING", provider="outbox")
    if not settings.entitlement_webhook_url:
        return EntitlementResult(
            status="FAILED", provider="webhook", error="entitlement webhook is not configured"
        )
    token = (
        settings.entitlement_webhook_token.get_secret_value()
        if settings.entitlement_webhook_token
        else None
    )
    headers = {
        "Idempotency-Key": f"{access_request.id}:{action}",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "action": action,
        "access_request_id": str(access_request.id),
        "organization_id": str(access_request.organization_id),
        "data_product_version_id": str(access_request.data_product_version_id),
        "principal_id": access_request.requested_by,
        "expires_at": access_request.expires_at.isoformat() if access_request.expires_at else None,
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.entitlement_timeout_seconds, follow_redirects=False
        ) as client:
            response = await client.post(
                settings.entitlement_webhook_url, json=payload, headers=headers
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return EntitlementResult(status="FAILED", provider="webhook", error=str(exc)[:1000])
    reference = result.get("reference") if isinstance(result, dict) else None
    return EntitlementResult(
        status="PROVISIONED" if action == "PROVISION" else "REVOKED",
        provider="webhook",
        reference=str(reference)[:500] if reference else None,
    )
