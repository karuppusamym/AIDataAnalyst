from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.oidc import OidcVerificationError, OidcVerifier, context_from_claims
from aida.security_types import SecurityContext as SecurityContext
from aida.siem_routing import SecurityEvent, SiemConfig, route_to_siem
from aida.token_revocation import TokenRevokedError, enforce_not_revoked

_oidc_verifiers: dict[tuple[str, str, str, str], OidcVerifier] = {}


def _route_auth_failure(settings: Settings, reason: str) -> None:
    """OB-2: every rejected bearer-token attempt is a SOC-notable
    AUTH_FAILURE. This runs before a `SecurityContext` exists, so it cannot
    go through `aida.events.record_audit` like every other security event in
    this codebase (that funnel is what routes DENIED/kill-switch/revocation
    events) -- it calls `route_to_siem` directly instead, at the exact point
    authentication itself is refused.
    """
    route_to_siem(
        SecurityEvent(
            event_type="AUTH_FAILURE",
            severity="HIGH",
            source="oidc-gateway",
            correlation_id=get_correlation_id(),
            details={"reason": reason},
        ),
        SiemConfig(
            transport=settings.siem_transport,
            endpoint=settings.siem_endpoint,
            enabled=settings.siem_enabled,
            include_details=settings.siem_include_details,
        ),
    )


def _oidc_verifier(settings: Settings) -> OidcVerifier:
    key = (
        settings.oidc_issuer or "",
        settings.oidc_audience or "",
        settings.oidc_jwks_url or "",
        settings.oidc_jwks_json or "",
    )
    verifier = _oidc_verifiers.get(key)
    if verifier is None:
        verifier = OidcVerifier(settings)
        _oidc_verifiers[key] = verifier
    return verifier


async def get_security_context(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    principal_id: str | None = Header(default=None, alias="X-Principal-Id"),
    principal_type: str = Header(default="USER", alias="X-Principal-Type"),
    organization_header: UUID | None = Header(default=None, alias="X-Organization-Id"),
    roles: str = Header(default="Viewer", alias="X-Roles"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    business_purpose: str | None = Header(default=None, alias="X-Business-Purpose"),
) -> SecurityContext:
    if settings.identity_provider == "oidc":
        if not authorization or not authorization.startswith("Bearer "):
            _route_auth_failure(settings, "missing bearer token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a bearer token is required",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            _route_auth_failure(settings, "empty bearer token")
            raise HTTPException(status_code=401, detail="a bearer token is required")
        try:
            claims = await _oidc_verifier(settings).verify(token)
            # ID-4: a revoked token -- including one presented again after logout or
            # an admin's response to a compromised credential -- must be rejected on
            # this, its very next use. A failed lookup denies too (INV-4 fail closed).
            await enforce_not_revoked(session, claims)
            return context_from_claims(claims, settings)
        except (OidcVerificationError, TokenRevokedError) as exc:
            _route_auth_failure(settings, str(exc))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="bearer token verification failed",
            ) from exc
    if settings.identity_provider == "development":
        if not principal_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-Principal-Id is required in development mode",
            )
        role_set = frozenset(role.strip() for role in roles.split(",") if role.strip())
        return SecurityContext(
            principal_id=principal_id,
            principal_type=principal_type,
            organization_id=organization_header,
            roles=role_set,
            business_purpose=(business_purpose.strip()[:200] if business_purpose else None),
        )
    raise HTTPException(status_code=503, detail="identity provider is unavailable")


def require_roles(*allowed: str) -> Callable[[SecurityContext], Awaitable[SecurityContext]]:
    async def dependency(
        context: SecurityContext = Depends(get_security_context),
    ) -> SecurityContext:
        if context.roles.isdisjoint(allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"one of these roles is required: {', '.join(sorted(allowed))}",
            )
        return context

    return dependency


def enforce_organization(context: SecurityContext, requested: UUID) -> None:
    if "PlatformAdmin" in context.roles:
        return
    if context.organization_id != requested:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-organization access denied",
        )
