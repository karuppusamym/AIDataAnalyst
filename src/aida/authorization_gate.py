"""The one call a surface makes to be authorized (ADR-0018, rollout step 3).

Everything before this module was machinery: a policy engine, an authorization
function, rule-derived membership, shadow mode, all tested and none of them
reachable from a single line of production traffic. This is the wiring, and it is
deliberately one function so that "is this surface authorized?" has one answer and
one place to read it.

Three properties are worth stating because each was a decision:

**It is called from the choke point, not from the handlers.** The gate sits inside
`QueryExecutionGateway`, which INV-2 already makes the only path to a warehouse.
Gating handlers instead would mean the gate is present on the handlers somebody
remembered, which is a different and much weaker claim.

**A refusal is a first-class outcome, not an exception that leaks.** The gate raises
`AuthorizationDenied` carrying a reason code and nothing else -- no policy text, no
resource value (INV-6). Callers translate it into whatever refusal their surface
already knows how to record, so an authorization denial is audited by the same code
that audits every other denial.

**An unresolved workspace is not an allow.** It is its own state, with its own
posture (`Settings.unresolved_workspace_posture`), because the honest description of
this rollout is that most callers do not yet name a workspace. Today that state
proceeds and is logged, which is what makes turning the gate on a non-event. It
becomes a denial by configuration -- one setting, no code change -- once clients pass
workspace ids, and that flip is the point of naming the state instead of hiding it
inside `allowed=True`.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from aida.security_types import SecurityContext
from aida.workspace_resolution import resolve_workspace
from aida.workspace_service import authorize_enforced
from atlas.platform.config import Settings

_log = structlog.get_logger(__name__)

# Postures for a request whose workspace could not be resolved.
SHADOW_UNRESOLVED = "SHADOW"
DENY_UNRESOLVED = "DENY"

# `SecurityContext.principal_type` is the platform's word; `principal_kind` is the
# policy engine's. Anything that is not recognisably a human or a registered agent is
# treated as a service, which is the most constrained of the three.
_PRINCIPAL_KINDS: dict[str, str] = {
    "USER": "HUMAN",
    "HUMAN": "HUMAN",
    "AGENT": "AGENT",
    "SERVICE": "SERVICE",
    "SERVICE_ACCOUNT": "SERVICE",
}


def principal_kind_of(context: SecurityContext) -> str:
    """Map the identity layer's principal type onto the policy engine's subject kind.

    Unknown types map to SERVICE rather than HUMAN. A new principal type arriving
    from the identity provider should land in the most constrained bucket until
    somebody decides otherwise, not inherit the permissions of a person.
    """
    return _PRINCIPAL_KINDS.get(context.principal_type.upper(), "SERVICE")


class AuthorizationDenied(PermissionError):
    """A refusal from the gate. Carries a reason code and no resource detail (INV-6)."""

    def __init__(self, reason_code: str, *, workspace_id: UUID | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.workspace_id = workspace_id


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """What the gate did. Returned only on the allowed path; refusals raise."""

    workspace_id: UUID | None
    reason_code: str
    # False when no workspace could be resolved, so no decision was reached at all.
    # A caller that treats this as "authorized" is making a claim the gate did not.
    decided: bool


async def gate(
    session: AsyncSession,
    context: SecurityContext,
    *,
    settings: Settings,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    workspace_id: UUID | None = None,
    datasource_id: UUID | None = None,
    schema_name: str | None = None,
    classifications: frozenset[str] = frozenset(),
    certification: str | None = None,
    quality_state: str | None = None,
    freshness_state: str | None = None,
    now: datetime | None = None,
) -> GateOutcome:
    """Authorize one access, or raise `AuthorizationDenied`.

    Resolves the workspace subject-independently (see `workspace_resolution`), then
    defers entirely to `authorize_enforced` -- which means a workspace in SHADOW
    records what it would have done and allows, and a workspace in ENFORCE denies.
    The gate adds no judgement of its own on that path; its only original decision is
    what to do when there is no workspace to ask about.
    """
    resolution = await resolve_workspace(
        session,
        organization_id=context.organization_id,
        requested_workspace_id=workspace_id,
        datasource_id=datasource_id,
        now=now,
    )
    if resolution.workspace_id is None:
        if settings.unresolved_workspace_posture == DENY_UNRESOLVED:
            raise AuthorizationDenied(resolution.reason_code)
        # Value-free (INV-6), and at warning level on purpose: this is the metric that
        # says how far the client migration has to go before the posture can flip.
        _log.warning(
            "authorization.workspace_unresolved",
            reason_code=resolution.reason_code,
            action=action,
            resource_type=resource_type,
            principal_kind=principal_kind_of(context),
        )
        return GateOutcome(workspace_id=None, reason_code=resolution.reason_code, decided=False)

    result = await authorize_enforced(
        session,
        context,
        durable_divergence=True,
        workspace_id=resolution.workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        datasource_id=datasource_id,
        schema_name=schema_name,
        classifications=classifications,
        certification=certification,
        quality_state=quality_state,
        freshness_state=freshness_state,
        principal_kind=principal_kind_of(context),
        now=now,
    )
    if not result.allowed:
        raise AuthorizationDenied(result.reason_code, workspace_id=result.workspace_id)
    return GateOutcome(
        workspace_id=result.workspace_id, reason_code=result.reason_code, decided=True
    )
