"""The access axis: workspaces, membership, source bindings, authorization (ADR-0018).

A workspace is the unit of grant, membership, budget and blast radius. It replaces
the line-of-business / data-domain segment of the old tenancy path, leaving the
tenancy scope on governed records as `(organization_id, workspace_id)` -- two levels,
short and stable, because a reorganisation must not be a data migration.

Three behaviours in here carry most of the design weight:

* **Source bindings expire.** The same warehouse serves many workspaces, and two
  workspaces on one source can legitimately see different things. The binding is
  where that is expressed, audited, and *time-boxed*. Expiry is the mechanism that
  stops entitlement creep, and it is the thing almost every platform omits.
* **Binding approval routes to the source owner**, not to a central administrative
  queue, because central queues are where these requests die.
* **Authorization fails closed at every step** (INV-4): no membership, no binding,
  an expired binding, a binding outside its schema scope, or no applicable ALLOW
  policy each produce a refusal with a reason code -- never a degraded success.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_graph import classification_scope, load_policies
from aida.models import SourceBinding, Workspace, WorkspaceMembership
from aida.policy_engine import PolicyDecision, Resource, Subject, evaluate
from aida.security_types import SecurityContext
from aida.timeutil import is_expired
from aida.workspace_access import (
    apply_enforcement_mode,
    record_divergence,
    record_divergence_durably,
    rule_derived_roles,
)

# Workspace roles, weakest first. Roles are additive across memberships; a DENY from
# policy always wins over a grant from a role, and maker != checker (INV-8) holds
# regardless of role -- a workspace_owner who proposes still cannot approve.
WORKSPACE_ROLES = ("viewer", "analyst", "steward", "reviewer", "workspace_owner")

_ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"READ_METADATA"}),
    "analyst": frozenset(
        {"READ_METADATA", "READ_DATA", "EXECUTE_TOOL", "CONSUME_CONTEXT", "PROPOSE"}
    ),
    "steward": frozenset(
        {"READ_METADATA", "READ_DATA", "EXECUTE_TOOL", "CONSUME_CONTEXT", "PROPOSE"}
    ),
    "reviewer": frozenset(
        {"READ_METADATA", "READ_DATA", "EXECUTE_TOOL", "CONSUME_CONTEXT", "APPROVE"}
    ),
    "workspace_owner": frozenset(
        {"READ_METADATA", "READ_DATA", "EXECUTE_TOOL", "CONSUME_CONTEXT", "PROPOSE", "APPROVE"}
    ),
}

# How long a source binding lives before it must be reviewed again. A year is long
# enough not to be busywork and short enough that a forgotten grant surfaces before
# it becomes permanent.
DEFAULT_BINDING_DAYS = 365


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    """The outcome of an authorization check, safe to log and to audit.

    Value-free by construction (INV-6): reason codes and identifiers only, never a
    resource value and never the policy expression that fired.
    """

    allowed: bool
    reason_code: str
    workspace_id: UUID | None = None
    binding_id: UUID | None = None
    decision: PolicyDecision | None = None


def _expired(expires_at: datetime | None, now: datetime) -> bool:
    # Delegates rather than comparing directly: a stored timestamp reads back naive from
    # SQLite and aware from PostgreSQL, so the obvious comparison is wrong on one backend
    # only. This is an expiry check on an access grant, which is the worst place for a
    # backend-dependent answer.
    return is_expired(expires_at, now)


async def membership_roles(
    session: AsyncSession, workspace_id: UUID, principal_id: str, *, now: datetime | None = None
) -> frozenset[str]:
    """Live workspace roles for a principal. An expired membership grants nothing."""
    moment = now or datetime.now(UTC)
    rows = await session.scalars(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.principal_id == principal_id,
            WorkspaceMembership.status == "ACTIVE",
        )
    )
    return frozenset(
        row.role for row in rows.all() if not _expired(row.expires_at, moment)
    )


async def active_binding(
    session: AsyncSession,
    workspace_id: UUID,
    datasource_id: UUID,
    *,
    now: datetime | None = None,
) -> SourceBinding | None:
    """The live binding letting this workspace reach this datasource, or None.

    Returns None for an expired binding rather than raising, so callers get one
    refusal path for "never had access" and "no longer has access" -- the caller
    should not be able to tell those apart from the outside, and neither should an
    attacker probing for which datasources exist.
    """
    moment = now or datetime.now(UTC)
    binding = await session.scalar(
        select(SourceBinding).where(
            SourceBinding.workspace_id == workspace_id,
            SourceBinding.datasource_id == datasource_id,
            SourceBinding.status == "ACTIVE",
        )
    )
    if binding is None or _expired(binding.expires_at, moment):
        return None
    return binding


def binding_covers_schema(binding: SourceBinding, schema_name: str | None) -> bool:
    """An empty schema scope means the whole datasource; otherwise it is an allowlist."""
    scope = list(binding.schema_scope or [])
    if not scope:
        return True
    if schema_name is None:
        return False
    return schema_name in scope


async def authorize(
    session: AsyncSession,
    context: SecurityContext,
    *,
    workspace_id: UUID,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    datasource_id: UUID | None = None,
    schema_name: str | None = None,
    classifications: frozenset[str] = frozenset(),
    certification: str | None = None,
    quality_state: str | None = None,
    freshness_state: str | None = None,
    principal_kind: str = "HUMAN",
    now: datetime | None = None,
) -> AuthorizationResult:
    """The single authorization entry point for workspace-scoped access.

    Order matters, and every step fails closed:

    1. The workspace exists, is ACTIVE, and belongs to the caller's organization.
    2. The caller holds a live membership whose role permits the action at all.
       This is the cheap check and it runs before any policy loading.
    3. If the request touches a datasource, a live, unexpired binding covering the
       requested schema must exist.
    4. The attribute-based policy set decides, with the resource described by its
       classification closure rather than by its identity.
    """
    moment = now or datetime.now(UTC)

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.status != "ACTIVE":
        return AuthorizationResult(allowed=False, reason_code="WORKSPACE_UNAVAILABLE")
    if context.organization_id is None:
        # Fail closed (INV-4/INV-5). Written first as
        # `if context.organization_id is not None and <mismatch>`, which skipped tenant
        # isolation entirely for a caller that simply omitted the organization -- and
        # development identity makes `X-Organization-Id` optional, so None is reachable
        # from outside. An absent tenant claim is a denial, never a waiver.
        return AuthorizationResult(allowed=False, reason_code="NO_ORGANIZATION_CONTEXT")
    if workspace.organization_id != context.organization_id:
        # 403 rather than 404 everywhere, so "exists elsewhere" and "does not exist"
        # are indistinguishable from outside (30-contracts/02-api-conventions.md).
        # Note there is deliberately no PlatformAdmin bypass here, unlike
        # `enforce_organization`: a workspace is a tenancy boundary, and INV-5 says
        # isolation is total.
        return AuthorizationResult(allowed=False, reason_code="CROSS_ORGANIZATION_DENIED")

    # Explicit membership rows, plus anything the identity provider's roles grant through
    # a workspace access rule. The rule path is what makes the ADR-0018 migration's
    # memberless workspaces usable at all: there is no persisted principal table to
    # backfill memberships from, so membership is derived rather than invented.
    roles = await membership_roles(session, workspace_id, context.principal_id, now=moment)
    roles = roles | await rule_derived_roles(
        session, workspace, frozenset(context.roles), now=moment
    )
    if not roles:
        return AuthorizationResult(
            allowed=False, reason_code="NO_WORKSPACE_MEMBERSHIP", workspace_id=workspace_id
        )
    permitted = frozenset().union(*(_ROLE_ACTIONS.get(role, frozenset()) for role in roles))
    if action not in permitted:
        return AuthorizationResult(
            allowed=False, reason_code="ROLE_DOES_NOT_PERMIT_ACTION", workspace_id=workspace_id
        )

    binding_id: UUID | None = None
    if datasource_id is not None:
        binding = await active_binding(session, workspace_id, datasource_id, now=moment)
        if binding is None:
            return AuthorizationResult(
                allowed=False, reason_code="NO_ACTIVE_SOURCE_BINDING", workspace_id=workspace_id
            )
        if not binding_covers_schema(binding, schema_name):
            return AuthorizationResult(
                allowed=False,
                reason_code="OUTSIDE_BINDING_SCHEMA_SCOPE",
                workspace_id=workspace_id,
                binding_id=binding.id,
            )
        permitted_classifications = frozenset(binding.permitted_classifications or ())
        if permitted_classifications and not classifications <= permitted_classifications:
            return AuthorizationResult(
                allowed=False,
                reason_code="CLASSIFICATION_OUTSIDE_BINDING",
                workspace_id=workspace_id,
                binding_id=binding.id,
            )
        binding_id = binding.id

    node_scope: frozenset[UUID] = frozenset()
    if resource_id is not None:
        node_scope = await classification_scope(
            session, workspace.organization_id, resource_type, resource_id, as_of=moment
        )

    decision = evaluate(
        await load_policies(session, workspace.organization_id),
        Subject(
            principal_id=context.principal_id,
            principal_kind=principal_kind,
            roles=frozenset(context.roles) | roles,
            workspace_id=workspace_id,
            purpose=context.business_purpose,
            isolation_boundary_id=workspace.isolation_boundary_id,
        ),
        Resource(
            resource_type=resource_type,
            resource_id=resource_id,
            classifications=classifications,
            business_node_ids=node_scope,
            certification=certification,
            datasource_id=datasource_id,
            schema_name=schema_name,
            quality_state=quality_state,
            freshness_state=freshness_state,
        ),
        action,
        now=moment,
    )
    return AuthorizationResult(
        allowed=decision.allowed,
        reason_code=decision.reason_code,
        workspace_id=workspace_id,
        binding_id=binding_id,
        decision=decision,
    )


async def authorize_enforced(
    session: AsyncSession,
    context: SecurityContext,
    *,
    durable_divergence: bool = False,
    **kwargs: Any,
) -> AuthorizationResult:
    """`authorize`, then honour the workspace's enforcement mode. Call this from surfaces.

    The distinction matters and is the whole reason this function exists separately:
    `authorize` answers *what is the correct decision*, and this answers *what should
    happen right now*. In a `SHADOW` workspace those differ -- the correct decision may be
    a denial while the right action is to proceed and write down that it would have been
    denied.

    Wiring surfaces to `authorize` directly would enforce attribute-based access the
    moment it was introduced, against workspaces the ADR-0018 migration created with no
    memberships at all. That is a platform-wide outage dressed as a security improvement.
    Every caller should use this; `authorize` stays public because the authorization-probe
    endpoint deliberately wants the unmodulated answer.

    `durable_divergence` decides where the shadow record lands. The default writes it to
    the caller's session, which is right for the administration endpoints that commit.
    Pass True from anywhere that does not commit, or that might roll back -- a read path,
    or an execution that is about to be rejected -- because a divergence discarded with
    the request it describes biases the readiness report towards success, and that report
    is what a human reads before flipping a workspace to ENFORCE.
    """
    result = await authorize(session, context, **kwargs)
    workspace = await session.get(Workspace, kwargs["workspace_id"])
    if workspace is None:
        # No workspace, no mode to honour, and no safe reading other than refusal.
        return result

    outcome = apply_enforcement_mode(
        workspace, allowed=result.allowed, reason_code=result.reason_code
    )
    if outcome.shadow_would_have_denied:
        principal_kind = str(kwargs.get("principal_kind", "HUMAN"))
        action = str(kwargs.get("action", ""))
        resource_type = str(kwargs.get("resource_type", ""))
        resource_id = kwargs.get("resource_id")
        matched_policy_code = result.decision.matched_policy_code if result.decision else None
        if durable_divergence:
            await record_divergence_durably(
                workspace.id,
                workspace.organization_id,
                principal_id=context.principal_id,
                principal_kind=principal_kind,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                reason_code=result.reason_code,
                matched_policy_code=matched_policy_code,
            )
        else:
            record_divergence(
                session,
                workspace,
                principal_id=context.principal_id,
                principal_kind=principal_kind,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                reason_code=result.reason_code,
                matched_policy_code=matched_policy_code,
            )
    return AuthorizationResult(
        allowed=outcome.allowed,
        reason_code=outcome.reason_code,
        workspace_id=result.workspace_id,
        binding_id=result.binding_id,
        decision=result.decision,
    )


async def create_workspace(
    session: AsyncSession,
    *,
    organization_id: UUID,
    name: str,
    slug: str,
    purpose: str,
    owner_principal: str,
    isolation_boundary_id: UUID | None = None,
    monthly_cost_ceiling: int | None = None,
) -> Workspace:
    """Create a workspace and seat its first owner in one step.

    Deliberately one call: a workspace with no owner is a workspace nobody can
    administer, and making that state reachable invites it to occur. Creating a
    workspace should be minutes, not a project -- the opposite of the operating-model
    burden that stalls commercial governance rollouts.
    """
    workspace = Workspace(
        organization_id=organization_id,
        name=name,
        slug=slug,
        purpose=purpose,
        isolation_boundary_id=isolation_boundary_id,
        monthly_cost_ceiling=monthly_cost_ceiling,
    )
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMembership(
            organization_id=organization_id,
            workspace_id=workspace.id,
            principal_id=owner_principal,
            role="workspace_owner",
            granted_by=owner_principal,
        )
    )
    await session.flush()
    return workspace


async def request_binding(
    session: AsyncSession,
    *,
    organization_id: UUID,
    workspace_id: UUID,
    datasource_id: UUID,
    purpose: str,
    requested_by: str,
    schema_scope: list[str] | None = None,
    permitted_classifications: list[str] | None = None,
    masking_profile: str = "DEFAULT",
    max_query_cost: int | None = None,
) -> SourceBinding:
    """Open a PENDING_APPROVAL binding request. It grants nothing until approved."""
    binding = SourceBinding(
        organization_id=organization_id,
        workspace_id=workspace_id,
        datasource_id=datasource_id,
        schema_scope=list(schema_scope or []),
        permitted_classifications=list(permitted_classifications or []),
        masking_profile=masking_profile,
        purpose=purpose,
        max_query_cost=max_query_cost,
        requested_by=requested_by,
    )
    session.add(binding)
    await session.flush()
    return binding


class BindingApprovalError(RuntimeError):
    """Raised when a binding cannot be approved. Carries a reason code, not a value."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


async def approve_binding(
    session: AsyncSession,
    binding: SourceBinding,
    *,
    approver_principal: str,
    valid_for_days: int = DEFAULT_BINDING_DAYS,
    now: datetime | None = None,
) -> SourceBinding:
    """Approve a binding, setting its expiry.

    Maker != checker (INV-8): the principal who requested a binding can never be the
    principal who approves it. Enforced here as well as in the governance review
    queue, because a binding is a grant of source access and is exactly the kind of
    object where a self-approval path must not exist.
    """
    moment = now or datetime.now(UTC)
    if binding.status != "PENDING_APPROVAL":
        raise BindingApprovalError("BINDING_NOT_PENDING")
    if binding.requested_by == approver_principal:
        raise BindingApprovalError("MAKER_CHECKER_SEPARATION_REQUIRED")
    binding.status = "ACTIVE"
    binding.approved_by = approver_principal
    binding.approved_at = moment
    binding.expires_at = moment.replace(microsecond=0) + timedelta(days=valid_for_days)
    await session.flush()
    return binding
