"""Current-principal identity endpoint (UX-1, module 21 SS5 / module 01 SS7 `GET /v1/me`).

A single read-only endpoint: it exposes exactly what the authenticated principal
already carries in its `SecurityContext` -- principal, roles, tenant scope, and the
persona derived server-side from OIDC groups (`aida.oidc.context_from_claims`) -- plus
which identity provider produced that context. Nothing here computes anything new.

This is the seam the experience shell uses to decide, per module 21 SS5, whether the
manual persona switcher may exist at all: `identity_provider == "OIDC"` means persona
is derived and not user-selectable, so the UI must not offer a picker; `"DEVELOPMENT"`
is the one mode where a browser-selected persona is legitimate, because it grants
nothing beyond the local iteration convenience module 01 already scopes development
identity to.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from aida.config import Settings, get_settings
from aida.schemas import ApiModel
from aida.security import SecurityContext, get_security_context

router = APIRouter(prefix="/v1", tags=["identity"])


class MeRead(ApiModel):
    principal_id: str
    principal_type: str
    organization_id: UUID | None
    roles: list[str]
    # None when running the development identity provider, or when an OIDC
    # principal's groups map to no configured persona and no default is set.
    persona: str | None
    # "OIDC" | "DEVELOPMENT" -- the exact gate `aida.security.get_security_context`
    # already branches on, echoed here rather than re-derived so the client checks
    # the one flag the server itself checks.
    identity_provider: str


@router.get(
    "/me",
    response_model=MeRead,
    summary="Current principal, roles, tenant scope, and server-derived persona",
)
async def get_me(
    context: SecurityContext = Depends(get_security_context),
    settings: Settings = Depends(get_settings),
) -> MeRead:
    return MeRead(
        principal_id=context.principal_id,
        principal_type=context.principal_type,
        organization_id=context.organization_id,
        roles=sorted(context.roles),
        persona=context.persona,
        identity_provider=settings.identity_provider.upper(),
    )
