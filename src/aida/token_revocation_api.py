"""Token revocation admin/self-service API (ID-4, module 01 identity-and-tenancy).

A single endpoint, deliberately: revoking a token requires possessing that token
(the caller must supply the raw JWT to revoke, not just a claimed identifier), so
this doubles as self-service logout -- revoking your own current token -- and as
the admin response to a compromised credential -- revoking a token that was
captured elsewhere (logs, a SIEM alert, an incident report). Requiring the raw
token rather than an asserted `jti` means nobody can revoke -- or probe the
existence of -- a token they cannot themselves produce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import RevokedToken
from aida.oidc import OidcVerificationError, OidcVerifier, context_from_claims, token_identifier
from aida.schemas import ApiModel
from aida.security import SecurityContext, get_security_context

router = APIRouter(prefix="/v1", tags=["identity"])

_ADMIN_ROLES = ("PlatformAdmin", "OrganizationAdmin")


class TokenRevocationRequest(ApiModel):
    token: str = Field(min_length=1, max_length=8_000)
    reason: str = Field(min_length=3, max_length=500)


class TokenRevocationRead(ApiModel):
    token_identifier: str
    subject: str
    organization_id: UUID | None
    self_revocation: bool
    revoked_at: datetime
    token_expires_at: datetime


@router.post(
    "/security/tokens/revoke",
    response_model=TokenRevocationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Revoke a bearer token (self logout, or admin response to a compromised credential)",
)
async def revoke_token(
    body: TokenRevocationRequest,
    context: SecurityContext = Depends(get_security_context),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> TokenRevocationRead:
    if settings.identity_provider != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="token revocation requires the OIDC identity provider",
        )
    try:
        claims = await OidcVerifier(settings).verify(body.token)
        target = context_from_claims(claims, settings)
    except OidcVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the supplied token could not be verified",
        ) from exc

    is_self = target.principal_id == context.principal_id
    if not is_self:
        if context.roles.isdisjoint(_ADMIN_ROLES):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="revoking another principal's token requires an admin role",
            )
        if "PlatformAdmin" not in context.roles:
            if target.organization_id is None or target.organization_id != context.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="cross-organization token revocation denied",
                )

    identifier = token_identifier(claims)
    expires_at = datetime.fromtimestamp(claims["exp"], tz=UTC)
    record = RevokedToken(
        organization_id=target.organization_id,
        token_identifier=identifier,
        subject=target.principal_id,
        token_expires_at=expires_at,
        revoked_by=context.principal_id,
        reason=body.reason,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="token is already revoked",
        ) from None

    record_audit(
        session,
        context,
        action="token.revoked",
        resource_type="token",
        resource_id=identifier,
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"subject": target.principal_id, "self_revocation": is_self},
    )
    await session.commit()
    await session.refresh(record)
    return TokenRevocationRead(
        token_identifier=record.token_identifier,
        subject=record.subject,
        organization_id=record.organization_id,
        self_revocation=is_self,
        revoked_at=record.revoked_at,
        token_expires_at=record.token_expires_at,
    )
