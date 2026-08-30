"""Which workspace is a request in? (ADR-0018, rollout step 3).

`workspace_service.authorize` needs a workspace id, and almost nothing in the
platform's existing API surface supplies one -- the contracts predate ADR-0018.
This module answers the question for those callers, and the *way* it answers is
the point.

**Resolution is subject-independent.** The tempting implementation is "find the
workspaces this principal belongs to, intersect with the ones bound to this
datasource, take the match". That reads as helpful and is a hole: it picks the
workspace by where the caller already has access and then asks whether the caller
has access there, which is a question with a foregone answer. Every rule below
looks only at the request and at platform state, never at who is asking.

So there are exactly two ways to get a workspace:

* the request names one (the destination, once clients are migrated), or
* the datasource has exactly *one* live binding, so the answer is forced.

A datasource with two live bindings has no forced answer, and inventing one would
be guessing which grant to evaluate against. That is `WORKSPACE_AMBIGUOUS`, and it
is a refusal to answer rather than a denial -- what happens next is the caller's
enforcement posture to decide, not this module's.

Nothing here validates the workspace it returns. `authorize` already checks
existence, status and organization, and fails closed on each; duplicating those
checks here would mean two places that must agree about what a usable workspace is.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import SourceBinding
from aida.timeutil import is_live

# The request named a workspace. `authorize` still decides whether it is usable.
RESOLVED_EXPLICIT = "RESOLVED_EXPLICIT"
# One live binding reaches this datasource, so the workspace is forced.
RESOLVED_SOLE_BINDING = "RESOLVED_SOLE_BINDING"
# Nothing to resolve from: no workspace named and no datasource in the request.
WORKSPACE_NOT_SPECIFIED = "WORKSPACE_NOT_SPECIFIED"
# More than one live binding. The correct answer exists but the request did not say.
WORKSPACE_AMBIGUOUS = "WORKSPACE_AMBIGUOUS"
# No live binding at all -- nothing may reach this datasource from any workspace.
NO_BINDING_FOR_DATASOURCE = "NO_BINDING_FOR_DATASOURCE"
# No tenant claim, so not even the binding lookup is safe to scope (INV-5).
NO_ORGANIZATION_CONTEXT = "NO_ORGANIZATION_CONTEXT"


@dataclass(frozen=True, slots=True)
class WorkspaceResolution:
    """A workspace id, or a reason code saying why there is not one."""

    workspace_id: UUID | None
    reason_code: str

    @property
    def resolved(self) -> bool:
        return self.workspace_id is not None


async def live_bindings_for_datasource(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    now: datetime | None = None,
) -> tuple[SourceBinding, ...]:
    """Every ACTIVE, unexpired binding reaching this datasource, in id order.

    Ordered so that "how many" and "which one" are stable across calls; the caller
    only ever uses the single-element case, but a stable order makes the ambiguous
    case reproducible in a log.
    """
    moment = now or datetime.now(UTC)
    rows = (
        await session.scalars(
            select(SourceBinding)
            .where(
                SourceBinding.organization_id == organization_id,
                SourceBinding.datasource_id == datasource_id,
                SourceBinding.status == "ACTIVE",
            )
            .order_by(SourceBinding.id)
        )
    ).all()
    return tuple(row for row in rows if is_live(row.expires_at, moment))


async def resolve_workspace(
    session: AsyncSession,
    *,
    organization_id: UUID | None,
    requested_workspace_id: UUID | None = None,
    datasource_id: UUID | None = None,
    now: datetime | None = None,
) -> WorkspaceResolution:
    """Pick the workspace this request belongs to, without consulting the caller.

    Order:

    1. An explicitly named workspace wins. It is returned unvalidated on purpose --
       `authorize` owns the existence, status and organization checks, and a caller
       naming a workspace it cannot use gets a denial there, which is the right
       outcome and the right place for it.
    2. Otherwise, if the request touches a datasource, a *single* live binding
       forces the answer. This is what carries the estate the ADR-0018 migration
       created, where every datasource got exactly one grandfathered binding from
       its project.
    3. Otherwise there is no answer, and saying so is the whole contribution.
    """
    if requested_workspace_id is not None:
        return WorkspaceResolution(requested_workspace_id, RESOLVED_EXPLICIT)
    if organization_id is None:
        # An absent tenant claim is not a licence to search every organization's
        # bindings for a match. Refuse to resolve (INV-4/INV-5).
        return WorkspaceResolution(None, NO_ORGANIZATION_CONTEXT)
    if datasource_id is None:
        return WorkspaceResolution(None, WORKSPACE_NOT_SPECIFIED)

    bindings = await live_bindings_for_datasource(
        session, organization_id=organization_id, datasource_id=datasource_id, now=now
    )
    if not bindings:
        return WorkspaceResolution(None, NO_BINDING_FOR_DATASOURCE)
    if len(bindings) > 1:
        return WorkspaceResolution(None, WORKSPACE_AMBIGUOUS)
    return WorkspaceResolution(bindings[0].workspace_id, RESOLVED_SOLE_BINDING)
