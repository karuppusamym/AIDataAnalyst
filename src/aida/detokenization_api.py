"""Detokenization endpoint (QG-6, module 16 query-gateway).

`query_gateway.py`'s masking pass replaces a tokenization-configured column's
value with a reversible token (`aida.tokenization.TokenizationProvider`)
instead of the flat ``"***MASKED***"`` redaction. That reversal must never
happen as a side effect of running a query -- the whole point of tokenizing
rather than redacting is that recovering the original value is a deliberate,
narrow, accountable act, not something a query result implicitly grants.

This module is that one, explicit act: a single endpoint, gated to a small set
of roles, unconditionally audited on both the allow and the deny path (a
denied attempt is exactly as much evidence as a granted one -- INV-7's
"attributable" property does not stop at successes), and fail-closed the same
way `query_gateway.py`'s tokenize call is -- a provider that cannot be
resolved or that fails mid-call denies the request rather than returning the
token unchanged or a best-effort guess.

Mirrors `token_revocation_api.py`'s shape (a single sensitive administrative
action, manually role-checked inside the handler rather than through a bare
`require_roles` dependency) specifically so the denial path can be audited
before the 403 is raised -- a `Depends(require_roles(...))` failure never
reaches a handler body at all, which would make an unauthorized attempt
unaccountable rather than merely refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.schemas import ApiModel
from aida.security import SecurityContext, get_security_context
from aida.tokenization import TokenizationError, resolve_tokenization_provider

router = APIRouter(prefix="/v1", tags=["query-gateway"])

# Same tier as `token_revocation_api._ADMIN_ROLES` plus the two roles this
# platform already uses elsewhere for sensitive-data review duties
# (`ai_registry_api.AI_READERS`, `api.CATALOG_BULK_ACTION_WRITE_ROLES`):
# reversing a token is a compliance/fraud-investigation action, not a general
# read, so "can see masked results" (any authenticated role) is deliberately
# not enough.
DETOKENIZE_ROLES = ("PlatformAdmin", "OrganizationAdmin", "ComplianceOfficer", "DataSteward")


class DetokenizeRequest(ApiModel):
    token: str = Field(min_length=1, max_length=2_000)
    # Required, not optional: a detokenize call with no stated reason is exactly
    # the silent-recovery path this endpoint exists to prevent. Free text today
    # (matches `TokenRevocationRequest.reason`); narrowing to an enum of approved
    # purposes is a policy decision for whoever owns the compliance workflow this
    # feeds, not this module.
    purpose: str = Field(min_length=3, max_length=500)
    datasource_id: UUID | None = None


class DetokenizeRead(ApiModel):
    value: str
    detokenized_at: datetime


@router.post(
    "/security/tokens/detokenize",
    response_model=DetokenizeRead,
    status_code=status.HTTP_200_OK,
    summary="Reverse a tokenized value back to its original form (gated, audited; QG-6)",
)
async def detokenize_value(
    body: DetokenizeRequest,
    context: SecurityContext = Depends(get_security_context),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> DetokenizeRead:
    correlation_id = get_correlation_id()
    audit_details: dict[str, object] = {
        "purpose": body.purpose,
        "datasource_id": str(body.datasource_id) if body.datasource_id else None,
    }
    if context.roles.isdisjoint(DETOKENIZE_ROLES):
        # INV-6: no token or resource value in the denial record -- the same
        # value-freedom the gate() denial path already keeps.
        record_audit(
            session,
            context,
            action="query.detokenize",
            resource_type="token",
            resource_id=None,
            outcome="DENIED",
            correlation_id=correlation_id,
            details={**audit_details, "reason": "ROLE_NOT_AUTHORIZED"},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"detokenization requires one of these roles: {', '.join(DETOKENIZE_ROLES)}",
        )

    try:
        provider = resolve_tokenization_provider(settings)
        value = await provider.detokenize(body.token)
    except TokenizationError as exc:
        record_audit(
            session,
            context,
            action="query.detokenize",
            resource_type="token",
            resource_id=None,
            outcome="DENIED",
            correlation_id=correlation_id,
            details={**audit_details, "reason": "TOKENIZATION_PROVIDER_UNAVAILABLE"},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the tokenization provider is unavailable",
        ) from exc

    record_audit(
        session,
        context,
        action="query.detokenize",
        resource_type="token",
        resource_id=None,
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details=audit_details,
    )
    await session.commit()
    return DetokenizeRead(value=value, detokenized_at=datetime.now(UTC))
