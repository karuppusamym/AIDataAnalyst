from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from aida.config import Settings, get_settings
from aida.oidc import OidcVerificationError, OidcVerifier, context_from_claims
from aida.security_types import SecurityContext as SecurityContext

_oidc_verifiers: dict[tuple[str, str, str, str], OidcVerifier] = {}


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
    principal_id: str | None = Header(default=None, alias="X-Principal-Id"),
    principal_type: str = Header(default="USER", alias="X-Principal-Type"),
    organization_header: UUID | None = Header(default=None, alias="X-Organization-Id"),
    roles: str = Header(default="Viewer", alias="X-Roles"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    business_purpose: str | None = Header(default=None, alias="X-Business-Purpose"),
) -> SecurityContext:
    if settings.identity_provider == "oidc":
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a bearer token is required",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(status_code=401, detail="a bearer token is required")
        try:
            claims = await _oidc_verifier(settings).verify(token)
            return context_from_claims(claims, settings)
        except OidcVerificationError as exc:
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
