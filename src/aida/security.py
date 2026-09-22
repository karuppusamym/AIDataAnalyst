from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.delegation import DelegationGrant, delegated_roles_available, is_delegation_active
from aida.models import Delegation
from aida.oidc import (
    OidcTokenExpired,
    OidcVerificationError,
    OidcVerifier,
    context_from_claims,
)
from aida.security_types import SecurityContext as SecurityContext
from aida.siem_delivery import siem_config_from_settings
from aida.siem_routing import SecurityEvent, route_to_siem_durably
from aida.token_revocation import TokenRevokedError, enforce_not_revoked

_oidc_verifiers: dict[tuple[str, str, str, str, int, int], OidcVerifier] = {}


async def _route_auth_failure(settings: Settings, reason: str) -> None:
    """OB-2: every rejected bearer-token attempt is a SOC-notable
    AUTH_FAILURE. This runs before a `SecurityContext` exists, so it cannot
    go through `aida.events.record_audit` like every other security event in
    this codebase (that funnel is what routes DENIED/kill-switch/revocation
    events) -- it records the intent directly instead, at the exact point
    authentication itself is refused.

    `route_to_siem_durably` rather than `route_to_siem` (F04/F12): this call
    is followed immediately by an `HTTPException`, so the request's session is
    rolled back and closed. An intent staged into it would be discarded, which
    is the same "evidence that never commits" defect on the one path where a
    SOC most needs the record. It commits its own transaction instead, and
    returns without opening one at all when no destination is configured.
    """
    await route_to_siem_durably(
        SecurityEvent(
            event_type="AUTH_FAILURE",
            severity="HIGH",
            source="oidc-gateway",
            correlation_id=get_correlation_id(),
            details={"reason": reason},
        ),
        siem_config_from_settings(settings),
    )


def shared_oidc_verifier(settings: Settings) -> OidcVerifier:
    """The one verifier this process uses for `settings`' identity provider (R11-AUD10).

    Everything that verifies a bearer token asks here -- `get_security_context` for the caller's
    own token, `token_revocation_api.revoke_token` for the token it is asked to revoke -- because
    the verifier is where the key set is cached and where the windows that protect the provider
    live: the unknown-`kid` cooldown (R11-AUD09) and the backoff after a failed refresh
    (R11-AUD10). A verifier built per call has an empty cache and no memory of the last failure,
    so it fetches every time and is limited by nothing; the revocation route did exactly that. One
    shared verifier also means a failure one route meets is known to the other.

    Created lazily, on first use, and kept for the life of the process. "The same identity
    provider" is every setting the verifier reads -- issuer, audience, key-set URL, pinned key
    set, clock skew and cache lifetime; `tests/test_oidc.py` fails when the verifier class reads
    one that the key below does not name. Two `Settings` that differ in any of them get different
    verifiers, so a verifier can never apply one configuration's tolerance to another's tokens --
    in a running process there is one `Settings` (`get_settings` is cached) and so one verifier,
    while tests that build their own `Settings` stay apart. The clock skew and the cache lifetime
    were not part of this identity before; they are now because `verify` reads both.

    A verifier holds an `asyncio.Lock`, which is tied to the event loop that first contends for
    it. Real deployments have one loop per process. A test that runs its own loops and reaches
    this must call `reset_shared_oidc_verifiers` first, which also drops the key set another test
    left cached.
    """
    key = (
        settings.oidc_issuer or "",
        settings.oidc_audience or "",
        settings.oidc_jwks_url or "",
        settings.oidc_jwks_json or "",
        settings.oidc_clock_skew_seconds,
        settings.oidc_jwks_cache_seconds,
    )
    verifier = _oidc_verifiers.get(key)
    if verifier is None:
        verifier = OidcVerifier(settings)
        _oidc_verifiers[key] = verifier
    return verifier


def reset_shared_oidc_verifiers() -> None:
    """Forget every shared verifier, so the next use builds a fresh one with an empty cache.

    For tests, which share a process and would otherwise share a verifier -- and the key set,
    the backoff and the failure it remembers -- between two tests that configure the same
    identity provider.
    """
    _oidc_verifiers.clear()


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
            await _route_auth_failure(settings, "missing bearer token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a bearer token is required",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            await _route_auth_failure(settings, "empty bearer token")
            raise HTTPException(status_code=401, detail="a bearer token is required")
        try:
            claims = await shared_oidc_verifier(settings).verify(token)
            # ID-4: a revoked token -- including one presented again after logout or
            # an admin's response to a compromised credential -- must be rejected on
            # this, its very next use. A failed lookup denies too (INV-4 fail closed).
            await enforce_not_revoked(session, claims)
            return context_from_claims(claims, settings)
        except (OidcVerificationError, TokenRevokedError) as exc:
            await _route_auth_failure(settings, str(exc))
            # Expiry is the only reason named to the caller (see
            # `OidcTokenExpired`). Revocation stays identical to every other
            # failure, as `TokenRevokedError` requires (INV-4).
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "bearer token has expired"
                    if isinstance(exc, OidcTokenExpired)
                    else "bearer token verification failed"
                ),
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


def _as_aware_utc(value: datetime) -> datetime:
    """sqlite (used by every in-memory test in this codebase) does not
    preserve tzinfo on a `DateTime(timezone=True)` column the way Postgres
    does -- a value round-tripped through a real sqlite-backed session comes
    back naive even though it was written aware, and comparing a naive and
    an aware datetime raises `TypeError` (`tests/test_profiling_exception_policy.py`
    hits the identical quirk and works around it the same way). A value that
    already carries tzinfo (the normal Postgres case) passes through
    unchanged.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def require_roles_or_delegated(
    *allowed: str,
) -> Callable[..., Awaitable[SecurityContext]]:
    """Like `require_roles`, but a principal holding none of `allowed`
    directly may still pass if an active, unexpired delegation currently
    grants it one of `allowed` from a delegator who holds it.

    PG-4: delegation has to actually widen who is *permitted* at the real
    enforcement point, not just exist as inert data next to it -- the
    tracker's own most common documented failure mode (DQ-3, RT-7, AG-6,
    TL-3, OB-1/2/3). This is that enforcement point for governance-review
    decision authority: `semantic_api.decide_governance_review` and
    `bulk_decide_governance_reviews` depend on this instead of plain
    `require_roles`, so an active delegation actually lets the delegate
    decide a review it otherwise couldn't.

    On the delegated path the returned context has the matched role(s)
    unioned into `roles` (so every existing role check downstream, e.g.
    self-approval, keeps working unmodified) and carries
    `active_delegation_id` / `active_delegator_principal_id` so the caller
    can audit *which* delegation was exercised, at the moment it was used --
    PG-4's "audited" requirement covers both grant and use, not grant alone.
    """

    async def dependency(
        context: SecurityContext = Depends(get_security_context),
        session: AsyncSession = Depends(get_session),
    ) -> SecurityContext:
        if not context.roles.isdisjoint(allowed):
            return context
        if context.organization_id is not None:
            now = datetime.now(UTC)
            rows = (
                await session.scalars(
                    select(Delegation).where(
                        Delegation.organization_id == context.organization_id,
                        Delegation.delegate_principal_id == context.principal_id,
                        Delegation.status == "ACTIVE",
                    )
                )
            ).all()
            grants = [
                DelegationGrant(
                    id=str(row.id),
                    delegator_principal_id=row.delegator_principal_id,
                    delegate_principal_id=row.delegate_principal_id,
                    delegated_roles=frozenset(row.delegated_roles),
                    starts_at=_as_aware_utc(row.starts_at),
                    expires_at=_as_aware_utc(row.expires_at),
                    status=row.status,
                )
                for row in rows
            ]
            available = delegated_roles_available(
                grants, delegate_principal_id=context.principal_id, at=now
            )
            granted = available & set(allowed)
            if granted:
                for grant in grants:
                    if grant.delegated_roles & granted and is_delegation_active(grant, at=now):
                        return replace(
                            context,
                            roles=context.roles | granted,
                            active_delegation_id=UUID(grant.id),
                            active_delegator_principal_id=grant.delegator_principal_id,
                        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"one of these roles (directly or via an active delegation) is required: "
            f"{', '.join(sorted(allowed))}",
        )

    return dependency


def enforce_organization(context: SecurityContext, requested: UUID) -> None:
    if "PlatformAdmin" in context.roles:
        return
    if context.organization_id != requested:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-organization access denied",
        )
