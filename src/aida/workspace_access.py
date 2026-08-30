"""Rule-derived workspace membership, and shadow-mode authorization (ADR-0018 rollout).

Two mechanisms, both of which exist because of the same discovery: the ADR-0018 migration
creates one workspace per project and **zero memberships**, because there is nothing in
this codebase to backfill them from. There is no persisted principal table at all --
identity and roles arrive as OIDC claims per request and are never stored -- so no record
exists of who used which project.

That makes the obvious next step, "wire `authorize` into the read paths", a change that
would deny every request in the platform. Both mechanisms here are the way to make that
step safe rather than the way to postpone it.

**Rule-derived membership.** A `workspace_access_rule` maps an identity-provider role onto
a workspace role, scoped to one workspace, to everything under a business node, or (rarely)
org-wide. One rule covers every migrated workspace without inventing an access grant nobody
made, and revoking the rule revokes the access. Rules only ever *grant*: an explicit
membership row is evaluated alongside them, and a DENY policy still outranks everything.

**Shadow mode.** A workspace in `SHADOW` computes the full attribute-based decision,
records divergences, and never denies. Flipping to `ENFORCE` then becomes a measurement --
"this workspace has recorded no would-be denials for a week" -- rather than a leap of
faith. Introducing an authorization system in enforcing mode is how you discover in
production that it denies something it should not.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_graph import descendant_ids
from aida.models import (
    AuthorizationShadowRecord,
    BusinessAssignment,
    Workspace,
    WorkspaceAccessRule,
)
from aida.timeutil import is_live

SHADOW = "SHADOW"
ENFORCE = "ENFORCE"


def _live(expires_at: datetime | None, moment: datetime) -> bool:
    return is_live(expires_at, moment)


async def rule_derived_roles(
    session: AsyncSession,
    workspace: Workspace,
    subject_roles: frozenset[str],
    *,
    now: datetime | None = None,
) -> frozenset[str]:
    """Workspace roles this principal gets from rules rather than from a membership row.

    Evaluated narrowest-first only in the sense that all matching rules union together --
    a rule cannot take access away, so there is no precedence question to resolve. If that
    ever changes, it stops being a grant mechanism and needs its own ADR.
    """
    if not subject_roles:
        return frozenset()
    moment = now or datetime.now(UTC)

    candidates = (
        await session.scalars(
            select(WorkspaceAccessRule).where(
                WorkspaceAccessRule.organization_id == workspace.organization_id,
                WorkspaceAccessRule.status == "ACTIVE",
                WorkspaceAccessRule.subject_role.in_(subject_roles),
                or_(
                    WorkspaceAccessRule.workspace_id == workspace.id,
                    WorkspaceAccessRule.workspace_id.is_(None),
                ),
            )
        )
    ).all()
    if not candidates:
        return frozenset()

    granted: set[str] = set()
    node_scoped: list[WorkspaceAccessRule] = []
    for rule in candidates:
        if not _live(rule.expires_at, moment):
            continue
        if rule.business_node_id is None:
            granted.add(rule.workspace_role)
        else:
            node_scoped.append(rule)

    if node_scoped:
        # A node-scoped rule applies when the workspace itself is classified at or below
        # that node. Resolved once per distinct node rather than once per rule.
        assigned = frozenset(
            (
                await session.scalars(
                    select(BusinessAssignment.business_node_id).where(
                        BusinessAssignment.organization_id == workspace.organization_id,
                        BusinessAssignment.target_type == "WORKSPACE",
                        BusinessAssignment.target_id == str(workspace.id),
                        BusinessAssignment.status == "ACTIVE",
                        BusinessAssignment.effective_to.is_(None),
                    )
                )
            ).all()
        )
        if assigned:
            for node_id in {rule.business_node_id for rule in node_scoped}:
                if node_id is None:
                    continue
                subtree = await descendant_ids(session, workspace.organization_id, node_id)
                if assigned & subtree:
                    granted.update(
                        rule.workspace_role
                        for rule in node_scoped
                        if rule.business_node_id == node_id
                    )
    return frozenset(granted)


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """What the caller should actually do, and what the engine thought."""

    enforced: bool
    allowed: bool
    reason_code: str
    shadow_would_have_denied: bool


def apply_enforcement_mode(
    workspace: Workspace, *, allowed: bool, reason_code: str
) -> ShadowOutcome:
    """Translate a decision into an action, according to the workspace's mode.

    In `SHADOW`, a denial becomes an allow carrying `shadow_would_have_denied=True`. The
    caller is expected to record that (see `record_divergence`) and proceed. In `ENFORCE`
    the decision stands.

    Deliberately *not* a global switch: rollout is per workspace, so a team that has
    reviewed its shadow record can enforce while the rest of the estate has not.
    """
    if workspace.authorization_mode == ENFORCE:
        return ShadowOutcome(
            enforced=True,
            allowed=allowed,
            reason_code=reason_code,
            shadow_would_have_denied=False,
        )
    return ShadowOutcome(
        enforced=False,
        allowed=True,
        reason_code="SHADOW_MODE_NOT_ENFORCING",
        shadow_would_have_denied=not allowed,
    )


def record_divergence(
    session: AsyncSession,
    workspace: Workspace,
    *,
    principal_id: str,
    principal_kind: str,
    action: str,
    resource_type: str,
    resource_id: str | None,
    reason_code: str,
    matched_policy_code: str | None = None,
) -> None:
    """Persist one would-be denial. Agreements are counted, never stored.

    Storing every allowed read would be a second access log at request volume carrying no
    information -- the interesting event is disagreement between what the new engine says
    and what actually happened.
    """
    session.add(
        AuthorizationShadowRecord(
            organization_id=workspace.organization_id,
            workspace_id=workspace.id,
            principal_id=principal_id,
            principal_kind=principal_kind,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            shadow_allowed=False,
            reason_code=reason_code,
            matched_policy_code=matched_policy_code,
        )
    )


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Is this workspace safe to switch to ENFORCE?"""

    workspace_id: UUID
    mode: str
    would_be_denials: int
    distinct_principals_affected: int
    top_reason_codes: tuple[tuple[str, int], ...]
    ready: bool


async def enforcement_readiness(
    session: AsyncSession, workspace: Workspace, *, since: datetime | None = None
) -> ReadinessReport:
    """Summarise the shadow record so the decision to enforce is evidence-based.

    `ready` is deliberately a blunt "no recorded would-be denials in the window". It is a
    prompt to look, not an approval: a workspace nobody used this week also has no
    divergences, and that is not the same as being safe.
    """
    statement = select(
        AuthorizationShadowRecord.reason_code,
        func.count().label("hits"),
        func.count(func.distinct(AuthorizationShadowRecord.principal_id)).label("principals"),
    ).where(AuthorizationShadowRecord.workspace_id == workspace.id)
    if since is not None:
        statement = statement.where(AuthorizationShadowRecord.observed_at >= since)
    rows = (
        await session.execute(
            statement.group_by(AuthorizationShadowRecord.reason_code).order_by(
                func.count().desc()
            )
        )
    ).all()
    total = sum(int(row[1]) for row in rows)
    principals = max((int(row[2]) for row in rows), default=0)
    return ReadinessReport(
        workspace_id=workspace.id,
        mode=workspace.authorization_mode,
        would_be_denials=total,
        distinct_principals_affected=principals,
        top_reason_codes=tuple((str(row[0]), int(row[1])) for row in rows[:5]),
        ready=(total == 0),
    )
